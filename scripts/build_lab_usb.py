#!/usr/bin/env python3.11
# Part of lab-in-a-box — deliver a completed lab onto a USB stick, so it can
# be handed off and run standalone on other hardware.
#
# Design: create one ordinary VM (the "lab-host VM") via this project's
# existing VM-creation pipeline, bootstrap it with the SAME already-tested
# NAT-mode automation-VM flow this project already ships
# (setup_kvm_node.py + setup_lab_automation.sh, _network_mode=nat) — no new
# bootstrap logic at all — then run the lab's own setup_lab.py, unchanged,
# ON that nested automation VM against a copy of the lab definition whose
# node addresses have been remapped into the internal NAT range (in memory
# only — see libs/lab_usb.py). Shutting the lab-host VM down leaves its own
# disk (raw format, not this project's usual QCOW2 — see
# backends.LibvirtBackend.create_vm's disk_format param) as a complete,
# self-contained, bootable image of the whole lab.
#
# See /root/.claude/plans/wiggly-zooming-pretzel.md for the full design
# write-up and TODO (repo root) for the task breakdown this implements.
#
# Usage:
#   build_lab_usb.py <lab.json> [--build-only]
#
# --build-only stops once the lab-host VM's own raw disk is a complete,
# shut-down, ready-to-copy appliance image, and prints its path instead of
# writing it to a real USB device. This is the only mode a live test can
# meaningfully run in (there is no real USB hardware to test the final `dd`
# against) — real-device selection/confirmation/write/grow (TODO task 4)
# is not yet implemented; pass --build-only until it is.
__version__ = "__LABVERSION__"

import ipaddress
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import backends  # noqa: E402
import lab_usb  # noqa: E402
from lab_creation import (  # noqa: E402
    log, die, ssh_run, ssh_output, run_libvirt_tool, check_ssh_conn,
    prepare_cloud_init, copy_to_hypervisor,
    total_lab_resources, ensure_lab_ssh_key, distribute_lab_ssh_key,
    _generate_unused_mac,
)

# Registration-free base OS for the lab-host VM specifically — it's meant to
# be handed off and booted standalone on hardware nobody has SCC credentials
# for, unlike every other node this project creates (which default to SLE
# Micro). cloud-init is what makes the DHCP path clean (see
# templates/cloud-init.template_network-config-dhcp and
# lab_creation.prepare_cloud_init's dhcp-when-myip-is-empty branch).
# Regular openSUSE Leap (not "Micro") — confirmed live 2026-08-31: Leap
# Micro's transactional/immutable root breaks virt-customize's own
# internal logging (it writes to /tmp/builder.log inside the guest, which
# is genuinely read-only there even offline) AND ships an interactive
# jeos-firstboot wizard that blocks headless boot. Regular Leap has neither
# problem — an ordinary writable RPM-based root, no jeos-firstboot at all —
# and is a better fit anyway: the lab-host VM just needs to be a normal
# functional Linux host, not an immutable appliance in its own right.
#
# Switched from 15.6 to 16.0 2026-09-01: 15.6 hit an unresolved, intermittent
# bug where the lab-host VM's own root filesystem reverted to its pristine
# first-boot Btrfs snapshot after some reboot in the bootstrap flow (see
# TODO's "LIVE TEST STATUS" / project_usb_delivery_live_test_2026-09-01.md) —
# trying a different Leap release to see if it's 15.6-image-specific. Note
# the filename shape genuinely differs from 15.x: no "openSUSE-" prefix,
# "Cloud" build variant (not "kvm-and-xen") — confirmed against
# download.opensuse.org/distribution/leap/16.0/appliances/, not guessed.
_DEFAULT_LAB_HOST_ISO_IMAGE = "Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2"
_DEFAULT_LAB_HOST_OS_VARIANT = "opensuse16.0"

