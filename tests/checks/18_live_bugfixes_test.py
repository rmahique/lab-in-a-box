#!/usr/bin/env python3
# Regression tests for bugs live disposable-VM smoke testing on
# nuc6.mydemo.lab surfaced during the python_migration cutover (2026-08-28),
# fixed in the same session. No live host needed here — SSH/subprocess are
# mocked. Run from 18_live_bugfixes.sh, in its own container — see
# tests/run_tests.sh.
import socket
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import services  # noqa: E402
import backends  # noqa: E402
import lab_creation as lc  # noqa: E402

sys.path.insert(0, str(_REPO / "scripts"))
import install_uyuni  # noqa: E402

# This test container has no local virsh/virt-install — force
# run_libvirt_tool()'s local branch so the mocked subprocess.run below is
# what actually gets called (matching the real automation VM host, which
# always has these tools locally), not the SSH-fallback branch. See
# 10_lab_creation_core_test.py's identical patch for the full rationale.
lc._has_local_binary = lambda binary: True

# add_to_dns/del_from_dns/add_service_dns also touch a LOCAL zone file
# (NAMED_ZONE_DIR / "<domain>.lan") — real absolute paths under
# /var/lib/named, which must never be touched for real in this containerized
# test. Point it at a scratch tempdir instead.
services.NAMED_ZONE_DIR = Path(tempfile.mkdtemp())

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeSSH:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []

    def __call__(self, hostname, cmd, **kwargs):
        self.calls.append((hostname, cmd, kwargs))
        for substr, result in self.responses:
            if substr in cmd:
                return result
        return FakeResult()


# ── Bug 1: an unreachable secondary DNS server must not abort provisioning ──
# (found live: REMOTE_DNS_SERVERS' second host was down — "No route to
# host" — and add_to_dns raised RuntimeError, aborting VM creation entirely,
# even though bash's own add_to_dns has no such check and just moves on.)
def _dying_ssh(hostname, cmd, **kwargs):
    if kwargs.get("check", True):
        raise RuntimeError("SSH command failed (rc=255) on {}: no route to host".format(hostname))
    return FakeResult(returncode=255)


services.ssh_run = _dying_ssh
svc = services.DNSService()
svc.restart_named = lambda: None  # avoid a real local `systemctl restart named`

try:
    svc.add_to_dns("vm1.mydemo.lab", "192.168.88.199", "mydemo.lab", "88.168.192",
                    remote_dns_servers=["1.2.3.4"])
    ok = True
except RuntimeError:
    ok = False
check("DNSService.add_to_dns: an unreachable secondary DNS server does not raise", ok)

try:
    svc.del_from_dns("vm1.mydemo.lab", "192.168.88.199", "mydemo.lab", "88.168.192",
                      remote_dns_servers=["1.2.3.4"])
    ok = True
except RuntimeError:
    ok = False
check("DNSService.del_from_dns: an unreachable secondary DNS server does not raise", ok)

try:
    svc.add_service_dns(
        {"nodes": {"vm1": {"myip": "192.168.88.10", "kcluster": "c1"}}},
        "c1", "rke2", "cluster1", "mydemo.lab", remote_dns_servers=["1.2.3.4"])
    ok = True
except RuntimeError:
    ok = False
check("DNSService.add_service_dns: an unreachable secondary DNS server does not raise", ok)

# And confirm it's genuinely check=False being passed, not just a lucky
# no-op — every remote-server SSH call the two DNS-mutating paths make must
# explicitly opt out of raising.
fake = FakeSSH()
services.ssh_run = fake
svc.add_to_dns("vm1.mydemo.lab", "192.168.88.199", "mydemo.lab", "88.168.192",
                remote_dns_servers=["1.2.3.4"])
remote_calls = [kw for h, c, kw in fake.calls if h == "1.2.3.4"]
check("DNSService.add_to_dns: every remote-server call passes check=False",
      len(remote_calls) >= 3 and all(kw.get("check") is False for kw in remote_calls))


