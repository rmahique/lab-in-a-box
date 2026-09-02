#!/usr/bin/env python3
# Mocked-subprocess unit tests for libs/lab_creation.py —
# no live KVM host is available in this project. Covers: ssh_run/ssh_output
# command shape, the multi-KVM-host resolve/locate/select logic (new in the
# python port — bash only ever had one hypervisor), load_vm_vars merge
# order, and validate_lab_definition's preflight checks (with subprocess.run
# mocked so this runs in a container with no virsh/ping/jq installed). Run
# from 10_lab_creation_core.sh, in its own container — see tests/run_tests.sh.
import json
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation as lc  # noqa: E402
import primary  # noqa: E402
import targets  # noqa: E402

# Captured before any test below monkeypatches lc.subprocess.run to a fake —
# process_template() (used by the prepare_cloud_init() tests near the end of
# this file) needs the REAL subprocess.run to actually shell out to bash.
_real_subprocess_run = lc.subprocess.run

# This test container has no local virsh/virt-install (see the file
# header) — force run_libvirt_tool()'s local branch regardless, so the
# mocked lc.subprocess.run below is what actually gets called (matching
# the real automation VM host, which always has these tools locally) —
# rather than silently falling through to the SSH-fallback branch, whose
# argv shape ("ssh", ..., "virsh --connect qemu:///system ...") these
# fakes don't recognize.
lc._has_local_binary = lambda binary: True

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Drop-in for subprocess.run: records every call, returns a scripted
    result keyed by a substring match against the argv joined with spaces."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or []  # list of (substring, FakeCompleted)

    def __call__(self, args, **kwargs):
        cmd = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
        self.calls.append((cmd, kwargs))
        for substr, result in self.responses:
            if substr in cmd:
                return result
        return FakeCompleted()


# ── ssh_run: command shape + check=True error handling ──────────────────────
fake = FakeRun()
lc.subprocess.run = fake
lc.ssh_run("host1", "echo hi")
check("ssh_run: exactly one subprocess.run call", len(fake.calls) == 1)
cmd, kwargs = fake.calls[0]
check("ssh_run: uses accept-new host key checking", "StrictHostKeyChecking=accept-new" in cmd)
check("ssh_run: connects as root@<hostname>", "root@host1" in cmd)
check("ssh_run: command passed through verbatim", cmd.endswith("echo hi"))

fake = FakeRun(responses=[("false-cmd", FakeCompleted(returncode=1))])
lc.subprocess.run = fake
died = False
try:
    lc.ssh_run("host1", "false-cmd")
except RuntimeError:
    died = True
check("ssh_run: check=True raises RuntimeError on non-zero exit", died)

fake = FakeRun(responses=[("false-cmd", FakeCompleted(returncode=1))])
lc.subprocess.run = fake
result = lc.ssh_run("host1", "false-cmd", check=False)
check("ssh_run: check=False returns the result instead of raising", result.returncode == 1)

fake = FakeRun(responses=[("cat /etc/hostname", FakeCompleted(stdout="  myhost\n"))])
lc.subprocess.run = fake
check("ssh_output: strips stdout", lc.ssh_output("host1", "cat /etc/hostname") == "myhost")


# ── ensure_lab_ssh_key / distribute_lab_ssh_key ──────────────────────────────
# One keypair for the whole lab, generated and kept ON the lab-host VM
# itself (never locally, never per-VM), then pushed to every nested VM so
# the lab-host VM can reach any of them.
_lab_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexamplekeycontent lab-in-a-box"

# Key doesn't exist yet -> generated, then read back.
fake = FakeRun(responses=[
    ("test -f", FakeCompleted(returncode=1)),
    ("ssh-keygen", FakeCompleted(returncode=0)),
    ("cat", FakeCompleted(stdout=_lab_pubkey + "\n")),
])
lc.subprocess.run = fake
pubkey = lc.ensure_lab_ssh_key("10.0.0.5")
check("ensure_lab_ssh_key: returns the generated public key, stripped",
      pubkey == _lab_pubkey)
keygen_calls = [c for c, _ in fake.calls if "ssh-keygen" in c]
check("ensure_lab_ssh_key: generates a new key when none exists",
      len(keygen_calls) == 1)
check("ensure_lab_ssh_key: generates ed25519, no passphrase, at the fixed default path",
      "-t ed25519" in keygen_calls[0] and "-N ''" in keygen_calls[0]
      and "/root/.ssh/id_lab_ed25519" in keygen_calls[0])