# CPU/RAM/disk headroom added on top of the lab's own totals (total_lab_resources)
# for the lab-host VM's own bare-OS overhead AND the nested automation VM it
# bootstraps (which needs real resources of its own — RKE2/K3s installs,
# webui, etc. all run there too) — NOT just the host OS by itself.
_CPU_OVERHEAD = 4
_MEM_OVERHEAD_MIB = 8192
_DISK_OVERHEAD_GIB = 80

# The nested automation VM's own static IP inside the internal NAT network —
# matches this session's own NAT+port-forwarding feature's convention (the
# automation VM gets a static IP under NAT mode, same as under bridge mode;
# only the network it's static WITHIN differs). Host 2 (host 1 is the NAT
# network's own gateway).
_NESTED_AUTOMATION_IP = "192.168.150.2"
_NAT_NETWORK_NAME = "labnat"
_NAT_NETWORK_CIDR = "192.168.150.0/24"


def _lab_host_name(definition):
    common = definition.get("common", {}) or {}
    domain = common.get("mydomain") or "mydemo.lab"
    return "labhost.{}".format(domain)


def create_lab_host_vm(definition, config, defaults, host_name):
    """
    Creates the lab-host VM itself via this project's existing create_vm —
    nothing special (per the user's own framing): DHCP (myip omitted),
    disk_format="raw" (so its own disk can be dd'd onto a USB stick
    afterward — see create_vm's docstring), sized to hold the whole lab
    plus headroom for its own OS and the nested automation VM.

    Returns (backend, remote_host, virt_srv) — the caller needs these to
    keep talking to the OUTER hypervisor (to discover the lab-host VM's
    DHCP-assigned IP, and later to shut it down / locate its disk file).
    """
    total_cpu, total_mem, total_disk = total_lab_resources(definition)
    vm_cpu = total_cpu + _CPU_OVERHEAD
    vm_mem = total_mem + _MEM_OVERHEAD_MIB
    vm_dsk = total_disk + _DISK_OVERHEAD_GIB
    log("Lab-host VM sized at {} vCPU, {} MiB RAM, {} GiB disk "
        "(lab totals {}/{}/{} + overhead {}/{}/{})".format(
            vm_cpu, vm_mem, vm_dsk, total_cpu, total_mem, total_disk,
            _CPU_OVERHEAD, _MEM_OVERHEAD_MIB, _DISK_OVERHEAD_GIB))

    remote_host = config.get("REMOTE_HOST", "")
    virt_srv = config.get("VIRT_SRV", "qemu:///system")
    vm_img_loc = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")
    iso_loc = defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")

    backend = backends.LibvirtBackend(
        virt_srv, remote_host=remote_host, vm_img_loc=vm_img_loc,
        iso_loc=iso_loc, lab_setup_path=lab_setup_path,
    )

    _, mac_by_domain = backend.list_used_macs()
    mymac = _generate_unused_mac(set(mac_by_domain.values()))
    network = "{},mac.address={}".format(config.get("NETWORK", "bridge=br0"), mymac)
    log("- Lab-host VM '{}' will use MAC {}".format(host_name, mymac))

    backend.copy_vm_image(_DEFAULT_LAB_HOST_ISO_IMAGE, host_name, str(vm_dsk),
                           config_method="cloud-init", disk_format="raw")

    env = dict(defaults)
    env.update(config)
    env["myip"] = ""  # DHCP — see prepare_cloud_init's dhcp-when-empty-myip branch
    env["mymac"] = mymac
    env["mydomain"] = (definition.get("common", {}) or {}).get("mydomain") or "mydemo.lab"
    prepare_cloud_init(host_name, lab_setup_path, env)
    copy_to_hypervisor(remote_host, lab_setup_path, host_name, config_method="cloud-init", vm_img_loc=vm_img_loc)

    backend.create_vm(
        host_name, str(vm_cpu), str(vm_mem), str(vm_dsk), network,
        os_variant=_DEFAULT_LAB_HOST_OS_VARIANT, boot="uefi", config_method="cloud-init",
        disk_format="raw",
    )
    return backend, remote_host, virt_srv, mymac