# ── Bug 2: --qemu-commandline must be one argv element, not two ─────────────
# (found live: virt-install rejected the two-element ["--qemu-commandline",
# "-fw_cfg ..."] form with "expected one argument" — argparse saw the
# leading "-" in the value and treated it as a new flag. bash's own
# equivalent uses --qemu-commandline="..." — a single token — precisely to
# avoid this.)
subproc_calls = []


def _fake_run(args, **kwargs):
    subproc_calls.append(args)
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_run
backend = backends.LibvirtBackend(
    "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa",
    remote_host="hv1", iso_loc="/iso", vm_img_loc="/var/lib/libvirt/images",
    lab_setup_path="/srv/www/htdocs/lab_creation",
)
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="", ign_file="vm1.ign", com_file="vm1",
)
argv = subproc_calls[-1]
check("create_vm (ignition+combustion): --qemu-commandline is a single argv element",
      "--qemu-commandline" not in argv)
qemu_arg = next((a for a in argv if a.startswith("--qemu-commandline=")), None)
check("create_vm (ignition+combustion): --qemu-commandline=<value> form is used", qemu_arg is not None)
check("create_vm (ignition+combustion): value carries both fw_cfg entries",
      qemu_arg is not None and "opt/com.coreos/config" in qemu_arg and "opt/org.opensuse.combustion/script" in qemu_arg)


# ── disk_format="raw": the USB-delivery lab-host VM's own disk needs to be
# dd-able directly onto a USB block device afterward, which QCOW2's own
# container format doesn't allow — a deliberate, narrow exception to this
# project's usual QCOW2-everywhere convention. Default stays qcow2,
# byte-for-byte unchanged.
subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="", ign_file="vm1.ign", com_file="vm1",
)
disk_arg = next(a for a in subproc_calls[-1] if a.startswith("size="))
check("create_vm: default disk_format is still qcow2, no driver.type override",
      ".qcow2" in disk_arg and "driver.type" not in disk_arg)

subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="", ign_file="vm1.ign", com_file="vm1", disk_format="raw",
)
disk_arg = next(a for a in subproc_calls[-1] if a.startswith("size="))
check("create_vm: disk_format='raw' uses a .raw path with an explicit driver.type",
      ".raw" in disk_arg and ".qcow2" not in disk_arg and "driver.type=raw" in disk_arg)


# ── vm_machine: a 2015-era CentOS 7 GenericCloud image (kernel 3.10.0-229)
# hangs in a dracut emergency shell ("Not all disks have been found") when
# booted under virt-install's own q35 default — confirmed live 2026-09-02
# on a completely unmodified clone of the source image (so this is a
# genuine chipset/old-kernel incompatibility, not anything config_method-
# specific). --machine pc (legacy i440fx) boots it cleanly. Left empty by
# default so every already-working image is unaffected.
subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="virt_customize", ign_file="vm1.ign", com_file="vm1",
)
check("create_vm: no vm_machine given -> no --machine flag at all, virt-install picks its own default",
      "--machine" not in subproc_calls[-1])

subproc_calls.clear()
backend.create_vm(
    "vm1", "2", "4096", "40", "network=default,model=virtio",
    config_method="virt_customize", ign_file="vm1.ign", com_file="vm1", vm_machine="pc",
)
argv = subproc_calls[-1]
check("create_vm: vm_machine='pc' adds --machine pc to the virt-install invocation",
      "--machine" in argv and argv[argv.index("--machine") + 1] == "pc")