# Key already exists -> reused, no ssh-keygen call at all.
fake = FakeRun(responses=[
    ("test -f", FakeCompleted(returncode=0)),
    ("cat", FakeCompleted(stdout=_lab_pubkey + "\n")),
])
lc.subprocess.run = fake
pubkey = lc.ensure_lab_ssh_key("10.0.0.5")
check("ensure_lab_ssh_key: reuses an existing key rather than regenerating",
      pubkey == _lab_pubkey and not any("ssh-keygen" in c for c, _ in fake.calls))

# distribute_lab_ssh_key: one two-hop SSH per target, run FROM the lab-host
# VM (the orchestrator itself has no direct route into the nested VMs'
# internal NAT range) — never embeds the pubkey directly in a shell string,
# routes it through stdin at every hop instead.
fake = FakeRun()
lc.subprocess.run = fake
lc.distribute_lab_ssh_key("10.0.0.5", _lab_pubkey, ["192.168.150.10", "192.168.150.11"])
check("distribute_lab_ssh_key: exactly one SSH call per target VM",
      len(fake.calls) == 2)
for cmd, kwargs in fake.calls:
    check("distribute_lab_ssh_key: call goes to the lab-host VM, not the target directly",
          "root@10.0.0.5" in cmd)
    check("distribute_lab_ssh_key: the inner hop targets the nested VM's own root@",
          "root@192.168.150.1" in cmd)
    check("distribute_lab_ssh_key: never embeds the pubkey literally in the command string",
          _lab_pubkey not in cmd)
    check("distribute_lab_ssh_key: passes the pubkey via stdin instead",
          kwargs.get("input") == _lab_pubkey)
    check("distribute_lab_ssh_key: idempotent (checks for the existing line before appending)",
          "grep -qxF" in cmd)
    check("distribute_lab_ssh_key: POSIX-sh compatible, no bash-only here-strings",
          "<<<" not in cmd)


# ── total_lab_resources: pure function, no I/O ───────────────────────────────
res_def = {
    "common": {"VM_CPU": "2", "VM_MEM": "4096", "VM_DSK": "40"},
    "nodes": {
        "vm1": {},  # falls back to common's values entirely
        "vm2": {"VM_CPU": "4", "VM_MEM": "8192", "VM_DSK": "80"},  # per-node override wins
    },
}
check("total_lab_resources: per-node values override common, common fills in the rest, and it sums both nodes",
      lc.total_lab_resources(res_def) == (2 + 4, 4096 + 8192, 40 + 80))

check("total_lab_resources: an empty lab (no nodes) totals to zero",
      lc.total_lab_resources({"common": {}, "nodes": {}}) == (0, 0, 0))

check("total_lab_resources: a node with neither its own value nor a common default contributes 0",
      lc.total_lab_resources({"common": {}, "nodes": {"vm1": {"VM_CPU": "2"}}}) == (2, 0, 0))


# ── resolve_kvm_host / locate_kvm_host / select_kvm_host ─────────────────────
# Single-host config (today's default): no probing, no selection logic runs.
definition = {"nodes": {"vm1": {}}, "common": {}}
config = {"REMOTE_HOST": "hv1", "VIRT_SRV": "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa"}
probed = []
lc._host_resources = lambda host, vm_img_loc: probed.append(host) or (99, 99999, 99999)
host, virt_srv = lc.resolve_kvm_host(definition, "vm1", config)
check("resolve_kvm_host: single configured host used directly, no probing", host == "hv1" and not probed)
check("resolve_kvm_host: reuses the configured VIRT_SRV verbatim for the default host",
      virt_srv == config["VIRT_SRV"])

# Multi-host: explicit per-node override wins over selection.
definition2 = {"nodes": {"vm1": {"kvm_host": "hv3"}}, "common": {}}
config2 = {"REMOTE_HOST": "hv1", "KVM_HOSTS": "hv1 hv2 hv3"}
host, virt_srv = lc.resolve_kvm_host(definition2, "vm1", config2)
check("resolve_kvm_host: explicit nodes.kvm_host overrides selection", host == "hv3")
check("resolve_kvm_host: derives a fresh qemu+ssh URI for a non-default host",
      virt_srv == "qemu+ssh://root@hv3/system?keyfile=.ssh/id_rsa")