def discover_lab_host_ip(host_name, remote_host, virt_srv, mymac, bridge, retry_limit=30, retry_interval=10):
    """
    The lab-host VM gets a DHCP lease, not a known-in-advance static IP —
    unlike every other node this project creates. Primary mechanism:
    `virsh domifaddr --source agent` — confirmed live 2026-08-31 that
    qemu-guest-agent IS pre-installed and running by default on the
    regular openSUSE Leap 15.6 "Cloud" image this VM uses (unlike the
    openSUSE Leap MICRO image tried first, which has neither
    qemu-guest-agent nor a writable /tmp for virt-customize — see
    _DEFAULT_LAB_HOST_ISO_IMAGE's own comment for why Leap Micro was
    dropped entirely, not just worked around).

    Fallback if the agent never responds (e.g. a different base image is
    configured later without checking this): the hypervisor's own ARP/
    neighbor table, prodded with a broadcast ping — confirmed live this
    is NOT reliable by itself (many modern guests ignore broadcast ICMP by
    default; DHCP negotiation traffic alone did not reliably populate the
    hypervisor's neighbor cache for the lease to show up there either), so
    it's a best-effort fallback, not the primary mechanism.
    """
    log("Waiting for the lab-host VM's DHCP-assigned IP (via the QEMU guest agent)…")
    for _ in range(retry_limit):
        result = run_libvirt_tool(
            "virsh", remote_host, virt_srv,
            ["domifaddr", host_name, "--source", "agent"],
            capture_output=True, text=True, check=False,
        )
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[-2] == "ipv4" and "/" in parts[-1]:
                ip = parts[-1].split("/")[0]
                if not ip.startswith("127."):
                    return ip
        time.sleep(retry_interval)

    log("Guest agent never responded — falling back to the hypervisor's own ARP table")
    import ipaddress as _ipaddress
    addr_out = ssh_output(remote_host, "ip -4 -o addr show dev {} | awk '{{print $4}}'".format(bridge))
    if not addr_out:
        die("could not determine {}'s own IPv4 address/CIDR on {}".format(bridge, remote_host))
    broadcast = str(_ipaddress.ip_interface(addr_out.splitlines()[0]).network.broadcast_address)
    for _ in range(retry_limit):
        ssh_run(remote_host, "ping -b -c 2 -w 2 {} >/dev/null 2>&1; true".format(broadcast), check=False)
        result = ssh_run(remote_host, "ip neigh show", capture=True, check=False)
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[4].lower() == mymac.lower():
                return parts[0]
        time.sleep(retry_interval)
    die("Timed out waiting for the lab-host VM's DHCP-assigned IP via both the guest agent "
        "and {}'s ARP table (MAC {})".format(remote_host, mymac))