# ── copy_vm_image / disk_format: found live on nuc6 (2026-08-31) — create_vm's
# disk_format="raw" landed its own new disk at <vm_name>.raw, but
# copy_vm_image() (which actually populates the disk with the source
# image's content, called BEFORE create_vm) still unconditionally wrote to
# <vm_name>.qcow2 regardless — create_vm's --import would then have found
# nothing at .raw and silently created a blank, non-bootable disk instead
# of using the copied image. Also: a plain `cp` renamed to .raw would not
# even be a valid raw disk (the source is genuinely QCOW2-container
# content) — needs a real `qemu-img convert -O raw`, not a rename.
subproc_calls.clear()
backend.copy_vm_image("source.qcow2", "vm1", "40", config_method="cloud-init")
cmds = [" ".join(c) for c in subproc_calls]
check("copy_vm_image: default disk_format still plain `cp` to <vm_name>.qcow2, unchanged",
      any("cp" in c and "vm1.qcow2" in c for c in cmds)
      and not any("qemu-img convert" in c for c in cmds))
check("copy_vm_image: default disk_format resizes with -f qcow2",
      any("qemu-img resize -f qcow2" in c and "vm1.qcow2" in c for c in cmds))
check("copy_vm_image: default disk_format never runs a GPT repair (qcow2 disks aren't grown post-copy)",
      not any("sgdisk" in c for c in cmds))

subproc_calls.clear()
backend.copy_vm_image("source.qcow2", "vm1", "40", config_method="cloud-init", disk_format="raw")
cmds = [" ".join(c) for c in subproc_calls]
check("copy_vm_image: disk_format='raw' converts (not cp's) the source into <vm_name>.raw",
      any("qemu-img convert -O raw" in c and "source.qcow2" in c and "vm1.raw" in c for c in cmds))
check("copy_vm_image: disk_format='raw' never does a plain cp of the source image",
      not any("cp /iso/source.qcow2" in c for c in cmds))
check("copy_vm_image: disk_format='raw' resizes with -f raw against the .raw path",
      any("qemu-img resize -f raw" in c and "vm1.raw" in c for c in cmds))
# GPT backup header/table repair: growing a raw GPT-partitioned disk with
# qemu-img resize leaves the backup GPT structures at the old end of the
# disk instead of the new one — confirmed live 2026-09-01 as the likely
# cause of a lab-host VM's root filesystem appearing to reset to its
# pristine first-boot Btrfs snapshot after a reboot (dmesg: "GPT: Use GNU
# Parted to correct GPT errors."). sgdisk -e moves them back to the end.
check("copy_vm_image: disk_format='raw' repairs the GPT backup header/table after resize",
      any("sgdisk -e" in c and "vm1.raw" in c for c in cmds))


# ── Bug 3: delete_vm must not undefine the domain before removing storage ───
# (found live, twice: a bare `undefine --nvram` succeeded regardless of
# whether the domain was running, removing its definition before the real
# `undefine --nvram --remove-all-storage` call ever ran — which then failed
# with "domain not found", leaving the qcow2 file on disk. Fixed by dropping
# the redundant first undefine: `destroy` alone guarantees a stopped domain
# without touching its definition, so the one remaining undefine call always
# finds it and removes storage too.)
virsh_calls = []


def _fake_virsh_run(args, **kwargs):
    virsh_calls.append(args)
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_virsh_run
backend.delete_vm("vm1")
check("delete_vm: issues exactly 2 virsh calls (destroy, then undefine+remove-all-storage)",
      len(virsh_calls) == 2)
check("delete_vm: never calls a bare 'undefine' without --remove-all-storage",
      not any("undefine" in a and "--remove-all-storage" not in a for a in virsh_calls))
check("delete_vm: destroy runs before the storage-removing undefine",
      "destroy" in virsh_calls[0] and "undefine" in virsh_calls[1])
check("delete_vm: the undefine call includes --remove-all-storage",
      "--remove-all-storage" in virsh_calls[1])