# Multi-host: resource-based selection picks the host with the most free
# memory among those that qualify.
definition3 = {"nodes": {"vm1": {"VM_CPU": 4, "VM_MEM": 4096, "VM_DSK": 20}}, "common": {}}
config3 = {"REMOTE_HOST": "hv1", "KVM_HOSTS": "hv1 hv2"}
resources = {"hv1": (2, 2048, 2048), "hv2": (8, 16384, 40960)}  # hv1 too small, hv2 qualifies
lc._host_resources = lambda host, vm_img_loc: resources[host]
host, virt_srv = lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
check("resolve_kvm_host: selects the only qualifying host under resource pressure", host == "hv2")

resources2 = {"hv1": (8, 8192, 40960), "hv2": (8, 16384, 40960)}  # both qualify, hv2 has more RAM
lc._host_resources = lambda host, vm_img_loc: resources2[host]
host, virt_srv = lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
check("resolve_kvm_host: picks the host with the most free memory among qualifiers", host == "hv2")

resources3 = {"hv1": (1, 512, 512), "hv2": (1, 512, 512)}  # neither qualifies
lc._host_resources = lambda host, vm_img_loc: resources3[host]
died = False
try:
    lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
except SystemExit:
    died = True
check("resolve_kvm_host: dies when no configured host has enough resources", died)

# A host that fails to answer is disqualified, not fatal, if another qualifies.
def _flaky_resources(host, vm_img_loc):
    if host == "hv1":
        raise RuntimeError("ssh timeout")
    return (8, 16384, 40960)


lc._host_resources = _flaky_resources
host, virt_srv = lc.resolve_kvm_host(definition3, "vm1", config3, vm_img_loc="/var/lib/libvirt/images/")
check("resolve_kvm_host: an unreachable candidate host is skipped, not fatal", host == "hv2")

# locate_kvm_host: probes each configured host's virsh dominfo for an
# existing VM and returns the first one that has it.
fake = FakeRun(responses=[
    ("--connect qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa dominfo vm1", FakeCompleted(returncode=1)),
    ("--connect qemu+ssh://root@hv2/system?keyfile=.ssh/id_rsa dominfo vm1", FakeCompleted(returncode=0)),
])
lc.subprocess.run = fake
config4 = {"REMOTE_HOST": "hv1", "KVM_HOSTS": "hv1 hv2"}
host, virt_srv = lc.locate_kvm_host(definition2, "vm1", config4) if False else (None, None)
# (definition2 has an explicit kvm_host override which would short-circuit
# probing; use a definition with no override to actually exercise probing)
definition4 = {"nodes": {"vm1": {}}, "common": {}}
host, virt_srv = lc.locate_kvm_host(definition4, "vm1", config4)
check("locate_kvm_host: finds the VM on the second configured host", host == "hv2")

fake = FakeRun(responses=[])  # every dominfo call returns the default FakeCompleted(returncode=0)
lc.subprocess.run = fake
host, virt_srv = lc.locate_kvm_host(definition4, "vm1", config4)
check("locate_kvm_host: returns the first host that has the domain", host == "hv1")

fake = FakeRun(responses=[
    ("dominfo vm1", FakeCompleted(returncode=1)),
])
lc.subprocess.run = fake
died = False
try:
    lc.locate_kvm_host(definition4, "vm1", config4)
except SystemExit:
    died = True
check("locate_kvm_host: dies when no configured host has the domain", died)


# ── load_vm_vars: merge order + auto-detect fallbacks ────────────────────────
lc._detect_dns = lambda: "8.8.8.8"
lc._detect_gateway = lambda: "192.168.1.1"
lc._detect_domain = lambda: "mydemo.lab"
lc._detect_netmask = lambda: "24"
definition5 = {
    "common": {"VM_MEM": "4096", "mydns": ""},
    "nodes": {"vm1": {"myip": "192.168.1.50", "kcluster": "cluster1", "VM_MEM": "8192"}},
}
env = lc.load_vm_vars(definition5, "vm1")
check("load_vm_vars: per-node value overrides common", env["VM_MEM"] == "8192")
check("load_vm_vars: kcluster key is renamed to clu_name", env.get("clu_name") == "cluster1" and "kcluster" not in env)
check("load_vm_vars: auto-detects mydns when unset/empty", env["mydns"] == "8.8.8.8")
check("load_vm_vars: auto-detects mygw when absent", env["mygw"] == "192.168.1.1")
check("load_vm_vars: derives mynet_reverse from myip when present", "mynet_reverse" in env and env["mynet_reverse"])