def bootstrap_lab_host_vm(lab_host_ip, defaults):
    """
    Turns the freshly-booted lab-host VM into a KVM/libvirt host running a
    nested, NAT-mode automation VM — reusing setup_kvm_node.py +
    setup_lab_automation.sh completely unchanged (this project's own
    already-tested "automation VM under NAT" flow — see README's Walkthrough
    3), just configured to run one level deeper than usual. Nothing new to
    verify here beyond "does the copy + remote invocation work" — the
    bootstrap logic itself already has its own live-tested track record.
    """
    # install_demo_server_scripts.sh only CHECKS for python3.11 and dies if
    # it's missing — it doesn't install it (confirmed live 2026-08-31: this
    # project's documented "tested images" — SLE Micro, openSUSE Leap
    # Micro — apparently ship it already; the plain openSUSE Leap 15.6
    # Cloud image this VM uses does not). Installing it explicitly here
    # rather than assuming a specific base image's own package set.
    #
    # `nc` is a second, similar gap: setup_kvm_node.py's own `-y`-only path
    # (main(), no target host) deliberately probes reachability of the
    # literal string "-y" via `nc -z -w 5 -y 22` expecting it to just report
    # "unreachable" so it falls through to the real do_it_all() branch — a
    # legitimate design, but it dies with an uncaught FileNotFoundError
    # instead when `nc` itself isn't installed (confirmed live 2026-08-31 on
    # this same Cloud image). Not a bug worth patching in setup_kvm_node.py
    # itself — just another missing base-image prerequisite, same as
    # python311 above.
    log("Installing python3.11 + nc (prerequisites setup_kvm_node.py itself only checks for, doesn't install)")
    # openSUSE Leap 16.0 dropped the versioned `python311` package entirely —
    # confirmed live 2026-09-01: its system `python3` IS 3.13, no separate
    # 3.11 package exists or is needed, "zypper install python311" just
    # fails with "No provider of 'python311' found". setup_kvm_node.py itself
    # is hardcoded to invoke `python3.11` specifically though (matching every
    # other place this project pins 3.11), so rather than special-case the
    # invocation, symlink python3.11 -> the system python3 here whenever the
    # real package isn't available but python3 itself is already new enough.
    r = ssh_run(lab_host_ip, "zypper --gpg-auto-import-keys install -y python311", check=False)
    if r.returncode != 0:
        ssh_run(
            lab_host_ip,
            "command -v python3.11 >/dev/null 2>&1 || "
            "{ v=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3, 7))'); "
            "[ \"$v\" = True ] && ln -sf \"$(command -v python3)\" /usr/local/bin/python3.11; }",
        )
    ssh_run(lab_host_ip, "zypper --gpg-auto-import-keys install -y netcat-openbsd")

    # openSUSE Leap 15.6 Cloud ships kernel-default-base — a stripped kernel
    # package with no KVM modules at all (confirmed live 2026-08-31: no
    # kvm_intel/kvm_amd under /lib/modules, /dev/kvm never appears, so every
    # nested VM this lab-host creates would fall back to pure QEMU emulation
    # instead of KVM acceleration). kernel-default-base conflicts with the
    # real kernel-default package, hence the forced resolution + reboot.
    # Skipped once /dev/kvm already exists (a re-run against an
    # already-fixed lab-host VM) — rebooting unconditionally on every retry
    # is wasted time at best and, confirmed live 2026-08-31, a source of
    # real flakiness (an SSH command landing mid-reboot dies with rc=255).
    has_kvm = ssh_run(lab_host_ip, "test -e /dev/kvm", check=False).returncode == 0
    if not has_kvm:
        log("Installing kernel-default (kernel-default-base ships no KVM modules) and rebooting")
        ssh_run(
            lab_host_ip,
            "zypper --gpg-auto-import-keys --non-interactive install --force-resolution kernel-default",
            input_text="1\n",
        )
        ssh_run(lab_host_ip, "reboot", check=False)
        check_ssh_conn(lab_host_ip)

    log("Copying setup_demo_server/ + libs/ onto the lab-host VM")
    repo_root = Path(__file__).resolve().parent.parent
    ssh_run(lab_host_ip, "mkdir -p /root/setup_demo_server /root/libs")
    # setup_kvm_node.py resolves its own libs/ as a SIBLING directory
    # (_SCRIPT_DIR.parent / "libs") — mirroring the real repo layout, not
    # just the deployed /usr/local/lib/lab_creation one, which doesn't
    # exist yet on a brand-new host. Both dirs need to land as actual
    # siblings under /root for that resolution to work.
    for src, dst in (("setup_demo_server", "/root/setup_demo_server/"), ("libs", "/root/libs/")):
        r = __import__("subprocess").run([
            "rsync", "-aq", "{}/".format(repo_root / src), "root@{}:{}".format(lab_host_ip, dst),
        ])
        if r.returncode != 0:
            die("failed to rsync {}/ to the lab-host VM".format(src))

    # A hand-rolled minimal lab.cfg silently omits whatever variable
    # setup_lab_automation.sh happens to need (confirmed live 2026-08-31:
    # missing _qemu_addr alone breaks virt-install with
    # "--connect: expected one argument", and ROOT_SSH_PUB_KEY is needed too
    # — setup_kvm_node.py's main() loads ONLY lab.cfg, with no fallback to
    # lab.cfg.template's own defaults for anything it omits). Instead, start
    # from the real, complete lab.cfg.template already sitting in
    # setup_demo_server/ and override just the handful of keys this flow
    # actually needs to change.
    template_path = repo_root / "setup_demo_server" / "lab.cfg.template"
    local_pubkey = Path("/root/.ssh/id_rsa.pub")
    if not local_pubkey.is_file():
        die("no local {} found — needed to seed the lab-host VM's "
            "ROOT_SSH_PUB_KEY".format(local_pubkey))
    # setup_lab_automation.sh uses _mygw/_mydns/_mynet/_mynetrev directly,
    # unconditionally — it has no NAT-mode-aware derivation of its own
    # (confirmed live 2026-08-31: leaving lab.cfg.template's bridge-mode
    # defaults, 192.168.8.0/24, in place while _myip pointed into the NAT
    # range produced a VM with a static IP but a gateway/DNS on a network it
    # was never attached to — it never became reachable). configure_nat_network()
    # always puts libvirt's own gateway/DNS forwarder at the NAT range's
    # first host address, so derive all four from the same CIDR this flow
    # already uses for the network itself.
    _nat_net = ipaddress.ip_network(_NAT_NETWORK_CIDR, strict=False)
    _nat_gateway = str(list(_nat_net.hosts())[0])
    overrides = {
        "_network_mode": "\"nat\"",
        "_nat_network_name": "\"{}\"".format(_NAT_NETWORK_NAME),
        "_nat_network_cidr": "\"{}\"".format(_NAT_NETWORK_CIDR),
        "_myip": "\"{}\"".format(_NESTED_AUTOMATION_IP),
        "_mygw": _nat_gateway,
        "_mydns": _nat_gateway,
        "_mynet": _NAT_NETWORK_CIDR,
        "_mynetrev": ".".join(reversed(str(_nat_net.network_address).split(".")[:3])),
        "AUTOMATION_HOSTNAME": "'automation.lab'",
        "_QCOW_IMAGE": defaults.get(
            "_QCOW_IMAGE",
            "/var/lib/libvirt/images/sources/openSUSE-Leap-15.6-Minimal-VM.x86_64-kvm-and-xen.qcow2",
        ),
        "ROOT_SSH_PUB_KEY": "'{}'".format(local_pubkey.read_text().strip()),
        # lab.cfg.template's own default ("spice") needs QEMU built with
        # spice support — confirmed live 2026-08-31, same failure the
        # template's own comment already documents for a minimal-install
        # host: "unsupported configuration: spice graphics are not
        # supported with this QEMU". "none" looked like the obvious
        # alternative but is actively wrong here: confirmed live 2026-08-31
        # via `virsh domstats --cpu-total` (pegged near 100% indefinitely,
        # the VM never reachable) vs. a `virsh screenshot` A/B test — with
        # --graphics=none this specific openSUSE-Leap-15.6 kvm-and-xen
        # appliance image spins in a boot-time busy-loop and never brings
        # networking up at all; with any real graphics device attached it
        # boots cleanly and comes up on the network within a minute. "vnc"
        # needs no spice packages and doesn't require an actual viewer.
        "_automation_graphics": "\"vnc\"",
    }
    lab_cfg_lines = []
    seen = set()
    for line in template_path.read_text().splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
        if key in overrides:
            lab_cfg_lines.append("{}={}".format(key, overrides[key]))
            seen.add(key)
        else:
            lab_cfg_lines.append(line)
    # Any override key lab.cfg.template didn't already have a line for
    # (shouldn't happen today, but fail loudly rather than silently drop it
    # if the template ever changes shape).
    missing = set(overrides) - seen
    if missing:
        die("lab.cfg.template is missing expected key(s): {}".format(", ".join(sorted(missing))))
    ssh_run(lab_host_ip, "cat > /root/setup_demo_server/lab.cfg", input_text="\n".join(lab_cfg_lines) + "\n")

    log("Running setup_kvm_node.py on the lab-host VM "
        "(creates the NAT'd libvirt network + the nested automation VM — unchanged, already-tested code)")
    ssh_run(
        lab_host_ip,
        "cd /root/setup_demo_server && python3.11 setup_kvm_node.py -y",
        check=True,
    )
    check_ssh_conn(_NESTED_AUTOMATION_IP)
    log("Nested automation VM is up at {}".format(_NESTED_AUTOMATION_IP))