# ── Bug 4: reboot_vm must prefer a direct guest reboot over virsh/ACPI ──────
# (found live, on a fresh Uyuni server install: virsh's ACPI-triggered
# `reboot` routinely fails to produce a lifecycle event within 120s in this
# nested-virt environment. The ORIGINAL fallback, an immediate hard `reset`,
# is confirmed live — reproduced TWICE on two separate disposable VMs, even
# with a guest-side `sync` added first — to silently lose a just-installed
# transactional-update snapshot: `transactional-update pkg install` returns
# and correctly marks the new snapshot as default, but a `reset` (the
# hardware reset line, not a guest/qemu-mediated shutdown) can still boot
# back into the OLD snapshot. A first fix escalated through ACPI `reboot`
# then ACPI `shutdown` before falling back to `reset` — confirmed live this
# never loses a snapshot, but ACPI signals routinely never reach the guest
# in this environment either, so it still fell through to `reset` most of
# the time. What actually works, confirmed live: a plain `ssh vm "reboot"`
# — bypassing ACPI-signal-forwarding through qemu entirely — completed in
# ~15s on a VM where the ACPI path had just failed twice in a row. Fixed:
# reboot via SSH directly whenever the guest is reachable; only fall back
# to the virsh ACPI/reset escalation when it isn't.)
sync_ssh_calls = []


def _fake_ssh_run(hostname, cmd, **kwargs):
    sync_ssh_calls.append((hostname, cmd, kwargs))
    return FakeResult(returncode=0)


class _FakeSocket:
    def close(self):
        pass


# Happy path: the guest is reachable over SSH -> reboots directly, never
# touches virsh at all.
virsh_calls.clear()
sync_ssh_calls.clear()
backends.ssh_run = _fake_ssh_run
backends.subprocess.run = _fake_virsh_run
backends.socket.create_connection = lambda addr, timeout=None: _FakeSocket()
backend.reboot_vm("vm1")
check("reboot_vm: reboots directly over SSH when the guest is reachable",
      any(h == "vm1" and c == "sync && reboot" for h, c, kw in sync_ssh_calls))
check("reboot_vm: the SSH reboot command is check=False (a dropped connection is expected)",
      all(kw.get("check") is False for h, c, kw in sync_ssh_calls))
check("reboot_vm: never touches virsh when the guest is reachable over SSH",
      len(virsh_calls) == 0)

# The guest is NOT reachable over SSH -> falls back to virsh's ACPI reboot,
# which succeeds this time -> nothing further needed.
virsh_calls.clear()
sync_ssh_calls.clear()


def _unreachable(addr, timeout=None):
    raise OSError("connection refused")


backends.socket.create_connection = _unreachable
backends.subprocess.run = _fake_virsh_run
backend.reboot_vm("vm1")
check("reboot_vm: falls back to virsh when the guest isn't reachable over SSH",
      len(sync_ssh_calls) == 0 and any("reboot" in a for a in virsh_calls))
check("reboot_vm: unreachable-guest happy path never calls shutdown or reset",
      not any("shutdown" in a or "reset" in a for a in virsh_calls))

# Unreachable guest, AND the ACPI reboot's event never arrives, but the
# escalated ACPI shutdown's event DOES -> starts the domain back up, never
# touches `reset`.
virsh_calls.clear()


def _fake_virsh_reboot_fails_shutdown_works(args, **kwargs):
    virsh_calls.append(args)
    if "event" in args:
        # First event call is for the reboot; second is for the shutdown.
        is_first = sum(1 for a in virsh_calls if "event" in a) == 1
        return FakeResult(returncode=1 if is_first else 0)
    if "domstate" in args:
        return FakeResult(returncode=0, stdout="shut off\n")
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_virsh_reboot_fails_shutdown_works
backend.reboot_vm("vm1")
check("reboot_vm: escalates to a graceful shutdown+start cycle, not reset",
      any("shutdown" in a for a in virsh_calls) and any("start" in a for a in virsh_calls)
      and not any("reset" in a for a in virsh_calls))

# Unreachable guest, and neither the reboot's nor the shutdown's lifecycle
# event arrives, and domstate never reports "shut off" either -> only THEN
# falls back to a hard reset, as a genuine last resort.
virsh_calls.clear()