definition6 = {"common": {}, "nodes": {"vm1": {}}}
env = lc.load_vm_vars(definition6, "vm1")
check("load_vm_vars: no mynet_reverse when myip is absent", "mynet_reverse" not in env)


# ── validate_lab_definition: preflight checks ────────────────────────────────
# subprocess.run is mocked throughout (no virsh/ping/jq in the test
# container); resolve_kvm_host is pinned to a single configured host so no
# multi-host probing runs during these checks.
lc._host_resources = lambda host, vm_img_loc: (99, 99999, 99999)
targets.check_ssh_only_reachability = lambda node, timeout=5: True


def _lab_def(data):
    """
    Writes `data` to a temp .json file and wraps it in a primary.LabDefinition
    pointed at that file — the same shape validate_lab_definition() actually
    receives in production (via primary.load_definition()). validate_lab_definition()
    no longer takes a separate path argument at all: it reads
    definition.source_path for its banner and for delegating to each addon's
    own `--validate` subprocess, so the fixture needs to carry that itself.
    """
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return primary.LabDefinition(data, f.name, "json")


base_common = {"ISO_IMAGE": "img.qcow2", "VM_MEM": "4096", "VM_DSK": "40", "VM_CPU": "2"}
single_host_cfg = {"REMOTE_HOST": "hv1", "VIRT_SRV": "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa"}

# All node fixtures below use config_method="virt_customize" to sidestep
# section 8's local ignition/combustion/cloud-init template-file checks,
# which are irrelevant to what this test is exercising (section 4-7's
# structural/hypervisor checks). "echo ok" is scripted to succeed (section
# 7's per-host SSH-reachability probe); every other subprocess.run call
# (virsh dominfo, ping, test -f) falls through to FakeRun's default
# returncode=0, which validate_lab_definition treats as WARN-only
# ("already exists" / "already responding" / image found) — never an ERROR
# — so it doesn't affect the pass/fail assertions below.
img_check_ok = FakeRun(responses=[("echo ok", FakeCompleted(returncode=0, stdout="ok"))])

# validate_lab_definition takes the already-loaded definition directly (read/
# parsed exactly once by the caller, via primary.load_definition) — it never
# re-reads/re-parses the file itself, and takes no separate path argument at
# all: a LabDefinition already knows its own source_path (used for the
# preflight banner and for delegating to each addon's own
# `install_<addon> --validate <path>` subprocess, section 5 below) — see the
# function's own docstring. Parsing (JSON/YAML format auto-detection,
# malformed-input handling) is primary.py's job and is covered in
# 11_primary_test.py instead, not re-tested here.

# Valid minimal definition.
lc.subprocess.run = img_check_ok
good = _lab_def({"common": dict(base_common),
                  "nodes": {"vm1": {"myip": "192.168.1.50", "mymac": "aa:bb:cc:dd:ee:01",
                                     "config_method": "virt_customize"}}})
check("validate_lab_definition: valid minimal definition passes",
      lc.validate_lab_definition(good, single_host_cfg, "/iso", "/lab") is True)

# Missing 'nodes'/'common'.
empty = _lab_def({})
check("validate_lab_definition: missing nodes/common fails",
      lc.validate_lab_definition(empty, single_host_cfg, "/iso", "/lab") is False)

# Duplicate IP across two nodes.
lc.subprocess.run = img_check_ok
dup_ip = _lab_def({
    "common": dict(base_common),
    "nodes": {
        "vm1": {"myip": "192.168.1.50", "config_method": "virt_customize"},
        "vm2": {"myip": "192.168.1.50", "config_method": "virt_customize"},
    },
})
check("validate_lab_definition: duplicate IP across nodes fails",
      lc.validate_lab_definition(dup_ip, single_host_cfg, "/iso", "/lab") is False)

# Node references an undefined kcluster.
bad_kcluster = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.50", "kcluster": "nope", "config_method": "virt_customize"}},
    "kclusters": {},
})
check("validate_lab_definition: undefined kcluster reference fails",
      lc.validate_lab_definition(bad_kcluster, single_host_cfg, "/iso", "/lab") is False)