def deploy_lab_on_nested_automation(definition, host_name_hint):
    """
    Installs the lab_creation toolchain onto the nested automation VM
    (install_automation_node_scripts.sh, unchanged), points its own
    REMOTE_HOST back at the lab-host VM itself (which is "the hypervisor"
    from the nested automation VM's point of view), then runs setup_lab.py
    on it — unchanged — against a remapped copy of the original lab
    definition. This is where every lab VM actually gets created; no new
    VM-creation code exists in this file at all for that.
    """
    log("Installing lab_creation onto the nested automation VM")
    ssh_run(_NESTED_AUTOMATION_IP, "mkdir -p /root/lab-in-a-box")
    repo_root = str(Path(__file__).resolve().parent.parent)
    r = __import__("subprocess").run([
        "rsync", "-aq", "--exclude=.git",
        "{}/".format(repo_root), "root@{}:/root/lab-in-a-box/".format(_NESTED_AUTOMATION_IP),
    ])
    if r.returncode != 0:
        die("failed to rsync the lab-in-a-box repo to the nested automation VM")
    ssh_run(_NESTED_AUTOMATION_IP, "cd /root/lab-in-a-box && ./install_automation_node_scripts.sh")

    lab_creation_cfg = (
        "REMOTE_HOST=\"{gw}\"\n"
        "VIRT_SRV=\"qemu+ssh://root@{gw}/system?keyfile=.ssh/id_rsa\"\n"
        "ROOT_SSH_KEY=\"$(cat /root/.ssh/id_rsa.pub)\"\n"
        "NETWORK=\"network={nat_name}\"\n"
    ).format(gw=_NESTED_AUTOMATION_IP.rsplit(".", 1)[0] + ".1", nat_name=_NAT_NETWORK_NAME)
    ssh_run(_NESTED_AUTOMATION_IP,
            "cp /etc/lab_creation.cfg.example /etc/lab_creation.cfg 2>/dev/null; "
            "cat > /etc/lab_creation.cfg", input_text=lab_creation_cfg)

    remapped = lab_usb.remap_lab_definition_to_nat(
        definition, nat_cidr=_NAT_NETWORK_CIDR, nested_automation_ip=_NESTED_AUTOMATION_IP)
    remapped_json = __import__("json").dumps(remapped, indent=2)
    ssh_run(_NESTED_AUTOMATION_IP, "cat > /root/lab.json", input_text=remapped_json)

    log("Running setup_lab.py on the nested automation VM — every lab VM is created from here on, "
        "using the same unchanged code path as any other deployment")
    ssh_run(_NESTED_AUTOMATION_IP, "setup_lab.py /root/lab.json")