def _fake_virsh_run_no_event(args, **kwargs):
    virsh_calls.append(args)
    if "event" in args:
        return FakeResult(returncode=1)
    if "domstate" in args:
        return FakeResult(returncode=0, stdout="running\n")
    return FakeResult(returncode=0)


backends.subprocess.run = _fake_virsh_run_no_event
backend.reboot_vm("vm1")
check("reboot_vm: still falls back to a hard reset when nothing graceful works",
      any("reset" in a for a in virsh_calls))
check("reboot_vm: tries shutdown before giving up and resetting",
      virsh_calls.index(next(a for a in virsh_calls if "shutdown" in a))
      < virsh_calls.index(next(a for a in virsh_calls if "reset" in a)))

backends.socket.create_connection = socket.create_connection


# ── Bug 5: mgradm install's pg_hba/IPv6 race must be pre-empted, not ────────
# recovered from after the fact
# (found live on cutoveruyuni2.mydemo.lab, 2026-08-28: the original recovery
# only patched pg_hba and did `systemctl restart uyuni-server` AFTER
# `mgradm install` had already died — but `mgradm install` itself performs
# schema/org/admin bootstrap, so a plain restart just brings the Tomcat
# process back up against a completely empty database (confirmed directly:
# spacecmd login "Invalid credentials", zero rows in web_contact, zero
# tables at all). And `mgradm install` refuses to simply be re-run once its
# containers/volumes exist ("Server is already initialized!"). Fixed by
# running install in the background and patching pg_hba the moment uyuni-db
# is ready, before uyuni-server's first connection attempt — so the ONE
# install command completes end-to-end.)
install_uyuni.time.sleep = lambda *a, **kw: None

fake = FakeSSH(responses=[("pg_isready", FakeResult(returncode=0)),
                          ("test -f", FakeResult(returncode=0)),
                          ("cat ", FakeResult(returncode=0, stdout="0\n"))])
install_uyuni.ssh_run = fake
install_uyuni._run_install_with_pg_hba_guard("host1", "mgradm install podman --admin-login admin")
launch_calls = [c for h, c, kw in fake.calls if "nohup" in c]
check("_run_install_with_pg_hba_guard: launches mgradm install in the background",
      len(launch_calls) == 1 and "mgradm install podman" in launch_calls[0])
hba_calls = [c for h, c, kw in fake.calls if "pg_hba_custom.conf" in c]
check("_run_install_with_pg_hba_guard: patches pg_hba once uyuni-db is ready",
      len(hba_calls) == 1)
pg_isready_idx = next(i for i, (h, c, kw) in enumerate(fake.calls) if "pg_isready" in c)
hba_idx = next(i for i, (h, c, kw) in enumerate(fake.calls) if "pg_hba_custom.conf" in c)
check("_run_install_with_pg_hba_guard: pg_hba patch happens after uyuni-db is confirmed ready",
      hba_idx > pg_isready_idx)

# Timeout: the rc-file marker never appears -> die(), not a silent return.
fake = FakeSSH(responses=[("pg_isready", FakeResult(returncode=0)),
                          ("test -f", FakeResult(returncode=1))])
install_uyuni.ssh_run = fake
try:
    install_uyuni._run_install_with_pg_hba_guard("host1", "mgradm install podman", timeout=1, poll_interval=1)
    died = False
except SystemExit:
    died = True
check("_run_install_with_pg_hba_guard: dies if the install never finishes within the timeout", died)

# The install command itself reports a non-zero exit in its rc file -> die().
fake = FakeSSH(responses=[("pg_isready", FakeResult(returncode=0)),
                          ("test -f", FakeResult(returncode=0)),
                          ("cat ", FakeResult(returncode=0, stdout="1\n"))])
install_uyuni.ssh_run = fake
try:
    install_uyuni._run_install_with_pg_hba_guard("host1", "mgradm install podman", timeout=5, poll_interval=1)
    died = False