# Invalid backend name.
bad_backend = _lab_def({"common": dict(base_common, backend="not-a-backend"),
                         "nodes": {"vm1": {"myip": "192.168.1.50", "config_method": "virt_customize"}}})
check("validate_lab_definition: invalid common.backend fails",
      lc.validate_lab_definition(bad_backend, single_host_cfg, "/iso", "/lab") is False)

# Existing node unreachable via SSH.
targets.check_ssh_only_reachability = lambda node, timeout=5: False
existing_unreachable = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.50", "existing": True, "config_method": "virt_customize"}},
})
check("validate_lab_definition: unreachable 'existing' node fails",
      lc.validate_lab_definition(existing_unreachable, single_host_cfg, "/iso", "/lab") is False)

# Existing node reachable: skips hypervisor/image checks entirely, passes.
targets.check_ssh_only_reachability = lambda node, timeout=5: True
existing_reachable = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.50", "existing": True, "config_method": "virt_customize"}},
})
check("validate_lab_definition: reachable 'existing' node passes",
      lc.validate_lab_definition(existing_reachable, single_host_cfg, "/iso", "/lab") is True)

# ── backend-aware ISO_IMAGE check ────────────────────────────────────────────
# Regression test for a real bug found live 2026-08-29: the ISO_IMAGE-exists-
# on-the-hypervisor check ran for every node regardless of backend, even
# though it's a purely libvirt concern — a Harvester-backed node (which
# resolves its image by name inside the cluster, never touching any KVM
# hypervisor's filesystem) failed preflight over an image that was never
# supposed to exist there at all.
img_check_missing = FakeRun(responses=[
    ("echo ok", FakeCompleted(returncode=0, stdout="ok")),
    ("test -f", FakeCompleted(returncode=1)),
])

lc.subprocess.run = img_check_missing
libvirt_missing_image = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.60", "config_method": "virt_customize"}},
})
check("validate_lab_definition: libvirt node with a missing ISO_IMAGE still fails (baseline)",
      lc.validate_lab_definition(libvirt_missing_image, single_host_cfg, "/iso", "/lab") is False)

lc.subprocess.run = img_check_missing
harvester_missing_image = _lab_def({
    "common": dict(base_common),
    "nodes": {"vm1": {"myip": "192.168.1.61", "backend": "harvester", "config_method": "virt_customize"}},
})
check("validate_lab_definition: harvester-backend node skips the libvirt-only ISO_IMAGE check",
      lc.validate_lab_definition(harvester_missing_image, single_host_cfg, "/iso", "/lab") is True)

lc.subprocess.run = img_check_missing
harvester_via_common = _lab_def({
    "common": dict(base_common, backend="harvester"),
    "nodes": {"vm1": {"myip": "192.168.1.62", "config_method": "virt_customize"}},
})
check("validate_lab_definition: common.backend=harvester (no per-node override) also skips the check",
      lc.validate_lab_definition(harvester_via_common, single_host_cfg, "/iso", "/lab") is True)


# ── common.ISO_IMAGE only required when a node lacks its own override ───────
# Regression test for a real bug reported live 2026-09-01: a lab where every
# node pins its own ISO_IMAGE never needs a common default at all, but
# validate_lab_definition() rejected it outright. config_method="virt_customize"
# on every node (same trick the harvester tests above use) keeps this test
# scoped to the ISO_IMAGE-required logic — it skips the separate ignition/
# combustion-template-existence checks that "" (the default config_method)
# would otherwise also trigger.
common_no_iso = dict(base_common)
common_no_iso["ISO_IMAGE"] = ""
# "echo ok" -> SSH reachability check for the image-exists-on-hypervisor
# check; everything else (the "test -f" image check itself) falls through
# to the default FakeCompleted(returncode=0) — succeeds.
lc.subprocess.run = FakeRun(responses=[("echo ok", FakeCompleted(returncode=0, stdout="ok"))])

lab_all_nodes_override_iso = _lab_def({
    "common": common_no_iso,
    "nodes": {
        "vm1": {"myip": "192.168.1.70", "ISO_IMAGE": "vm1.qcow2", "config_method": "virt_customize"},
        "vm2": {"myip": "192.168.1.71", "ISO_IMAGE": "vm2.qcow2", "config_method": "virt_customize"},
    },
})
check("validate_lab_definition: empty common.ISO_IMAGE passes when every node overrides it",
      lc.validate_lab_definition(lab_all_nodes_override_iso, single_host_cfg, "/iso", "/lab") is True)