def configure_handoff(definition):
    """
    Task 3's remaining pieces (SSH key generation/distribution is already
    done — ensure_lab_ssh_key/distribute_lab_ssh_key, called by the
    caller): the informational site on port 6969, MOTD, and a random
    8-digit numeric root password written into /etc/issue.
    """
    import random
    password = "".join(str(random.randint(0, 9)) for _ in range(8))
    ssh_run(_NESTED_AUTOMATION_IP, "echo 'root:{}' | chpasswd".format(password))

    nodes = list((definition.get("nodes", {}) or {}).keys())
    site_html = (
        "<!doctype html><html><head><title>Lab info</title></head><body>"
        "<h1>lab-in-a-box — delivered lab</h1>"
        "<p>Nodes in this lab: {}</p>"
        "<p>Root password for this appliance: see /etc/issue on the console.</p>"
        "</body></html>"
    ).format(", ".join(nodes) or "(none)")
    ssh_run(_NESTED_AUTOMATION_IP, "mkdir -p /root/lab-info-site && cat > /root/lab-info-site/index.html",
            input_text=site_html)
    ssh_run(_NESTED_AUTOMATION_IP,
            "cd /root/lab-info-site && nohup python3.11 -m http.server 6969 "
            ">/var/log/lab-info-site.log 2>&1 & disown")

    issue_text = (
        "\\S\n"
        "This is a lab-in-a-box delivered lab appliance.\n"
        "Root password: {}\n"
        "Lab info: http://<this-host>:6969/\n"
    ).format(password)
    ssh_run(_NESTED_AUTOMATION_IP, "cat > /etc/issue", input_text=issue_text)
    ssh_run(_NESTED_AUTOMATION_IP,
            "echo 'Lab info available at http://\\$(hostname -I | awk \"{print \\$1}\"):6969/' > /etc/motd")
    log("Root password (also written to /etc/issue on the nested automation VM): {}".format(password))