except SystemExit:
    died = True
check("_run_install_with_pg_hba_guard: dies if mgradm install's own exit code is non-zero", died)


# ── Bug 6: CLM stuck-build restart-and-retry wrapper ─────────────────────────
# (Round 4 of the CLM stuck-build investigation, 2026-08-28 — see
# MIGRATION_TODO.md, "the Web UI 'Build' button theory, tested and
# disproven": Uyuni's own async CLM align worker can get itself wedged
# after a small, non-deterministic number of builds; the only confirmed
# mitigation is restarting uyuni-server.service and re-triggering the
# same build/promote action. _wait_for_clm_with_restart_retry wraps
# sc.wait_for_content_environment with exactly that recovery, and
# run_clm_actions/_trigger_clm_action carry the validation + dispatch
# that used to live in spacecmd_common.run_content_lifecycle_actions
# before this wrapper needed to intercept it.)

# _trigger_clm_action: build vs promote dispatch, and promote's own
# 'from_env' validation.
build_calls = []
promote_calls = []
install_uyuni.sc.build_content_project = lambda h, e, p, msg: build_calls.append((h, e, p, msg))
install_uyuni.sc.promote_content_project = lambda h, e, p, frm: promote_calls.append((h, e, p, frm))
install_uyuni._trigger_clm_action("host1", "mgrctl exec --", "proj", "build", {"message": "m"})
check("_trigger_clm_action: build dispatches to sc.build_content_project with the message",
      build_calls == [("host1", "mgrctl exec --", "proj", "m")])

install_uyuni._trigger_clm_action("host1", "mgrctl exec --", "proj", "promote", {"from_env": "dev"})
check("_trigger_clm_action: promote dispatches to sc.promote_content_project with from_env",
      promote_calls == [("host1", "mgrctl exec --", "proj", "dev")])

died = False
try:
    install_uyuni._trigger_clm_action("host1", "mgrctl exec --", "proj", "promote", {})
except SystemExit:
    died = True
check("_trigger_clm_action: promote without 'from_env' dies", died)

# _wait_for_clm_with_restart_retry — happy path: reaches a terminal status
# on the very first (short) poll, never restarts, never re-triggers.
calls = {"wait": [], "trigger": [], "restart": []}


def _fake_wait_ok(hostname, exec_prefix, project, env, timeout=None, die_on_timeout=None, **kw):
    calls["wait"].append((timeout, die_on_timeout))
    return "built"


install_uyuni.sc.wait_for_content_environment = _fake_wait_ok
install_uyuni.sc.build_content_project = lambda *a, **kw: calls["trigger"].append(a)
install_uyuni.ssh_run = lambda *a, **kw: calls["restart"].append(a)
status = install_uyuni._wait_for_clm_with_restart_retry(
    "host1", "mgrctl exec --", "proj", "build", {}, "dev", timeout=1800, stall_timeout=300)
check("_wait_for_clm_with_restart_retry: happy path returns immediately, no restart",
      status == "built" and len(calls["wait"]) == 1 and len(calls["restart"]) == 0)
check("_wait_for_clm_with_restart_retry: first poll uses the short stall_timeout, not the full timeout, "
      "and does not ask wait_for_content_environment to die on timeout",
      calls["wait"][0] == (300, False))

# Stuck once, then recovers after a restart + retry of the same action.
calls = {"wait": [], "trigger": [], "restart": []}


def _fake_wait_stuck_then_ok(hostname, exec_prefix, project, env, timeout=None, die_on_timeout=None, **kw):
    calls["wait"].append((timeout, die_on_timeout))
    return "built" if len(calls["wait"]) > 1 else "building"