lab_one_node_missing_iso = _lab_def({
    "common": common_no_iso,
    "nodes": {
        "vm1": {"myip": "192.168.1.72", "ISO_IMAGE": "vm1.qcow2", "config_method": "virt_customize"},
        "vm2": {"myip": "192.168.1.73", "config_method": "virt_customize"},
    },
})
check("validate_lab_definition: empty common.ISO_IMAGE still fails when a node has no override",
      lc.validate_lab_definition(lab_one_node_missing_iso, single_host_cfg, "/iso", "/lab") is False)


# ── config_method="" (Ignition+Combustion) warns for a non-Micro image ──────
# Regression test for a real bug reported live 2026-09-01: a node with
# config_method="" (the default) and a plain SLES/Leap "kvm-and-xen" image
# (not SLE Micro) never gets its static IP configured, since Ignition/
# Combustion is silently a no-op on a guest with no ignition support built
# in. This should warn (not error — a bad heuristic shouldn't block a
# deploy), and only for the true Ignition-default case.
import io as _io
from contextlib import redirect_stdout as _redirect_stdout

lc.subprocess.run = FakeRun(responses=[("echo ok", FakeCompleted(returncode=0, stdout="ok"))])

lab_ignition_non_micro = _lab_def({
    "common": dict(base_common, **{"ISO_IMAGE": "SLES15-SP6-Minimal-VM.x86_64-kvm-and-xen-GM.qcow2"}),
    "nodes": {"vm1": {"myip": "192.168.1.80"}},  # config_method omitted -> Ignition+Combustion
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    # Not asserting ok is True here: config_method="" (Ignition) also
    # requires real ignition/combustion template files to exist at
    # lab_setup_path, a separate, pre-existing check unrelated to this
    # fix — "/lab" (this test's fixture path) never has them, so this
    # particular lab genuinely fails preflight for that reason regardless.
    # What's under test is specifically that the mismatch gets flagged as
    # a WARNING (not folded into/blocked by that unrelated error).
    lc.validate_lab_definition(lab_ignition_non_micro, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: config_method='' + non-Micro image warns about the mismatch",
      "is likely unsupported on ISO_IMAGE" in buf.getvalue())
check("validate_lab_definition: the mismatch is reported as a WARNING, not an ERROR",
      "[WARN]" in buf.getvalue().split("is likely unsupported on ISO_IMAGE")[0].splitlines()[-1])

lab_ignition_micro = _lab_def({
    "common": dict(base_common, **{"ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2"}),
    "nodes": {"vm1": {"myip": "192.168.1.81"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_ignition_micro, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: config_method='' + a genuine SLE Micro image warns about nothing",
      "is likely unsupported on ISO_IMAGE" not in buf.getvalue())

# config_method="cloud-init" on the same "kvm-and-xen" Minimal-VM image is
# ALSO unsupported (that image family uses JeOS Firstboot, not cloud-init —
# see _IMAGE_CONFIG_METHOD_SUPPORT's sources) — confirmed live 2026-09-02
# against two real such images on nuc6 (neither has a cloud-init binary or
# systemd unit), and the actual root cause of an unreachable VM reported the
# same day. Unlike the config_method="" case above, switching to cloud-init
# does NOT fix this image family — only virt_customize does.
lab_cloudinit_non_micro = _lab_def({
    "common": dict(base_common, **{
        "ISO_IMAGE": "SLES15-SP6-Minimal-VM.x86_64-kvm-and-xen-GM.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.82"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_cloudinit_non_micro, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: cloud-init on a kvm-and-xen Minimal-VM image also warns (JeOS Firstboot, not cloud-init)",
      "is likely unsupported on ISO_IMAGE" in buf.getvalue())

# ... but an image this heuristic doesn't recognize at all is left alone,
# regardless of config_method — "if distribution is not in the list then
# just continue" (explicit design requirement, not an oversight).
lab_cloudinit_unknown_image = _lab_def({
    "common": dict(base_common, **{
        "ISO_IMAGE": "some-completely-unrecognized-image.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.83"}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_cloudinit_unknown_image, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an unrecognized ISO_IMAGE is never warned about",
      "is likely unsupported on ISO_IMAGE" not in buf.getvalue())


# ── config_method enum validation ────────────────────────────────────────────
# Regression test for a real bug reported live 2026-09-02: a lab.json with
# config_method="virt-customize" (a "virt_customize" typo, hyphen instead of
# underscore) matched none of create_vm()'s config_method branches, so it
# silently never called virt-install at all — no VM, no error, anywhere.
lab_bad_config_method = _lab_def({
    "common": base_common,
    "nodes": {"vm1": {"myip": "192.168.1.90", "config_method": "virt-customize"}},
})
ok = lc.validate_lab_definition(lab_bad_config_method, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an invalid config_method value fails preflight", ok is False)


# ── installer-ISO vs. config_method mismatch ─────────────────────────────────
# Regression test for a real bug reported live 2026-09-02: an ISO_IMAGE
# ending in ".iso" (a genuine installer medium, e.g. an Ubuntu live-server
# ISO) used with any config_method other than "install_iso" makes
# copy_vm_image() `cp` + `qemu-img resize` it as if it were an existing
# qcow2 disk — which fails hard ("Image is not in qcow2 format") regardless
# of distro. This must be an ERROR (a guaranteed crash, not a heuristic).
lab_iso_wrong_method = _lab_def({
    "common": base_common,
    "nodes": {"vm1": {
        "myip": "192.168.1.91", "config_method": "cloud-init",
        "ISO_IMAGE": "ubuntu-24.04-live-server-amd64.iso",
    }},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    ok = lc.validate_lab_definition(lab_iso_wrong_method, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an installer ISO with a non-install_iso config_method fails preflight",
      ok is False and "is an installer ISO but config_method is" in buf.getvalue())
check("validate_lab_definition: the installer-ISO mismatch doesn't also fire the softer "
      "compatibility warning (would just be redundant noise on top of the real error)",
      "is likely unsupported on ISO_IMAGE" not in buf.getvalue())

lab_iso_right_method = _lab_def({
    "common": base_common,
    "nodes": {"vm1": {
        "myip": "192.168.1.92", "config_method": "install_iso",
        "ISO_IMAGE": "ubuntu-24.04-live-server-amd64.iso",
    }},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_iso_right_method, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an installer ISO with config_method=install_iso is not flagged",
      "is an installer ISO but config_method is" not in buf.getvalue())


# ── an explicit per-node config_method="" overrides a non-empty common ──────
# Regression test for a real bug reported live 2026-09-02: a node's explicit
# "config_method": "" (CLAUDE.md's own documented way to select Ignition+
# Combustion) was indistinguishable from "key omitted" once run through
# _empty(), so it silently fell back to inheriting a non-empty
# common.config_method instead of actually applying "" — unlike the real
# runtime path (load_vm_vars(), a plain per-node-always-overwrites merge),
# which already got this right. Verified here via the image/method
# compatibility warning: an SL-Micro node explicitly opting back into
# Ignition+Combustion under a cloud-init-default common must NOT warn.
lab_explicit_empty_override = _lab_def({
    "common": dict(base_common, **{
        "ISO_IMAGE": "SL-Micro.x86_64-6.2-Default-qcow-GM.qcow2",
        "config_method": "cloud-init",
    }),
    "nodes": {"vm1": {"myip": "192.168.1.93", "config_method": ""}},
})
buf = _io.StringIO()
with _redirect_stdout(buf):
    lc.validate_lab_definition(lab_explicit_empty_override, single_host_cfg, "/iso", "/lab")
check("validate_lab_definition: an explicit per-node config_method=\"\" wins over a non-empty "
      "common.config_method, instead of silently inheriting it",
      "is likely unsupported on ISO_IMAGE" not in buf.getvalue())


# ── prepare_cloud_init(): network_renderer defaulting/override ──────────────
# Real bash-eval process_template() against the real shipped templates (not
# mocked) — a real substitution bug in template_network-config would still be
# caught here, matching 30_setup_harvester_cluster_test.py's own precedent.
# Restore the real subprocess.run first — several tests above this point
# leave lc.subprocess.run monkeypatched to a fake for their own purposes.
lc.subprocess.run = _real_subprocess_run


def _render_network_config(variables):
    # prepare_cloud_init() always reads /root/.ssh/id_rsa.pub (mirrors bash's own
    # local `ROOT_SSH_KEY=$(cat ~/.ssh/id_rsa.pub)` reassignment) — this
    # container has no such key yet; ensure one exists rather than mock the
    # read, since a real automation VM always has one from its own bootstrap.
    pubkey_path = Path("/root/.ssh/id_rsa.pub")
    pubkey_path.parent.mkdir(parents=True, exist_ok=True)
    if not pubkey_path.exists():
        pubkey_path.write_text("ssh-rsa AAAAtest test@test\n")
    with tempfile.TemporaryDirectory() as tmp:
        ci_dir = Path(tmp) / "cloud-init"
        ci_dir.mkdir()
        for kind in ("user-data", "network-config", "network-config-dhcp", "meta-data"):
            src = _REPO / "templates" / "cloud-init.template_{}".format(kind)
            (ci_dir / "template_{}".format(kind)).write_text(src.read_text())
        lc.prepare_cloud_init("vm1.mydemo.lab", tmp, variables)
        return (ci_dir / "vm1.mydemo.lab_network-config").read_text()

_base_vars = {
    "_vm_name": "vm1.mydemo.lab", "mymac": "52:54:00:aa:bb:cc", "myip": "192.168.1.50",
    "mymask": "24", "mygw": "192.168.1.1", "mydns": "192.168.1.1", "mydomain": "mydemo.lab",
    "ISO_IMAGE": "", "ROOT_PWD_HASH": "x",
}

rendered = _render_network_config(dict(_base_vars))
check("prepare_cloud_init defaults network_renderer to NetworkManager when the lab JSON omits it "
      "(SLE Micro/SLES/Leap's own default — unchanged behavior for every existing lab)",
      "renderer: NetworkManager" in rendered)

rendered = _render_network_config(dict(_base_vars, network_renderer="networkd"))
check("prepare_cloud_init honors an explicit network_renderer override (e.g. for an Ubuntu guest, "
      "whose distro defaults to systemd-networkd, not NetworkManager)",
      "renderer: networkd" in rendered)

rendered = _render_network_config(dict(_base_vars, network_renderer=""))
check("prepare_cloud_init treats an explicit empty-string network_renderer the same as omitted, "
      "not as a literal blank renderer line",
      "renderer: NetworkManager" in rendered)

# An empty/omitted myip (the USB-delivery lab-host VM's own case — its
# address is unknown at build time, unlike every other node this project
# creates) selects the DHCP template instead of static addressing.
dhcp_vars = dict(_base_vars)
dhcp_vars["myip"] = ""
rendered = _render_network_config(dhcp_vars)
check("prepare_cloud_init: empty myip selects the DHCP network-config template",
      "dhcp4: true" in rendered and "addresses:" not in rendered)

del dhcp_vars["myip"]
rendered = _render_network_config(dhcp_vars)
check("prepare_cloud_init: omitted myip (not just empty-string) also selects DHCP",
      "dhcp4: true" in rendered)

check("prepare_cloud_init: a real static myip still gets the static template, unchanged",
      "dhcp4: false" in _render_network_config(dict(_base_vars))
      and "addresses:" in _render_network_config(dict(_base_vars)))

# Regression test for a real bug reported live 2026-09-02: setup_vm.py (the
# only real caller) never puts "_vm_name" in the variables dict it passes —
# it only ever has vm_name as a separate local — so template_meta-data's
# "${_vm_name}" rendered empty for every cloud-init node's instance-id/
# local-hostname. _base_vars above sets "_vm_name" explicitly, which would
# have hidden this regression forever; this check omits it deliberately, to
# exercise prepare_cloud_init()'s own caller contract instead of the test
# fixture's.
vars_without_vm_name = dict(_base_vars)
del vars_without_vm_name["_vm_name"]
with tempfile.TemporaryDirectory() as tmp:
    ci_dir = Path(tmp) / "cloud-init"
    ci_dir.mkdir()
    for kind in ("user-data", "network-config", "network-config-dhcp", "meta-data"):
        src = _REPO / "templates" / "cloud-init.template_{}".format(kind)
        (ci_dir / "template_{}".format(kind)).write_text(src.read_text())
    lc.prepare_cloud_init("vm1.mydemo.lab", tmp, vars_without_vm_name)
    meta_data = (ci_dir / "vm1.mydemo.lab_meta-data").read_text()
check("prepare_cloud_init: instance-id/local-hostname are populated even when the caller "
      "doesn't pass _vm_name explicitly",
      "instance-id: vm1.mydemo.lab" in meta_data and "local-hostname: vm1.mydemo.lab" in meta_data)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab_creation core checks passed")