def main():
    if len(sys.argv) < 2:
        print("Usage: {} <lab.json> [--build-only]".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    build_only = "--build-only" in sys.argv[2:]
    if "--build-only" not in sys.argv[2:] and len(sys.argv) > 2:
        die("Unknown argument(s): {}".format(" ".join(a for a in sys.argv[2:] if a != "--build-only")))

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    total_cpu, total_mem, total_disk = total_lab_resources(definition)
    log("Lab totals: {} vCPU, {} MiB RAM, {} GiB disk across {} node(s)".format(
        total_cpu, total_mem, total_disk, len(definition.get("nodes", {}))))

    host_name = _lab_host_name(definition)
    backend, remote_host, virt_srv, mymac = create_lab_host_vm(definition, config, defaults, host_name)
    bridge = config.get("NETWORK", "bridge=br0").split("=", 1)[-1]
    lab_host_ip = discover_lab_host_ip(host_name, remote_host, virt_srv, mymac, bridge)
    log("Lab-host VM reachable at {}".format(lab_host_ip))
    check_ssh_conn(lab_host_ip)

    bootstrap_lab_host_vm(lab_host_ip, defaults)
    deploy_lab_on_nested_automation(definition, host_name)

    pubkey = ensure_lab_ssh_key(_NESTED_AUTOMATION_IP)
    remapped = lab_usb.remap_lab_definition_to_nat(
        definition, nat_cidr=_NAT_NETWORK_CIDR, nested_automation_ip=_NESTED_AUTOMATION_IP)
    target_ips = [n.get("myip") for n in remapped.get("nodes", {}).values() if n.get("myip")]
    distribute_lab_ssh_key(_NESTED_AUTOMATION_IP, pubkey, target_ips)

    configure_handoff(definition)

    log("Shutting down the lab-host VM")
    run_libvirt_tool("virsh", remote_host, virt_srv, ["shutdown", host_name], check=False)
    for _ in range(60):
        result = run_libvirt_tool("virsh", remote_host, virt_srv, ["domstate", host_name],
                                   capture_output=True, text=True, check=False)
        if "shut off" in (result.stdout or ""):
            break
        time.sleep(5)
    else:
        die("lab-host VM did not shut down cleanly within 5 minutes")

    vm_img_loc = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")
    image_path = "{}/{}.raw".format(vm_img_loc, host_name)
    log("Lab-host VM shut down. Raw image ready at {}:{}".format(remote_host, image_path))

    if build_only:
        print(image_path if not remote_host else "{}:{}".format(remote_host, image_path))
        return

    die("Real USB device write is not yet implemented — pass --build-only "
        "and copy {}:{} manually for now".format(remote_host, image_path))


if __name__ == "__main__":
    main()