install_uyuni.sc.wait_for_content_environment = _fake_wait_stuck_then_ok
install_uyuni.sc.build_content_project = lambda *a, **kw: calls["trigger"].append(a)
install_uyuni.ssh_run = lambda *a, **kw: calls["restart"].append(a)
install_uyuni.time.sleep = lambda s: None
install_uyuni._ensure_server_container_active = lambda hostname, **kw: None
status = install_uyuni._wait_for_clm_with_restart_retry(
    "host1", "mgrctl exec --", "proj", "build", {"message": "m"}, "dev", timeout=1800, stall_timeout=300)
check("_wait_for_clm_with_restart_retry: recovers after one restart+retry", status == "built")
check("_wait_for_clm_with_restart_retry: polls exactly twice (initial + one retry)", len(calls["wait"]) == 2)
check("_wait_for_clm_with_restart_retry: restarts uyuni-server.service before retrying",
      any("systemctl restart uyuni-server.service" in str(c) for c in calls["restart"]))
check("_wait_for_clm_with_restart_retry: re-triggers the same action (build) after restart",
      len(calls["trigger"]) == 1)
check("_wait_for_clm_with_restart_retry: only the LAST poll asks wait_for_content_environment to die on timeout",
      calls["wait"][0][1] is False and calls["wait"][1][1] is True)

# Never recovers -> the final poll's own die_on_timeout=True is what
# actually raises (simulated here, since the real wait_for_content_environment
# implementation's own polling loop isn't what's under test in this file).
calls = {"wait": [], "trigger": [], "restart": []}


def _fake_wait_always_stuck(hostname, exec_prefix, project, env, timeout=None, die_on_timeout=None, **kw):
    calls["wait"].append((timeout, die_on_timeout))
    if die_on_timeout:
        raise SystemExit(1)
    return "building"


install_uyuni.sc.wait_for_content_environment = _fake_wait_always_stuck
install_uyuni.sc.build_content_project = lambda *a, **kw: calls["trigger"].append(a)
install_uyuni.ssh_run = lambda *a, **kw: calls["restart"].append(a)
died = False
try:
    install_uyuni._wait_for_clm_with_restart_retry(
        "host1", "mgrctl exec --", "proj", "build", {}, "dev", timeout=1800, stall_timeout=300)
except SystemExit:
    died = True
check("_wait_for_clm_with_restart_retry: dies for real if the retry doesn't recover either",
      died and len(calls["wait"]) == 2)

# run_clm_actions: no-op, validation, and full orchestration (trigger + wait).
install_uyuni.sc.ensure_spacecmd_config = lambda *a, **kw: None
calls = {"trigger": []}
install_uyuni.sc.build_content_project = lambda h, e, p, msg: calls["trigger"].append(("build", p))
install_uyuni.sc.promote_content_project = lambda h, e, p, frm: calls["trigger"].append(("promote", p, frm))
install_uyuni.run_clm_actions("host1", {})
check("run_clm_actions: no-op when uyuni_content_lifecycle_actions is unset", calls["trigger"] == [])

died = False
try:
    install_uyuni.run_clm_actions("host1", {"uyuni_content_lifecycle_actions": [{"project": "p", "action": "bogus"}]})
except SystemExit:
    died = True
check("run_clm_actions: invalid action dies", died)

died = False
try:
    install_uyuni.run_clm_actions(
        "host1", {"uyuni_content_lifecycle_actions": [{"project": "proj", "action": "build", "wait": True}]})
except SystemExit:
    died = True
check("run_clm_actions: 'wait' without 'wait_env' dies", died)

calls = {"trigger": []}
install_uyuni.sc.wait_for_content_environment = lambda *a, **kw: "built"
install_uyuni.run_clm_actions(
    "host1", {"uyuni_content_lifecycle_actions": [
        {"project": "proj", "action": "build", "message": "m"},
        {"project": "proj", "action": "promote", "from_env": "dev"},
    ]})
check("run_clm_actions: runs build then promote in order",
      calls["trigger"] == [("build", "proj"), ("promote", "proj", "dev")])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all live-bugfix regression checks passed")
