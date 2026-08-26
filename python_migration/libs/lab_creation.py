"""
lab_creation.py — VM management, DNS, Helm, SSH, templates, and variable loading.

Python equivalent of lab_creation.bash.

Typical usage:
    from lab_creation import (
        ssh_run, ssh_output, check_ssh_conn,
        load_vm_vars, section_vars,
        add_to_dns, del_from_dns,
        setup_helm, helm_repo_add,
        process_template,
    )
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import base64
import json
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ── Output helpers ────────────────────────────────────────────────────────────

_RED    = "\033[1;91m"
_YELLOW = "\033[1;33m"
_GREEN  = "\033[1;92m"
_WHITE  = "\033[1;97m"
_RESET  = "\033[0m"

_level = 0  # current indentation depth (mirrors $_lvl in bash)


def log(msg, level=None):
    """Print an indented message. Uses the module-level _level if level is None."""
    indent = "  " * (_level if level is None else level)
    print(indent + msg)


def warn(msg):
    """Print a yellow warning to stderr."""
    print("{}WARNING:{} {}".format(_YELLOW, _RESET, msg), file=sys.stderr)


def die(msg):
    """Print a red error and exit (mirrors fail_with_error)."""
    print("{}ERROR:{} {}".format(_RED, _RESET, msg), file=sys.stderr)
    raise SystemExit(1)


# ── Lab definition preflight validation ───────────────────────────────────────

def _jq_or(value, default=""):
    """Mirrors jq's `//` operator: only None/False count as absent (empty string is truthy)."""
    return default if value is None or value is False else value


def _empty(value):
    """Mirrors bash `[[ -z "$val" ]]` applied to a jq `// ""`-defaulted value."""
    return value is None or value is False or value == ""


def resolve_install_type(install_type, iso_image):
    """
    Auto-detect the installer flavour from the ISO filename when install_type is
    blank. Mirrors _resolve_install_type (bash).
    """
    itype = install_type or ""
    if not itype:
        iso_lower = (iso_image or "").lower()
        if re.search(r"sles|suse|opensuse|sl-micro|sle-micro", iso_lower):
            itype = "autoyast"
        elif re.search(r"rhel|centos|rocky|almalinux|fedora", iso_lower):
            itype = "kickstart"
        elif re.search(r"ubuntu", iso_lower):
            itype = "autoinstall"
        elif re.search(r"debian", iso_lower):
            itype = "preseed"
        else:
            itype = "autoyast"
    return itype


def validate_lab_definition(path, config, iso_loc, lab_setup_path, target_node=None, vm_img_loc=None):
    """
    Preflight-validate a lab definition file. Mirrors validate_lab_definition (bash),
    generalized to resolve a KVM host per node (new in the python port — bash
    only ever had one hypervisor).

    Call before setup_lab.py / setup_vm.py begins work. Prints a full issue
    report (errors + warnings) and returns True iff there were no errors —
    mirrors the bash function's 0/1 return code (warnings never block).

    Args:
        path           : path to the lab JSON file (re-read and parsed here,
                          same as the bash version, since JSON validity is
                          itself the first thing checked).
        config         : lab_creation.cfg dict (REMOTE_HOST, VIRT_SRV, KVM_HOSTS, …) —
                          used to resolve_kvm_host() each node, same resolution
                          that will place it when actually created.
        iso_loc         : source image directory on the hypervisor (ISO_LOC) —
                          identical path on every KVM host, by design.
        lab_setup_path  : ignition/combustion/cloud-init/install_iso template tree
                          on the automation VM (LAB_SETUP_PATH).
        target_node     : optional single VM hostname to restrict per-node/per-
                          kcluster checks to (mirrors the optional 2nd bash arg).
        vm_img_loc      : passed through to resolve_kvm_host()'s select_kvm_host()
                          resource probing when KVM_HOSTS has more than one host.

    NOTE: the bash version also computes a `_known_addons` list (via `command -v
    install_rancher` + a directory listing, falling back to `compgen -c`) that
    is assigned but never read anywhere in the function — verified with grep,
    dropped here as dead code with no behavioural effect.

    NOTE: the bash version iterates nodes/kclusters via `while read <<< "$var"`
    (a here-string), which runs the loop body once with an empty value even
    when $var is empty — bash:117 lacked the `[[ -z ... ]] && continue` guard
    the other two here-string loops in this function already had, so a lab
    with an empty "nodes" object would get a spurious "nodes.: 'myip' is
    required" error. Fixed in both bash (added the guard) and here (a plain
    Python loop over an empty list simply doesn't iterate).
    """
    issues = []
    counts = {"errors": 0, "warnings": 0}

    def err(msg):
        issues.append("  [ERROR] {}".format(msg))
        counts["errors"] += 1

    def warn(msg):
        issues.append("  [WARN]  {}".format(msg))
        counts["warnings"] += 1

    if target_node:
        print("{}── Preflight: validating '{}' for node '{}' ──{}".format(_WHITE, path, target_node, _RESET))
    else:
        print("{}── Preflight: validating '{}' ──{}".format(_WHITE, path, _RESET))

    # ── 1. JSON syntax ────────────────────────────────────────────────────────
    try:
        definition = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        err("JSON syntax error in '{}' — run: jq . '{}'".format(path, path))
        print("\n".join(issues))
        print("{}✗ Preflight FAILED{} — {} error(s), {} warning(s)".format(
            _RED, _RESET, counts["errors"], counts["warnings"]))
        return False

    # ── 2. Required top-level sections ────────────────────────────────────────
    if "nodes" not in definition:
        err("Missing required section: 'nodes'")
    if "common" not in definition:
        err("Missing required section: 'common'")

    common = definition.get("common") or {}
    nodes = definition.get("nodes") or {}
    kclusters = definition.get("kclusters") or {}

    # ── 3. common: required fields ────────────────────────────────────────────
    iso = _jq_or(common.get("ISO_IMAGE"))
    if _empty(iso):
        err("common.ISO_IMAGE is required")

    for req in ("VM_MEM", "VM_DSK", "VM_CPU"):
        if _empty(_jq_or(common.get(req))):
            err("common.{} is required".format(req))

    dsk_bus = _jq_or(common.get("VM_DSK_BUS"))
    if not _empty(dsk_bus) and dsk_bus not in ("virtio", "scsi", "sata", "usb", "ide"):
        err("common.VM_DSK_BUS '{}' is invalid — must be one of: virtio, scsi, sata, usb, ide".format(dsk_bus))

    net_model = _jq_or(common.get("VM_NET_MODEL"))
    if not _empty(net_model) and net_model not in ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci"):
        err("common.VM_NET_MODEL '{}' is invalid — must be one of: virtio, e1000, e1000e, rtl8139, vmxnet3, ne2k_pci".format(net_model))

    # ── 4. Per-node checks ─────────────────────────────────────────────────────
    seen_ips = set()
    seen_macs = set()
    kclusters_defined = set(kclusters.keys())

    nodes_to_check = [target_node] if target_node else list(nodes.keys())

    # Resolved lazily per node and cached — section 7 (image checks) reuses
    # the same resolution. A die() from resolve_kvm_host()/select_kvm_host()
    # (no host has enough resources, or bad config) is caught here rather
    # than aborting validation outright, so it can be reported as one more
    # preflight error alongside everything else.
    node_hosts = {}

    def resolved_host_for(node):
        if node not in node_hosts:
            try:
                node_hosts[node] = resolve_kvm_host(definition, node, config, vm_img_loc)
            except SystemExit:
                node_hosts[node] = (None, None)
        return node_hosts[node]

    for node in nodes_to_check:
        node_cfg = nodes.get(node) or {}
        myip = _jq_or(node_cfg.get("myip"))
        mymac = _jq_or(node_cfg.get("mymac"))
        kcluster = _jq_or(node_cfg.get("kcluster"))

        if _empty(myip):
            err("nodes.{}: 'myip' is required".format(node))

        if not _empty(myip):
            if myip in seen_ips:
                err("nodes.{}: IP {} is already assigned to another node".format(node, myip))
            else:
                seen_ips.add(myip)

        if not _empty(mymac):
            mymac_l = str(mymac).lower()
            if mymac_l in seen_macs:
                err("nodes.{}: MAC {} is duplicated within this JSON".format(node, mymac))
            else:
                seen_macs.add(mymac_l)

        if not _empty(kcluster) and kcluster not in kclusters_defined:
            err("nodes.{}: references kcluster '{}' which is not defined in 'kclusters'".format(node, kcluster))

        # Hypervisor: VM name must not already exist on the host it would be
        # (re)created on
        node_host, node_virt_srv = resolved_host_for(node)
        if node_virt_srv:
            dominfo = subprocess.run(
                ["virsh", "--connect", node_virt_srv, "dominfo", node],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if dominfo.returncode == 0:
                warn("nodes.{}: a VM with this name already exists on KVM host '{}' "
                     "(will be destroyed and recreated)".format(node, node_host))

        # IP reachability (informational)
        if not _empty(myip):
            ping = subprocess.run(["ping", "-c1", "-W1", str(myip)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ping.returncode == 0:
                warn("nodes.{}: IP {} is currently responding to ping — "
                     "something may already be using it".format(node, myip))

    # ── 5. kcluster checks ─────────────────────────────────────────────────────
    if target_node:
        node_kcluster = _jq_or((nodes.get(target_node) or {}).get("kcluster"))
        clusters_to_check = [] if _empty(node_kcluster) else [node_kcluster]
    else:
        clusters_to_check = list(kclusters.keys())

    for clu in clusters_to_check:
        clu_cfg = kclusters.get(clu) or {}
        ctype = _jq_or(clu_cfg.get("clu_type"))
        crel = _jq_or(clu_cfg.get("clu_rel"))
        cdomain = _jq_or(clu_cfg.get("mydomain"))

        if _empty(ctype):
            err("kclusters.{}: 'clu_type' is required (rke2 or k3s)".format(clu))
        if _empty(crel):
            err("kclusters.{}: 'clu_rel' is required (e.g. stable)".format(clu))
        if _empty(cdomain):
            err("kclusters.{}: 'mydomain' is required".format(clu))

        for addon in clu_cfg.get("addons") or []:
            if shutil.which("install_{}".format(addon)) is None:
                err("kclusters.{}: addon '{}' — script 'install_{}' not found in PATH".format(clu, addon, addon))

    # Per-VM addon scripts must also be present
    for node in nodes_to_check:
        node_cfg = nodes.get(node) or {}
        for addon in node_cfg.get("addons") or []:
            if shutil.which("install_{}".format(addon)) is None:
                err("nodes.{}: addon '{}' — script 'install_{}' not found in PATH".format(node, addon, addon))

    # ── 6. Per-addon field validation — delegate to each install script ───────
    # Each install_* script supports --validate <json> and exits non-zero
    # with [ERROR] lines if its own fields are invalid or missing.
    if target_node:
        node_kcluster = _jq_or((nodes.get(target_node) or {}).get("kcluster"))
        addon_list = list((nodes.get(target_node) or {}).get("addons") or [])
        if not _empty(node_kcluster):
            addon_list += list((kclusters.get(node_kcluster) or {}).get("addons") or [])
    else:
        addon_list = []
        for clu_cfg in kclusters.values():
            addon_list += list((clu_cfg or {}).get("addons") or [])
        for node_cfg in nodes.values():
            addon_list += list((node_cfg or {}).get("addons") or [])
    all_addons = sorted(set(addon_list))

    for addon in all_addons:
        exe = shutil.which("install_{}".format(addon))
        if not exe:
            continue
        result = subprocess.run([exe, "--validate", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            combined = (result.stdout or "") + (result.stderr or "")
            for line in combined.splitlines():
                err("addon '{}': {}".format(addon, re.sub(r"^\[ERROR\]\s*", "", line)))

    # ── 7. Hypervisor: source image exists ────────────────────────────────────
    # Per-node/per-host: with multiple KVM hosts a node's image needs to
    # exist on the specific host it resolves to (ISO_LOC is the same path on
    # every host by design, but the file itself only needs to be there via
    # setup_kvm_node's storage-sharing step — checked per host, not assumed).
    if _empty(iso_loc):
        err("ISO_LOC is not set — check /etc/lab_creation.defaults")

    hv_ssh_ok = {}  # host -> bool, cached so each distinct host is only SSH-tested once

    def hv_reachable(host):
        if host not in hv_ssh_ok:
            ssh_test = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=5", "-q", "root@{}".format(host), "echo ok"],
                capture_output=True, text=True,
            )
            ok = (ssh_test.stdout or "").strip() == "ok"
            if not ok:
                err("Cannot reach KVM host '{}' via SSH — image checks skipped ({})".format(
                    host, ((ssh_test.stdout or "") + (ssh_test.stderr or "")).strip()))
            hv_ssh_ok[host] = ok
        return hv_ssh_ok[host]

    def check_image_on_hv(img, label, host):
        if _empty(img) or _empty(iso_loc) or not host or not hv_reachable(host):
            return
        test = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=5", "-q", "root@{}".format(host),
             "test -f '{}/{}'".format(iso_loc, img)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if test.returncode != 0:
            avail_proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-q",
                 "root@{}".format(host),
                 "ls '{}/'*.qcow2 2>/dev/null | xargs -n1 basename".format(iso_loc)],
                capture_output=True, text=True,
            )
            avail = "  ".join((avail_proc.stdout or "").splitlines()[:10])
            suffix = " — available: {}".format(avail) if avail else ""
            err("{}: image '{}' not found at {}:{}/{}{}".format(label, img, host, iso_loc, img, suffix))

    if not _empty(iso_loc):
        hosts_used = {resolved_host_for(node)[0] for node in nodes_to_check} - {None}
        for host in hosts_used:
            check_image_on_hv(iso, "common.ISO_IMAGE", host)
        for node in nodes_to_check:
            node_host, _ = resolved_host_for(node)
            node_iso = _jq_or((nodes.get(node) or {}).get("ISO_IMAGE"))
            if not _empty(node_iso):
                check_image_on_hv(node_iso, "nodes.{}.ISO_IMAGE".format(node), node_host)

    # ── 8. Local: provisioning templates ──────────────────────────────────────
    # Only check the config methods actually used in this definition
    needs_ign_cmb = False
    needs_cloud_init = False
    needs_install_iso = False
    for node_cfg in nodes.values():
        cm = _jq_or((node_cfg or {}).get("config_method"))
        if cm == "cloud-init":
            needs_cloud_init = True
        elif cm == "install_iso":
            needs_install_iso = True
        elif cm in ("virt_customize", "iso-cloud-init"):
            pass
        else:
            needs_ign_cmb = True

    if needs_ign_cmb:
        if not Path("{}/ignition/template".format(lab_setup_path)).is_file():
            err("Ignition template not found: {}/ignition/template".format(lab_setup_path))
        if not Path("{}/combustion/template".format(lab_setup_path)).is_file():
            err("Combustion template not found: {}/combustion/template".format(lab_setup_path))

    if needs_cloud_init:
        for tpl in ("user-data", "network-config", "meta-data"):
            if not Path("{}/cloud-init/template_{}".format(lab_setup_path, tpl)).is_file():
                err("cloud-init template not found: {}/cloud-init/template_{}".format(lab_setup_path, tpl))

    if needs_install_iso:
        itype = _jq_or(common.get("install_type"))
        if not _empty(itype) and itype not in ("autoyast", "kickstart", "preseed", "autoinstall"):
            err("common.install_type '{}' is invalid — must be: autoyast, kickstart, preseed, or autoinstall".format(itype))
        eff_itype = resolve_install_type("" if _empty(itype) else itype, iso)
        if not Path("{}/install_iso/template_{}".format(lab_setup_path, eff_itype)).is_file():
            err("install_iso template not found: {}/install_iso/template_{}".format(lab_setup_path, eff_itype))

    # ── Report ─────────────────────────────────────────────────────────────────
    if issues:
        print("\n".join(issues))

    if counts["errors"] > 0:
        print("{}✗ Preflight FAILED{} — {} error(s), {} warning(s). Fix the above before proceeding.".format(
            _RED, _RESET, counts["errors"], counts["warnings"]))
        return False
    else:
        print("{}✓ Preflight passed{} — 0 errors, {} warning(s).".format(_GREEN, _RESET, counts["warnings"]))
        return True


# ── MAC address handling ────────────────────────────────────────────────────

def _list_domain_macs(virt_srv):
    """
    Returns (all_domain_lines, mac_by_domain) — the raw `virsh list --all --name`
    output lines and a {domain: lowercased_mac} map built from each domain's
    first vnet interface. Shared helper for check_or_generate_mac/vm_is_reusable
    style lookups (bash re-ran `virsh domiflist` per call site; consolidated
    here into one pass since the result is identical either way).
    """
    domains = subprocess.run(
        ["virsh", "--connect", virt_srv, "list", "--all", "--name"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    domains = [d.strip() for d in domains if d.strip()]

    mac_by_domain = {}
    for dom in domains:
        domif = subprocess.run(
            ["virsh", "--connect", virt_srv, "domiflist", dom],
            capture_output=True, text=True,
        ).stdout
        for line in domif.splitlines():
            if not line[:1].isspace():
                continue
            fields = line.split()
            if len(fields) >= 5 and re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", fields[4].lower()):
                mac_by_domain[dom] = fields[4].lower()
                break
    return domains, mac_by_domain


def _generate_unused_mac(used_macs):
    """
    Generate a random locally-administered unicast MAC not present in used_macs
    (an iterable of lower-case MAC strings). Mirrors _generate_unused_mac (bash).

    NOTE: uses Python's random module rather than bash's $RANDOM — different
    PRNG, but the same algorithm (byte0 = random & 0xFC | 0x02, bytes 1-5
    uniform random) and the same guarantee (result not in used_macs). Bit-exact
    output parity between bash and python runs was never possible here since
    they use unrelated PRNGs; functional parity (a valid, unused, locally-
    administered MAC) is what's preserved.
    """
    used = set(used_macs)
    while True:
        b0 = "{:02x}".format((random.randint(0, 255) & 0xFC) | 0x02)
        candidate = "{}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}".format(
            b0, random.randint(0, 255), random.randint(0, 255),
            random.randint(0, 255), random.randint(0, 255), random.randint(0, 255),
        )
        if candidate not in used:
            return candidate


def check_or_generate_mac(virt_srv, vm_name, mymac, input_file, bridge="br0", vm_net_model="virtio"):
    """
    Validate or generate the MAC address for a VM. Mirrors check_or_generate_mac (bash).

    Returns (mymac, network) — the resolved MAC and the libvirt NETWORK string
    built from it (bash instead wrote the globals `mymac` and `NETWORK`).

    Behaviour (identical to bash):
      - mymac empty                              → generate a random locally-
        administered MAC not already in use on the hypervisor.
      - mymac set, not in use (or used by this same VM) → use as-is.
      - mymac set, already used by a DIFFERENT VM → prompt on the controlling
        TTY to regenerate; on 'y'/'Y' rewrite input_file's nodes.<vm_name>.mymac
        in place and continue; on anything else, die() (mirrors fail_with_error,
        which exits the whole process — this never returns in that case).

    NOTE: bash rewrote input_file via `jq` (which reformats/pretty-prints the
    whole file); here it's rewritten via json.dump(indent=2) — same resulting
    data, cosmetically different byte-for-byte formatting.
    """
    _, mac_by_domain = _list_domain_macs(virt_srv)
    used_macs = set(mac_by_domain.values())

    if _empty(mymac):
        mymac = _generate_unused_mac(used_macs)
        log("- No MAC specified for \"{}{}{}\" — generated {}".format(_RED, vm_name, _RESET, mymac))
    else:
        mymac_lower = mymac.lower()
        owner = next((dom for dom, mac in mac_by_domain.items() if mac == mymac_lower), None)

        if owner and owner != vm_name:
            old_mac = mymac
            print("{}WARNING:{} MAC {} is already used by VM '{}'.".format(_YELLOW, _RESET, old_mac, owner),
                  file=sys.stderr)
            print("  Generate a new MAC and update {}? [y/N] ".format(input_file), end="", flush=True)
            with open("/dev/tty") as tty:
                answer = tty.readline().strip()
            if re.match(r"^[Yy]$", answer):
                mymac = _generate_unused_mac(used_macs)
                definition = json.loads(Path(input_file).read_text())
                definition["nodes"][vm_name]["mymac"] = mymac
                Path(input_file).write_text(json.dumps(definition, indent=2))
                log("- MAC updated to {} for \"{}{}{}\" in {}".format(mymac, _RED, vm_name, _RESET, input_file))
            else:
                die("MAC conflict on \"{}{}{}\" ({} owned by '{}') — aborting".format(
                    _RED, vm_name, _RESET, old_mac, owner))
        else:
            log("- MAC {} is available for \"{}{}{}\"".format(mymac, _RED, vm_name, _RESET))

    network = "bridge={},mac.address={},model={}".format(bridge, mymac, vm_net_model or "virtio")
    return mymac, network


def vm_is_reusable(virt_srv, vm_name, mymac, myip):
    """
    Returns True when the VM should be kept, False when it must be destroyed and
    recreated. Mirrors vm_is_reusable (bash).

    Checks in order: running on hypervisor, MAC matches (only when mymac is
    set), DNS resolves to expected IP, SSH accessible with default
    credentials. Any failed check returns False (safe default = recreate).
    """
    state = subprocess.run(
        ["virsh", "-c", virt_srv, "domstate", vm_name], capture_output=True, text=True,
    ).stdout.strip()
    if state != "running":
        log("  {}KEEP CHECK{} \"{}{}{}\": not running on hypervisor (state: {}) — will recreate".format(
            _YELLOW, _RESET, _RED, vm_name, _RESET, state or "not found"))
        return False

    if not _empty(mymac):
        _, mac_by_domain = _list_domain_macs(virt_srv)
        actual_mac = mac_by_domain.get(vm_name)
        if mymac.lower() != (actual_mac or "NOT_FOUND"):
            log("  {}KEEP CHECK{} \"{}{}{}\": MAC mismatch (want \"{}\", got \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, mymac, actual_mac or "none"))
            return False

    try:
        resolved_ip = socket.gethostbyname(vm_name)
    except OSError:
        resolved_ip = None
    if resolved_ip != myip:
        log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
            _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
        return False

    ssh_test = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
         "root@{}".format(vm_name), "exit 0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if ssh_test.returncode != 0:
        log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
            _YELLOW, _RESET, _RED, vm_name, _RESET))
        return False

    return True


# ── SSH helpers ───────────────────────────────────────────────────────────────

_SSH_BASE = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-q"]


def ssh_run(hostname, cmd, check=True, input_text=None, capture=False):
    """
    Run a shell command on a remote host as root via SSH.

    Args:
        hostname   : Target host (IP or FQDN).
        cmd        : Shell command string to execute remotely.
        check      : Raise RuntimeError on non-zero exit code.
        input_text : Text to send to the remote command's stdin.
        capture    : If True, capture stdout+stderr instead of streaming.

    Returns:
        subprocess.CompletedProcess
    """
    args = _SSH_BASE + ["root@{}".format(hostname), cmd]
    result = subprocess.run(
        args,
        universal_newlines=True,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            "SSH command failed (rc={}) on {}:\n  {}".format(
                result.returncode, hostname, cmd[:120]
            )
        )
    return result


def ssh_output(hostname, cmd):
    """Run a command on a remote host and return stripped stdout."""
    return ssh_run(hostname, cmd, capture=True).stdout.strip()


def check_ssh_conn(vm_name, tcp_port=22, retry_interval=2, retry_limit=100):
    """
    Poll a host's TCP port until it accepts a connection. Mirrors check_ssh_conn (bash).

    Defaults match bash exactly: 2s between retries, 100 retries (~200s total),
    port 22 — i.e. _tcp_port/_retry_interval/_retry_limit's bash defaults.
    Calls die() (mirrors fail_with_error, which exits the whole process) if the
    retry limit is exceeded — this never returns a failure value, same as bash.

    NOTE: bash used `nc -z -w 2 host port` for the probe (2s connect timeout);
    here a raw socket connect with the same 2s timeout is used instead — same
    observable result without depending on netcat being installed. bash's
    'ERROR - Netcat(nc) not installed' branch (exit code 127) has no equivalent
    since this doesn't shell out to nc.
    """
    global _level
    _level += 1
    log("Waiting for {}{}{} to come online".format(_RED, vm_name, _RESET))
    count = 0
    _level += 1
    try:
        while True:
            count += 1
            time.sleep(retry_interval)
            try:
                s = socket.create_connection((vm_name, tcp_port), timeout=2)
                s.close()
                log("{}{}{} is online".format(_RED, vm_name, _RESET))
                return
            except OSError:
                pass
            if count > retry_limit:
                die("retry limit ( {} ) exceeded waiting for {}{}{} to boot.".format(
                    retry_limit, _RED, vm_name, _RESET))
    finally:
        _level -= 2


# ── Variable loading ──────────────────────────────────────────────────────────

def section_vars(definition, section):
    """
    Return all key-value pairs from a named top-level section of the definition.

    Replaces the family of load_*_vars bash functions:
        load_rancher_vars() → section_vars(definition, "rancher")
        load_lh_vars()      → section_vars(definition, "longhorn")
        load_nv_vars()      → section_vars(definition, "neuvector")
        … and so on for every addon section.

    Only scalar values are returned; nested objects and arrays are skipped.
    Returns a dict.
    """
    raw = definition.get(section, {})
    if not raw:
        warn("No variables defined for section '{}'".format(section))
    return {k: v for k, v in raw.items() if isinstance(v, (str, int, float, bool))}


def load_common_vars(definition):
    """Return all key-value pairs from the 'common' section. Returns a dict."""
    return dict(definition.get("common", {}))


def load_vm_vars(definition, vm_name, config=None):
    """
    Load variables for a specific VM (mirrors load_vm_vars in bash).

    Merges 'common' keys with per-node keys, resolves kcluster → clu_name,
    and auto-detects mydns, mygw, mydomain, mynet_reverse when absent.

    Args:
        definition : Loaded lab definition dict.
        vm_name    : The node key as it appears in definition["nodes"].
        config     : Optional lab_creation.cfg dict for additional context.

    Returns a dict of all variables for this VM.
    """
    vars = {}

    vars.update(definition.get("common", {}))

    node = definition.get("nodes", {}).get(vm_name, {})
    for key, val in node.items():
        if key == "kcluster":
            vars["clu_name"] = val
        else:
            vars[key] = val

    if not vars.get("mydns"):
        vars["mydns"] = _detect_dns()
    if not vars.get("mygw"):
        vars["mygw"] = _detect_gateway()
    if not vars.get("mydomain"):
        vars["mydomain"] = _detect_domain()
    if not vars.get("mynet_reverse") and vars.get("myip"):
        vars["mynet_reverse"] = _reverse_ip(vars["myip"])
    if not vars.get("mymask"):
        vars["mymask"] = _detect_netmask()

    return vars


# ── Multi-host selection ───────────────────────────────────────────────────────
#
# New in the python port — bash only ever supported one hypervisor
# (REMOTE_HOST/VIRT_SRV as flat globals). KVM_HOSTS is an optional cfg key
# (space-separated, like REMOTE_DNS_SERVERS); when absent or single-valued,
# resolve_kvm_host() never calls select_kvm_host()'s SSH probing at all, so
# existing single-host setups are unaffected.

def _host_resources(host, vm_img_loc):
    """
    Query free vCPUs, free memory (MiB), and free disk (MiB) on vm_img_loc for
    one candidate KVM host, over SSH. Raises RuntimeError/ValueError on any
    query failure — the caller treats that host as disqualified rather than
    letting the whole selection blow up.
    """
    total_cpus = int(ssh_output(host, "nproc"))

    virt_srv = "qemu+ssh://root@{}/system?keyfile=.ssh/id_rsa".format(host)
    running = [d for d in ssh_output(host, "virsh --connect {} list --name".format(virt_srv)).splitlines() if d.strip()]
    used_cpus = 0
    for dom in running:
        used_cpus += int(ssh_output(host, "virsh --connect {} vcpucount --current {}".format(virt_srv, dom.strip())))
    free_cpu = max(total_cpus - used_cpus, 0)

    free_mem = int(ssh_output(host, "free -m | awk '/^Mem:/{print $7}'"))
    free_disk = int(re.sub(r"[^0-9]", "", ssh_output(host, "df -BM --output=avail {} | tail -1".format(vm_img_loc))))

    return free_cpu, free_mem, free_disk


def select_kvm_host(hosts, vm_cpu, vm_mem, vm_dsk, vm_img_loc):
    """
    Pick a KVM host from `hosts` with enough free CPU/memory/disk for a VM
    requesting vm_cpu vCPUs, vm_mem MiB RAM, and vm_dsk GiB disk on
    vm_img_loc (identical path on every host by design — see
    resolve_kvm_host). Among hosts with enough of all three, picks the one
    with the most free memory — simplest single tiebreaker, since memory is
    the usual bottleneck for these labs.

    Dies (via die()) listing each host's shortfall if none qualify.
    """
    candidates = []
    shortfalls = []
    for host in hosts:
        try:
            free_cpu, free_mem, free_disk = _host_resources(host, vm_img_loc)
        except (RuntimeError, ValueError) as e:
            shortfalls.append("{}: could not query resources ({})".format(host, e))
            continue

        missing = []
        if free_cpu < int(vm_cpu):
            missing.append("{} free vCPUs < {} requested".format(free_cpu, vm_cpu))
        if free_mem < int(vm_mem):
            missing.append("{} MiB free RAM < {} requested".format(free_mem, vm_mem))
        if free_disk < int(vm_dsk) * 1024:
            missing.append("{} MiB free disk < {} GiB requested".format(free_disk, vm_dsk))

        if missing:
            shortfalls.append("{}: {}".format(host, "; ".join(missing)))
        else:
            candidates.append((free_mem, host))

    if not candidates:
        die("No KVM host in [{}] has enough free resources for this VM "
            "({} vCPU, {} MiB RAM, {} GiB disk):\n  {}".format(
                ", ".join(hosts), vm_cpu, vm_mem, vm_dsk, "\n  ".join(shortfalls)))

    candidates.sort(reverse=True)  # most free memory first
    return candidates[0][1]


def _configured_hosts(config):
    """Returns (hosts, default_host): KVM_HOSTS split, or [REMOTE_HOST] when unset."""
    default_host = config.get("REMOTE_HOST", "")
    hosts_raw = config.get("KVM_HOSTS") or default_host
    hosts = hosts_raw.split() if hosts_raw else []
    return hosts, default_host


def _virt_srv_for_host(host, default_host, config):
    """
    libvirt connection URI for `host`. Reuses config["VIRT_SRV"] verbatim
    when `host` is the configured default REMOTE_HOST (preserving any
    customization — different keyfile, extra URI params — an existing
    single-host cfg might have); for any other host it's derived
    programmatically, since there's no per-host VIRT_SRV to reuse.
    """
    if host == default_host and config.get("VIRT_SRV"):
        return config["VIRT_SRV"]
    return "qemu+ssh://root@{}/system?keyfile=.ssh/id_rsa".format(host)


def resolve_kvm_host(definition, vm_name, config, vm_img_loc=None):
    """
    Resolve which KVM host a NEW VM should be created on, and its libvirt
    URI. For operations on an EXISTING VM (destroy, reboot), use
    locate_kvm_host() instead — see its docstring for why the two must not
    share one implementation.

    Precedence: an explicit nodes[vm_name].kvm_host override wins; otherwise
    select_kvm_host() picks one from KVM_HOSTS. With zero or one configured
    host (today's single-hypervisor setups, or KVM_HOSTS unset), no
    selection logic runs at all — the sole host is used directly, same as
    before this feature existed.

    Returns (remote_host, virt_srv).
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    explicit_host = node_cfg.get("kvm_host")

    hosts, default_host = _configured_hosts(config)

    if explicit_host:
        host = explicit_host
    elif len(hosts) <= 1:
        host = hosts[0] if hosts else default_host
    else:
        common_cfg = definition.get("common", {}) or {}
        vm_cpu = node_cfg.get("VM_CPU", common_cfg.get("VM_CPU", 0))
        vm_mem = node_cfg.get("VM_MEM", common_cfg.get("VM_MEM", 0))
        vm_dsk = node_cfg.get("VM_DSK", common_cfg.get("VM_DSK", 0))
        host = select_kvm_host(hosts, vm_cpu, vm_mem, vm_dsk, vm_img_loc or "/var/lib/libvirt/images/")

    return host, _virt_srv_for_host(host, default_host, config)


def locate_kvm_host(definition, vm_name, config):
    """
    Find which KVM host an EXISTING VM currently lives on, for destroy/
    reboot/reusability-check operations. Deliberately NOT the same code
    path as resolve_kvm_host(): a VM's host is never recorded anywhere
    after creation (kvm_host is optional and intentionally not written
    back to the JSON), so re-running resource-based selection here could
    return a different host than the one the VM actually runs on — the
    only reliable way to find it again is to ask each host directly.

    Precedence: an explicit nodes[vm_name].kvm_host override wins; with
    zero or one configured host, no probing happens (same as
    resolve_kvm_host() in that case). Otherwise queries each host with
    `virsh dominfo <vm_name>` and returns the first one that has it. Dies
    if no configured host has a domain by this name.
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    explicit_host = node_cfg.get("kvm_host")

    hosts, default_host = _configured_hosts(config)

    if explicit_host:
        host = explicit_host
        return host, _virt_srv_for_host(host, default_host, config)
    if len(hosts) <= 1:
        host = hosts[0] if hosts else default_host
        return host, _virt_srv_for_host(host, default_host, config)

    for host in hosts:
        virt_srv = _virt_srv_for_host(host, default_host, config)
        result = subprocess.run(
            ["virsh", "--connect", virt_srv, "dominfo", vm_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return host, virt_srv

    die("VM '{}' not found on any configured KVM host: {}".format(vm_name, ", ".join(hosts)))


# ── VM management ─────────────────────────────────────────────────────────────

def copy_vm_image(remote_host, iso_loc, iso_image, vm_img_loc, vm_name, vm_dsk_gb, config_method=""):
    """
    Copy a QCOW2 source image and resize it on the hypervisor. Mirrors copy_vm_img (bash).

    install_iso: the disk is created empty by virt-install, so there's nothing
    to copy or resize — mirrors bash's early return for config_method ==
    "install_iso".
    """
    if config_method == "install_iso":
        log("- install_iso: skipping base image copy (disk created by virt-install)")
        return

    log("- Copy the image for the new VM \"{}{}{}\"".format(_RED, vm_name, _RESET))
    result = ssh_run(remote_host, "cp {}/{} {}/{}.qcow2".format(iso_loc, iso_image, vm_img_loc, vm_name), check=False)
    if result.returncode != 0:
        die("Failed to copy image for vm  \"{}\"".format(vm_name))

    log("- Resize to {}G".format(vm_dsk_gb))
    result = ssh_run(remote_host, "qemu-img resize -f qcow2 {}/{}.qcow2 {}G".format(vm_img_loc, vm_name, vm_dsk_gb), check=False)
    if result.returncode != 0:
        die("Failed to resize VM image \"{}\" to \"{}G\"".format(vm_name, vm_dsk_gb))


def create_vm(
    virt_srv, vm_name, vm_cpu, vm_mem, vm_dsk_gb, vm_img_loc, network, remote_host,
    os_variant="slem5.4", boot="uefi", config_method="",  # boot: "uefi", "firmware=bios", "hd", …
    lab_setup_path="/srv/www/htdocs/lab_creation",
    extra_disks=None, extra_filesystems=None, vm_dsk_bus="virtio",
    ign_file=None, com_file=None, salt_states="",
    install_type="", iso_image="", iso_loc="", mydns="",
    vcluster="",
):
    """
    Create a VM on a KVM hypervisor via virt-install. Mirrors create_vm (bash),
    covering all 6 config_method branches:

        ""              → Ignition + Combustion (SLE Micro default)
        "install_iso"   → full OS install from installer ISO: autoyast/
                           kickstart/preseed (via --location/--extra-args,
                           blocks with --wait -1) or Ubuntu autoinstall (via
                           --cdrom + a seed CDROM built with mkisofs, also
                           --wait -1)
        "iso-cloud-init"→ NOTE: bash's own branch here only computes an unused
                           _boot_params value and creates no VM at all — it's
                           an incomplete stub in bash today. Preserved as a
                           no-op rather than guessing at the missing logic.
        "virt_customize"→ image already fully configured by
                           prepare_virt_customize_for_vm(); boot it directly
        "cloud-init"    → cloud-init ISO attached as a cdrom, then a 3-minute
                           wait, optional salt state apply, eject, reboot

    extra_disks entries look like "/dev/sdb,bus=scsi" or "UUID=xxx,bus=sata"
    (a path or a UUID= reference, with an optional per-disk bus override).
    """
    log("Creating VM '{}'".format(vm_name))

    # Normalise boot flag: "uefi=off" / "bios" / "legacy" → "firmware=bios"
    _BIOS_ALIASES = {"uefi=off", "bios", "legacy"}
    boot_flag = "firmware=bios" if boot in _BIOS_ALIASES else boot

    extra_disk_args = []
    for dsk in (extra_disks or []):
        bus_match = re.search(r",bus=([a-z]+)", dsk)
        dsk_bus_override = bus_match.group(1) if bus_match else ""
        dsk_path = dsk.split(",")[0]
        if "UUID" in dsk_path:
            lookup = ssh_run(
                remote_host,
                "lsblk -o UUID,PATH | grep {} | cut -d' ' -f2".format(dsk_path.replace("UUID=", "")),
                capture=True, check=False,
            )
            dsk_path = lookup.stdout.strip()
        extra_bus = dsk_bus_override or vm_dsk_bus or "virtio"
        extra_disk_args += ["--disk", "path={},bus={}".format(dsk_path, extra_bus)]

    extra_fs_args = []
    for fs in (extra_filesystems or []):
        extra_fs_args += ["--filesystem", fs]

    base_args = [
        "virt-install", "--connect", virt_srv,
        "--name", vm_name, "--autostart",
        "--boot", boot_flag, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
        "--os-variant", os_variant, "--import",
        "--disk", "size={},path={}/{}.qcow2,sparse=no,bus={},boot.order=1".format(
            vm_dsk_gb, vm_img_loc, vm_name, vm_dsk_bus or "virtio"),
        "--graphics", "spice,listen=0.0.0.0",
        "--network", network, "--noautoconsole",
    ]

    if config_method == "":
        ign = ign_file or vm_name
        com = com_file or vm_name
        qemu_args = (
            "-fw_cfg name=opt/com.coreos/config,"
            "file={}/ignition/{} "
            "-fw_cfg name=opt/org.opensuse.combustion/script,"
            "file={}/combustion/{}".format(lab_setup_path, ign, lab_setup_path, com)
        )
        _run(base_args + extra_fs_args + extra_disk_args + ["--qemu-commandline", qemu_args],
             "virt-install failed for '{}'".format(vm_name))

    elif config_method == "install_iso":
        itype = resolve_install_type(install_type, iso_image)

        if itype == "autoinstall":
            # Ubuntu 22+ subiquity: boot from --cdrom + a second "cidata" seed
            # CDROM. --wait -1 blocks until the installer powers the VM off.
            seed_local = tempfile.mktemp(prefix="seed_{}_".format(vm_name), suffix=".iso")
            seed_remote = "{}/seed_{}.iso".format(vm_img_loc, vm_name)
            mkiso = subprocess.run([
                "mkisofs", "-J", "-l", "-R", "-V", "cidata", "-iso-level", "3",
                "-o", seed_local,
                "{}/install_iso/{}/user-data".format(lab_setup_path, vm_name),
                "{}/install_iso/{}/meta-data".format(lab_setup_path, vm_name),
            ])
            if mkiso.returncode != 0:
                die("mkisofs seed failed for '{}'".format(vm_name))
            scp = subprocess.run([
                "scp", "-o", "StrictHostKeyChecking=accept-new", seed_local,
                "root@{}:{}".format(remote_host, seed_remote),
            ])
            os.unlink(seed_local)
            if scp.returncode != 0:
                die("scp seed failed for '{}'".format(vm_name))

            log("- Installing Ubuntu via autoinstall + seed CDROM (blocks until installer finishes)…")
            r = subprocess.run([
                "virt-install", "--connect", virt_srv,
                "--name", vm_name, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
                "--os-variant", os_variant or "ubuntu24.04",
                "--cdrom", "{}/{}".format(iso_loc, iso_image),
                "--disk", "size={},path={}/{}.qcow2,sparse=no,bus={},boot.order=2".format(
                    vm_dsk_gb, vm_img_loc, vm_name, vm_dsk_bus or "virtio"),
                "--disk", "path={},device=cdrom,readonly=on".format(seed_remote),
            ] + extra_disk_args + [
                "--graphics", "spice,listen=0.0.0.0",
                "--network", network, "--noautoconsole", "--wait", "-1",
            ])
            ssh_run(remote_host, "rm -f '{}'".format(seed_remote), check=False)
            if r.returncode != 0:
                die("virt-install (autoinstall) failed for '{}'".format(vm_name))
            subprocess.run(["virsh", "--connect", virt_srv, "autostart", vm_name])
            subprocess.run(["virsh", "--connect", virt_srv, "start", vm_name])
            return

        location_arg = "{}/{}".format(iso_loc, iso_image)
        extra_args_by_type = {
            "autoyast": "autoyast=http://{}/lab_creation/install_iso/{}.xml".format(mydns, vm_name),
            "kickstart": "inst.ks=http://{}/lab_creation/install_iso/{}.ks inst.sshd".format(mydns, vm_name),
            "preseed": "auto=true priority=critical url=http://{}/lab_creation/install_iso/{}.preseed".format(mydns, vm_name),
        }
        extra_args = extra_args_by_type[itype]

        log("- Installing via {} (this will block until the installer finishes)…".format(itype))
        r = subprocess.run([
            "virt-install", "--connect", virt_srv,
            "--name", vm_name, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
            "--os-variant", os_variant,
            "--location", location_arg,
            "--extra-args", "{} console=ttyS0,115200n8".format(extra_args),
            "--disk", "size={},path={}/{}.qcow2,sparse=no,bus={},boot.order=1".format(
                vm_dsk_gb, vm_img_loc, vm_name, vm_dsk_bus or "virtio"),
        ] + extra_disk_args + [
            "--graphics", "spice,listen=0.0.0.0",
            "--network", network, "--noautoconsole", "--wait", "-1",
        ])
        if r.returncode != 0:
            die("virt-install (install_iso) failed for '{}'".format(vm_name))
        # Installer powered off the VM — bring it back up and mark autostart
        subprocess.run(["virsh", "--connect", virt_srv, "autostart", vm_name])
        subprocess.run(["virsh", "--connect", virt_srv, "start", vm_name])

    elif config_method == "iso-cloud-init":
        # Mirrors bash exactly: this branch only ever computed an unused
        # _boot_params value (a Harvester config_url kernel arg) and never
        # actually called virt-install — a pre-existing incomplete stub, not
        # something introduced by this port. Left as a no-op.
        if vcluster == "harvester":
            pass  # _boot_params = "harvester.install.config_url=http://10.100.0.10/harvester/config-create.yaml"

    elif config_method == "virt_customize":
        # Image already fully configured by prepare_virt_customize_for_vm() —
        # boot it directly, no provisioning kernel args, no extra cdrom.
        r = subprocess.run(base_args + extra_fs_args + extra_disk_args)
        if r.returncode != 0:
            die("virt-install failed for '{}'".format(vm_name))

    elif config_method == "cloud-init":
        ci_iso = "{}/{}_ci.iso".format(vm_img_loc, vm_name)
        r = subprocess.run(base_args + extra_fs_args + extra_disk_args +
                            ["--disk", "{},device=cdrom".format(ci_iso)])
        if r.returncode != 0:
            die("virt-install for cloud-init failed for '{}'".format(vm_name))

        log("  - Waiting 3 minutes")
        time.sleep(180)

        if salt_states:
            log("  - applying salt states")
            setup_salt(vm_name, salt_states, lab_setup_path)
            for state in salt_states.split():
                subprocess.run(["salt-ssh", "-i", "-v", "--update-roster", vm_name, "state.apply", state])

        log("  - eject media")
        subprocess.run(["virsh", "--connect", virt_srv,
                        "change-media", vm_name, "--eject", ci_iso], check=False)

        log("- reboot node")
        subprocess.run(["virsh", "--connect", virt_srv, "reboot", vm_name], check=False)


def delete_vm(virt_srv, vm_name):
    """
    Remove a VM and all its storage from the hypervisor. Mirrors delete_vm
    (bash) — three calls, in this order:
      1. undefine --nvram (handles the VM if it's already shut off; undefine
         only works on a stopped domain)
      2. destroy (force power-off if it was running; expected to fail/no-op
         if step 1 already removed it — matches bash's 2>/dev/null on this
         call specifically)
      3. undefine --nvram --remove-all-storage (the one that actually deletes
         disk images; runs whether or not step 1 already handled the domain
         definition itself)
    """
    log("Deleting VM '{}'".format(vm_name))
    subprocess.run(["virsh", "-c", virt_srv, "undefine", vm_name, "--nvram"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["virsh", "-c", virt_srv, "destroy", vm_name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["virsh", "-c", virt_srv, "undefine", vm_name,
                    "--nvram", "--remove-all-storage"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def clean_ssh_keys(vm_name, myip):
    """Remove stale SSH known-hosts entries for a VM (mirrors clean_ssh_keys)."""
    known = str(Path.home() / ".ssh" / "known_hosts")
    for host in (vm_name, myip):
        subprocess.run(["ssh-keygen", "-f", known, "-R", host],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def prepare_local_as_kubeclient():
    """Ensure ~/.kube exists for kubeconfig storage (mirrors prepare_local_as_kubeclient)."""
    (Path.home() / ".kube").mkdir(parents=True, exist_ok=True)


def copy_to_hypervisor(remote_host, lab_setup_path, vm_name, config_method="", vm_img_loc=None):
    """
    Copy the provisioning materials needed for the install to the hypervisor.
    Mirrors copy_to_hypervisor (bash).

    config_method:
      "virt_customize" / "install_iso" → nothing to copy — both are already
        entirely hypervisor-side (virt-customize) or automation-VM-HTTP-side
        (install_iso answer files), same early return as bash.
      ""  (ignition+combustion, the default) → rsync the per-VM combustion
        file and ignition file, then chmod them world-readable.
      anything else (e.g. "cloud-init") → rsync the per-VM template_* output
        files, then build a NoCloud cidata ISO from them on the hypervisor.

    NOTE: bash's error messages on several of these steps were empty strings
    (`_msg="" fail_with_error`) — clearly unfinished placeholders, since an
    empty message produces a bare "ERROR: " with no detail. Filled in with
    descriptive text here; the pass/fail behaviour (die on any failure) is
    unchanged.
    """
    log("- Copy accross the lab setup materials")
    mkdir_test = ssh_run(remote_host, "[[ -d {0}/ ]] || mkdir -p {0}/".format(lab_setup_path), check=False)
    if mkdir_test.returncode != 0:
        die("failed creating new folder {}".format(lab_setup_path))

    if config_method in ("virt_customize", "install_iso"):
        return

    if config_method == "":
        r = ssh_run(remote_host, "mkdir -p {}/{{combustion,ignition}}".format(lab_setup_path), check=False)
        if r.returncode != 0:
            die("failed creating combustion/ignition folders on {}".format(remote_host))

        r = subprocess.run(["rsync", "-aqv",
                             "{}/combustion/{}".format(lab_setup_path, vm_name),
                             "root@{}:{}/combustion/".format(remote_host, lab_setup_path)])
        if r.returncode != 0:
            die("failed to rsync combustion file for '{}'".format(vm_name))

        r = subprocess.run(["rsync", "-aqv",
                             "{}/ignition/{}.ign".format(lab_setup_path, vm_name),
                             "root@{}:{}/ignition/".format(remote_host, lab_setup_path)])
        if r.returncode != 0:
            die("failed to rsync ignition file for '{}'".format(vm_name))

        r = ssh_run(remote_host, "chmod 0644 {0}/ignition/* {0}/combustion/*".format(lab_setup_path), check=False)
        if r.returncode != 0:
            die("failed to chmod ignition/combustion files on {}".format(remote_host))
    else:
        r = ssh_run(remote_host, "mkdir -p {}/{}".format(lab_setup_path, config_method), check=False)
        if r.returncode != 0:
            die("failed creating '{}' folder on {}".format(config_method, remote_host))

        # bash relied on an unquoted shell glob (${_vm_name}*) which bash itself
        # expands before invoking rsync — expand it the same way here.
        sources = sorted(str(p) for p in Path(lab_setup_path, config_method).glob("{}*".format(vm_name)))
        if not sources:
            die("no '{}' files found for '{}' in {}/{}".format(config_method, vm_name, lab_setup_path, config_method))
        r = subprocess.run(["rsync", "-aqv"] + sources +
                            ["root@{}:{}/{}/".format(remote_host, lab_setup_path, config_method)])
        if r.returncode != 0:
            die("failed to rsync '{}' files for '{}'".format(config_method, vm_name))

        remote_cmd = (
            "cd {lsp}/{cm}/; "
            "for i in {vm}*; do cp ${{i}} /tmp/${{i/{vm}_/}}; done ; "
            "rm -f {img}/{vm}_ci.iso; "
            "mkisofs -J -l -R -V cidata -iso-level 3 -o /tmp/ci_{vm}.iso "
            "/tmp/user-data /tmp/meta-data /tmp/network-config "
            "&& mv /tmp/ci_{vm}.iso {img}/{vm}_ci.iso"
        ).format(lsp=lab_setup_path, cm=config_method, vm=vm_name, img=vm_img_loc)
        r = ssh_run(remote_host, remote_cmd, check=False)
        if r.returncode != 0:
            die("failed to build cidata ISO for '{}'".format(vm_name))


# ── DNS management ────────────────────────────────────────────────────────────

NAMED_ZONE_DIR = Path("/var/lib/named")


def add_to_dns(vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
    """
    Add forward (A) and reverse (PTR) DNS records for a VM (mirrors add_to_dns).
    Modifies BIND zone files locally and on any remote DNS servers listed.
    """
    log("Adding DNS entry for '{}' → {}".format(vm_name, myip))
    short      = vm_name.split(".")[0]
    last_octet = myip.split(".")[-1]
    a_record   = "{}         IN  A       {}".format(short, myip)
    ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)

    lan_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
    rev_file = NAMED_ZONE_DIR / "{}.db".format(mynet_reverse)

    for server in (remote_dns_servers or []):
        _remote_dns_add(server, lan_file, a_record)
        _remote_dns_add(server, rev_file, ptr_record)
        _remote(server, "systemctl restart named")

    _dns_add_line(lan_file, a_record)
    _dns_add_line(rev_file, ptr_record)
    restart_named()


def del_from_dns(vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
    """Remove forward and reverse DNS records for a VM (mirrors del_from_dns)."""
    log("Removing DNS entry for '{}'".format(vm_name))
    short      = vm_name.split(".")[0]
    last_octet = myip.split(".")[-1]
    a_record   = "{}         IN  A       {}".format(short, myip)
    ptr_record = "{}      IN  PTR     {}.".format(last_octet, vm_name)

    lan_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
    rev_file = NAMED_ZONE_DIR / "{}.db".format(mynet_reverse)

    # NOTE: previously used just last_octet/short as the sed pattern here — a
    # substring-anywhere-in-line match, which risks e.g. deleting "node10"'s
    # entry while trying to delete "node1"'s. bash's own del_from_dns always
    # used the full record text as the sed pattern; matching that precision.
    for server in (remote_dns_servers or []):
        _remote(server, "sed '/{}/d' -i {}".format(ptr_record, rev_file))
        _remote(server, "sed '/{}/d' -i {}".format(a_record, lan_file))
        _remote(server, "systemctl restart named")

    _dns_remove_line(rev_file, ptr_record)
    _dns_remove_line(lan_file, a_record)
    restart_named()


def add_service_dns(definition, clu_name, clu_type, dns_entry, mydomain, remote_dns_servers=None):
    """
    Add round-robin A records for a cluster service DNS entry (mirrors add_service_dns).
    Prefers agent nodes; falls back to all cluster nodes if no agents exist.
    """
    nodes       = definition.get("nodes", {})
    install_key = "INSTALL_{}_TYPE".format(clu_type.upper())

    agent_nodes = [
        (name, cfg["myip"])
        for name, cfg in nodes.items()
        if cfg.get(install_key) == "agent" and cfg.get("kcluster") == clu_name and "myip" in cfg
    ]

    targets = agent_nodes or [
        (name, cfg["myip"])
        for name, cfg in nodes.items()
        if cfg.get("kcluster") == clu_name and "myip" in cfg
    ]

    msg = "agent" if agent_nodes else "all"
    log("DNS '{}' added pointing to {} nodes of cluster '{}'".format(dns_entry, msg, clu_name))

    zone_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)
    for _, ip in targets:
        record = "{}\tIN A  {}".format(dns_entry, ip)
        for server in (remote_dns_servers or []):
            _remote(server, "sed '/{}\tIN A  {}/d' -i {}".format(dns_entry, ip, zone_file))
            _remote(server, "echo -e '{}' >> {}".format(record, zone_file))
            _remote(server, "systemctl restart named")
        _dns_remove_line(zone_file, "{}\tIN A  {}".format(dns_entry, ip))
        _dns_append_line(zone_file, record)

    restart_named()


def restart_named(remote_servers=None):
    """Restart the local BIND named service and optionally on remote servers."""
    for server in (remote_servers or []):
        _remote(server, "systemctl restart named", check=False)
    subprocess.run(["systemctl", "restart", "named"], check=False)


def add_dns_to_named_rr(definition, dns_entry, node_name, mydomain, remote_dns_servers=None):
    """
    Add a single round-robin A record (dns_entry -> node_name's own myip) to
    the zone file, skipping if an identical record already exists. Mirrors
    add_dns_to_named_rr (bash).

    NOTE: a genuine standalone library function, not just inlined logic —
    while add_service_dns's per-target loop performs the equivalent
    add-one-record step for every target in a single call, install_rancher
    calls add_dns_to_named_rr directly, once per matching server node,
    followed by its own separate restart_named(). (A Phase 1 note claimed
    add_service_dns was this function's only caller — that was wrong; this
    second real caller only turned up while porting install_rancher.)
    """
    myip = (definition.get("nodes", {}).get(node_name, {}) or {}).get("myip", "")
    record = "{}\tIN A  {}".format(dns_entry, myip)
    zone_file = NAMED_ZONE_DIR / "{}.lan".format(mydomain)

    existing = zone_file.read_text().splitlines() if zone_file.exists() else []
    if record in existing:
        log("- DNS entry \"{}{} → {}{}\" already correct, skipping".format(_RED, dns_entry, myip, _RESET))
        return

    log("- add DNS entry \"{}{}.{}{}\"".format(_RED, dns_entry, mydomain, _RESET))

    for server in (remote_dns_servers or []):
        _remote_dns_add(server, zone_file, record)
        _remote(server, "systemctl restart named", check=False)

    _dns_add_line(zone_file, record)


# ── Provisioning files ────────────────────────────────────────────────────────

def prepare_ignition_combustion(
    vm_name, lab_setup_path, root_pwd_hash, root_ssh_key,
    mysource, sourcepath, mydns, myip, mymask, mygw,
    suse_email="", suse_regcode="", suse_url="",
):
    """
    Create Ignition + Combustion provisioning files for a VM. Mirrors
    prepare_ign_and_cmb (bash).

    NOTE: bash substitutes ROOT_SSH_KEY differently in the two output files —
    ignition.ign gets the LOCAL machine's own pubkey (`cat /root/.ssh/id_rsa.pub`,
    read fresh on every call), while combustion gets the `$ROOT_SSH_KEY` config
    variable (the `root_ssh_key` argument here). These can genuinely be
    different values, so both are preserved rather than collapsed into one.
    """
    base = Path(lab_setup_path)
    log("- Create ignition and combustion files for \"{}{}{}\"".format(_RED, vm_name, _RESET))

    root_pub_key = Path("/root/.ssh/id_rsa.pub").read_text().strip()

    ign_out = base / "ignition" / "{}.ign".format(vm_name)
    text = (base / "ignition" / "template").read_text()
    text = (text
            .replace("TEMPLATE_HN", vm_name)
            .replace("ROOT_PWD_HASH", root_pwd_hash)
            .replace("ROOT_SSH_KEY", root_pub_key))
    ign_out.write_text(text)

    cmb_out = base / "combustion" / vm_name
    text = (base / "combustion" / "template").read_text()
    inject = (
        "mysource={}\nsourcepath={}\nmydns={}\nmyip={}\n"
        "mymask={}\nmygw={}\nSUSE_email={}\nSUSE_regcode={}\nSUSE_url={}\n"
    ).format(mysource, sourcepath, mydns, myip, mymask, mygw,
             suse_email, suse_regcode, suse_url)
    text = text.replace("#local vars", "#local vars\n" + inject, 1)
    text = text.replace("ROOT_SSH_KEY", root_ssh_key)
    cmb_out.write_text(text)


def prepare_cloud_init(vm_name, lab_setup_path, variables):
    """
    Create cloud-init user-data, network-config, and meta-data files. Mirrors
    prepare_cloud-init (bash).

    variables : dict of values available to the templates via process_template
                (mirrors the shell variables visible during bash's eval/
                heredoc expansion). ROOT_SSH_KEY is always overridden with the
                local machine's own pubkey here — mirrors bash's local
                `ROOT_SSH_KEY=$(cat /root/.ssh/id_rsa.pub)` reassignment inside
                this function, which shadows whatever ROOT_SSH_KEY held before.
    """
    base = Path(lab_setup_path) / "cloud-init"
    log("- Create cloud-init files for \"{}{}{}\"".format(_RED, vm_name, _RESET))
    render_vars = dict(variables)
    render_vars["ROOT_SSH_KEY"] = Path("/root/.ssh/id_rsa.pub").read_text().strip()
    for kind in ("user-data", "network-config", "meta-data"):
        tmpl = base / "template_{}".format(kind)
        out  = base / "{}_{}".format(vm_name, kind)
        out.write_text(process_template(str(tmpl), render_vars))


# ── virt-customize ───────────────────────────────────────────────────────────
#
# NOTE: an earlier, local-execution design (_virt_ls/_virt_cat/_vc_detect_net_type/
# _vc_detect_iface/_vc_net_config, using virt-ls/virt-cat directly from the
# automation VM) lived here and was removed — verified via grep that none of
# them were called anywhere (not even from prepare_virt_customize below, and
# not from webui/). It was superseded by the current design, which generates a
# fully self-contained script (embedding its own vls/vcat/detect_net/etc.) and
# runs it entirely on the hypervisor over SSH, since virt-customize/virt-ls
# need to run where the qcow2 image actually lives.

def prepare_virt_customize_for_vm(
    remote_host, vm_img_loc, vm_name, myip, mymask, mygw, mydns, mydomain, mymac,
    vm_root_pass=None, root_pwd_hash=None, root_ssh_key_path=None,
):
    """
    Resolve the root password and SSH pubkey, then delegate to
    prepare_virt_customize(). Mirrors the bash-level prepare_virt_customize
    function (bash:605-669), which only computed these derived values and
    bridged into this same python function via `python3 -c` + env vars +
    base64 — that bridge is unnecessary here since this already runs
    in-process.

    Password fallback chain (identical to bash):
      VM_ROOT_PASS set   → plain password
      else ROOT_PWD_HASH → crypted hash
      else               → plain password "linux" (with a warning)

    Pubkey fallback chain (identical to bash):
      ROOT_SSH_KEY set (a private-key PATH) and "<path>.pub" exists → that pubkey
      else ~/.ssh/id_rsa.pub if it exists
      else no key injected

    NOTE: bash's own `|| fail_with_error` around this call is redundant here —
    prepare_virt_customize() already calls die() itself on failure (same
    message, mentioning vm_name), so there's nothing left for this wrapper
    to catch.
    """
    img = "{}/{}.qcow2".format(vm_img_loc, vm_name)
    prefix = mymask or "24"

    if vm_root_pass:
        pass_type, pass_val = "plain", vm_root_pass
    elif root_pwd_hash:
        pass_type, pass_val = "crypted", root_pwd_hash
    else:
        pass_type, pass_val = "plain", "linux"
        log("WARNING: neither VM_ROOT_PASS nor ROOT_PWD_HASH is set — using default password 'linux'")

    pubkey = None
    if root_ssh_key_path and Path(root_ssh_key_path + ".pub").is_file():
        pubkey = Path(root_ssh_key_path + ".pub").read_text().strip()
    elif Path.home().joinpath(".ssh", "id_rsa.pub").is_file():
        pubkey = Path.home().joinpath(".ssh", "id_rsa.pub").read_text().strip()

    log("- Customising '{}' via virt-customize ({}/{})".format(vm_name, myip, prefix))

    prepare_virt_customize(
        remote_host=remote_host, img_path=img, vm_name=vm_name,
        ip=myip, prefix=prefix, gw=mygw or "", dns=mydns or "",
        domain=mydomain or "", mac=mymac or "",
        pass_type=pass_type, pass_val=pass_val, pubkey=pubkey,
    )


def prepare_virt_customize(
    remote_host, img_path, vm_name,
    ip, prefix, gw, dns, domain, mac,
    pass_type, pass_val,
    pubkey=None,
):
    """
    Configure a QCOW2 image on the hypervisor via virt-customize.

    All guest inspection (virt-ls / virt-cat) and the virt-customize invocation
    run over SSH on remote_host.  Sensitive values are base64-encoded so they
    survive the SSH argument list without word-splitting.

    pass_type : "plain" or "crypted"
    pass_val  : plaintext password or crypt hash (e.g. $6$salt$hash)
    pubkey    : SSH public key string (the full "ssh-rsa AAAA… user@host" line), or None
    """
    log("Customising '{}' via virt-customize ({}/{})".format(vm_name, ip, prefix))

    pass_b64   = base64.b64encode(pass_val.encode()).decode()
    pubkey_b64 = base64.b64encode(pubkey.encode()).decode() if pubkey else ""

    # Build the Python snippet that runs on the hypervisor.
    # Values are embedded using repr() — produces valid Python literals regardless
    # of content (dollar signs, spaces, quotes, newlines).  No env vars, no SSH
    # argument quoting, no word-splitting risk.
    script = """\
import base64, os, re, subprocess, sys, tempfile

img       = {img!r}
vmname    = {vmname!r}
ip        = {ip!r}
prefix    = {prefix!r}
gw        = {gw!r}
dns       = {dns!r}
domain    = {domain!r}
mac       = {mac!r}
pass_type = {pass_type!r}
pass_b64  = {pass_b64!r}
pk_b64    = {pubkey_b64!r}
""".format(
        img=img_path, vmname=vm_name, ip=ip, prefix=str(prefix),
        gw=gw or "", dns=dns or "", domain=domain or "", mac=mac or "",
        pass_type=pass_type, pass_b64=pass_b64, pubkey_b64=pubkey_b64,
    ) + '''

def vls(path):
    r = subprocess.run(["virt-ls", "-a", img, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout.decode("utf-8","replace").splitlines() if r.returncode == 0 else []

def vcat(path):
    r = subprocess.run(["virt-cat", "-a", img, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout.decode("utf-8","replace") if r.returncode == 0 else ""

def _virt_exists(path):
    return subprocess.run(["virt-cat","-a",img,path],stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode==0

def detect_net():
    # netplan: only if the binary is installed — some cloud images drop .yaml templates without netplan
    if any(f.endswith((".yaml",".yml")) for f in vls("/etc/netplan")):
        if _virt_exists("/usr/sbin/netplan") or _virt_exists("/usr/bin/netplan"):
            return "netplan"
    # network-scripts before nm-keyfile: CentOS 7 has NM installed but uses ifcfg as primary
    if any(f.startswith("ifcfg-") and f!="ifcfg-lo" for f in vls("/etc/sysconfig/network-scripts")):
        return "network-scripts"
    # wicked: binary exists AND its service is enabled (in wants dirs).
    # SLES 16 may have the wicked binary as a compat package but NM is primary —
    # in that case wicked.service is not in the wants symlinks.
    if _virt_exists("/usr/sbin/wicked"):
        wants = (vls("/etc/systemd/system/network.target.wants") +
                 vls("/etc/systemd/system/multi-user.target.wants"))
        if any("wicked" in f.lower() for f in wants):
            return "wicked"
    # cloud-init+NM: SLES 16 uses cloud-init to configure NetworkManager — don't disable it
    if subprocess.run(["virt-ls","-a",img,"/etc/NetworkManager"],stdout=subprocess.PIPE,stderr=subprocess.PIPE).returncode==0:
        if _virt_exists("/usr/bin/cloud-init") or _virt_exists("/usr/sbin/cloud-init"):
            return "cloud-init"
        return "nm-keyfile"
    # wicked binary exists but NM absent and service not in wants — treat as wicked anyway
    if _virt_exists("/usr/sbin/wicked"):
        return "wicked"
    if "interfaces" in vls("/etc/network"): return "ifupdown"
    if any(f.endswith(".network") for f in vls("/etc/systemd/network")): return "systemd-networkd"
    return "unknown"

def _iface_from_udev():
    """Search udev rules in the image for a NAME= assignment matching our MAC."""
    ml = mac.lower()
    if not ml:
        return None
    for rdir in ["/etc/udev/rules.d", "/lib/udev/rules.d", "/usr/lib/udev/rules.d"]:
        for rf in vls(rdir):
            if not rf.endswith(".rules"):
                continue
            for ln in vcat("{}/{}".format(rdir, rf)).splitlines():
                if ml in ln.lower() and "NAME=" in ln:
                    for part in ln.split(","):
                        part = part.strip()
                        if part.startswith("NAME="):
                            return part.split("=",1)[1].strip().strip('"').strip("'")
    return None

def detect_iface(nt):
    ml, mu = mac.lower(), mac.upper()
    if nt == "wicked":
        # Prefer a file that already matches our MAC (LLADDR)
        fs = [f for f in vls("/etc/sysconfig/network") if f.startswith("ifcfg-") and f!="ifcfg-lo"]
        if ml:
            for f in fs:
                c = vcat("/etc/sysconfig/network/"+f)
                if "LLADDR" in c.upper() and ml in c.lower(): return f[len("ifcfg-"):]
        if fs: return fs[0][len("ifcfg-"):]
        # No existing files: try udev rules, else "eth0" (a .link file pins the name at boot)
        return _iface_from_udev() or "eth0"
    if nt == "network-scripts":
        fs = [f for f in vls("/etc/sysconfig/network-scripts") if f.startswith("ifcfg-") and f!="ifcfg-lo"]
        if mu:
            for f in fs:
                c = vcat("/etc/sysconfig/network-scripts/"+f)
                if "HWADDR" in c.upper() and mu in c.upper(): return f[len("ifcfg-"):]
        if fs: return fs[0][len("ifcfg-"):]
        return _iface_from_udev() or "eth0"
    if nt in ("nm-keyfile", "cloud-init"):
        cs = [f for f in vls("/etc/NetworkManager/system-connections") if not f.startswith(".")]
        if not cs: return "eth0"
        c = cs[0]; return c[:-len(".nmconnection")] if c.endswith(".nmconnection") else c
    if nt == "ifupdown":
        for ln in vcat("/etc/network/interfaces").splitlines():
            p = ln.split()
            if p[:1]==["iface"] and len(p)>=2 and p[1]!="lo": return p[1]
        return "eth0"
    if nt == "systemd-networkd":
        ns = [f for f in vls("/etc/systemd/network") if f.endswith(".network")]
        if ns:
            for ln in vcat("/etc/systemd/network/"+ns[0]).splitlines():
                if ln.startswith("Name="): return ln.split("=",1)[1].strip()
        return "eth0"
    if nt == "netplan":
        ys = [f for f in vls("/etc/netplan") if f.endswith((".yaml",".yml"))]
        if ys:
            in_e = False
            for ln in vcat("/etc/netplan/"+ys[0]).splitlines():
                if "ethernets:" in ln: in_e=True; continue
                if in_e and ln.startswith("    ") and not ln.startswith("     "):
                    return ln.strip().rstrip(":")
        return "eth0"
    return "eth0"

def net_config(nt, iface):
    extra = []
    chmod = None
    if nt == "wicked":
        ls = ["STARTMODE='auto'","BOOTPROTO='static'","IPADDR='{}/{}'".format(ip,prefix)]
        if mac:    ls.append("LLADDR='{}'".format(mac))
        if domain: ls.append("DOMAIN='{}'".format(domain))
        if gw: extra.append(("/etc/sysconfig/network/routes","default {} - -\\n".format(gw)))
        return "/etc/sysconfig/network/ifcfg-{}".format(iface), "\\n".join(ls)+"\\n", None, extra
    if nt == "network-scripts":
        ls = ["DEVICE={}".format(iface),"TYPE=Ethernet","BOOTPROTO=none","ONBOOT=yes"]
        if mac:    ls.append("HWADDR={}".format(mac))
        ls += ["IPADDR={}".format(ip),"PREFIX={}".format(prefix)]
        if gw:     ls.append("GATEWAY={}".format(gw))
        if dns:    ls.append("DNS1={}".format(dns))
        if domain: ls.append("DOMAIN={}".format(domain))
        return "/etc/sysconfig/network-scripts/ifcfg-{}".format(iface), "\\n".join(ls)+"\\n", None, extra
    if nt == "nm-keyfile":
        # Use mac-address match only — no interface-name — so NM applies the profile
        # regardless of the predictable interface name (enp1s0, eth0, etc.)
        ls = ["[connection]","id=lab-static","type=ethernet","autoconnect=true","","[ethernet]"]
        if mac: ls.append("mac-address={}".format(mac))
        ls += ["","[ipv4]","method=manual","addresses={}/{}".format(ip,prefix)]
        if gw:     ls.append("gateway={}".format(gw))
        if dns:    ls.append("dns={};".format(dns))
        if domain: ls.append("dns-search={};".format(domain))
        ls += ["","[ipv6]","method=disabled",""]
        return "/etc/NetworkManager/system-connections/lab-static.nmconnection", "\\n".join(ls), "0600", extra
    # cloud-init: network config is written via a NoCloud seed in the vc block — no direct file here
    if nt == "ifupdown":
        ls = ["auto lo","iface lo inet loopback","",
              "auto {}".format(iface),"iface {} inet static".format(iface),
              "    address {}/{}".format(ip,prefix)]
        if gw:     ls.append("    gateway {}".format(gw))
        if dns:    ls.append("    dns-nameservers {}".format(dns))
        if domain: ls.append("    dns-search {}".format(domain))
        return "/etc/network/interfaces", "\\n".join(ls)+"\\n", None, extra
    if nt == "systemd-networkd":
        ls = ["[Match]","MACAddress={}".format(mac) if mac else "Name={}".format(iface),
              "","[Network]","Address={}/{}".format(ip,prefix)]
        if gw:     ls.append("Gateway={}".format(gw))
        if dns:    ls.append("DNS={}".format(dns))
        if domain: ls.append("Domains={}".format(domain))
        return "/etc/systemd/network/10-{}.network".format(iface), "\\n".join(ls)+"\\n", None, extra
    if nt == "netplan":
        ys = [f for f in vls("/etc/netplan") if f.endswith((".yaml",".yml"))]
        npf = ys[0] if ys else "50-lab.yaml"
        ls = ["network:","  version: 2","  ethernets:","    {}:".format(iface),
              "      dhcp4: no","      addresses:","        - {}/{}".format(ip,prefix)]
        if gw: ls.append("      gateway4: {}".format(gw))
        ls.append("      nameservers:")
        if dns:    ls.append("        addresses: [{}]".format(dns))
        if domain: ls.append("        search: [{}]".format(domain))
        return "/etc/netplan/{}".format(npf), "\\n".join(ls)+"\\n", None, extra
    return None, None, None, extra

with tempfile.TemporaryDirectory(prefix="vc_") as tmp:
    nt    = detect_net()
    iface = detect_iface(nt)
    print("    net config: {}, interface: {}".format(nt, iface), flush=True)

    pass_val = base64.b64decode(pass_b64).decode()
    cred_f = os.path.join(tmp, "root_cred")
    open(cred_f,"w").write(pass_val)

    vc = ["virt-customize","-a",img,"--hostname",vmname,
          "--upload","{}:/tmp/.vc_rc".format(cred_f)]

    if pass_type == "plain":
        vc += ["--run-command",
               r'printf "root:%s\\n" "$(cat /tmp/.vc_rc)" | chpasswd; rm -f /tmp/.vc_rc']
    else:
        vc += ["--run-command",
               r"""awk 'BEGIN{FS=OFS=":"; getline h < "/tmp/.vc_rc"; close("/tmp/.vc_rc")}"""
               r""" /^root:/{$2=h}1' /etc/shadow > /tmp/.vc_sh"""
               r""" && cat /tmp/.vc_sh > /etc/shadow && rm -f /tmp/.vc_sh /tmp/.vc_rc"""
               r""" && awk 'BEGIN{FS=OFS=":"} /^root:/ && $2!="x" && $2!=""{$2="x"}1'"""
               r""" /etc/passwd > /tmp/.vc_pw"""
               r""" && cat /tmp/.vc_pw > /etc/passwd && rm -f /tmp/.vc_pw"""]

    if pk_b64:
        pubkey = base64.b64decode(pk_b64).decode()
        ak_f = os.path.join(tmp, "authorized_keys")
        open(ak_f,"w").write(pubkey)
        vc += ["--ssh-inject","root:file:{}".format(ak_f)]

    if nt in ("wicked", "network-scripts") and mac:
        # Write a systemd .link file to pin the interface name by MAC.
        # NAME= in udev rules is not supported for net renaming in modern systemd;
        # .link files are the correct mechanism (processed by systemd-udevd before wicked starts).
        link_content = "[Match]\\nMACAddress={}\\n\\n[Link]\\nName={}\\n".format(mac.lower(), iface)
        link_f = os.path.join(tmp, "10-lab.link")
        open(link_f, "w").write(link_content)
        vc += ["--run-command", "mkdir -p /etc/systemd/network"]
        vc += ["--upload", "{}:/etc/systemd/network/10-lab.link".format(link_f)]

    if nt == "wicked":
        # Find DHCP ifcfg files in Python (avoids shell quoting issues with regex)
        # and remove them one by one with a simple rm command per file
        dhcp_pat = re.compile(r"BOOTPROTO\s*=\s*\S*(dhcp|auto)", re.IGNORECASE)
        for _f in vls("/etc/sysconfig/network"):
            if not _f.startswith("ifcfg-") or _f == "ifcfg-lo":
                continue
            _content = vcat("/etc/sysconfig/network/" + _f)
            if dhcp_pat.search(_content):
                vc += ["--run-command", "rm -f /etc/sysconfig/network/{}".format(_f)]

    if nt in ("nm-keyfile", "cloud-init"):
        # Prevent NM from auto-generating "Wired connection 1" DHCP for unmatched interfaces
        nm_nodauto_f = os.path.join(tmp, "nm_no_auto")
        open(nm_nodauto_f, "w").write("[main]\\nno-auto-default=*\\n")
        vc += ["--run-command", "mkdir -p /etc/NetworkManager/conf.d /etc/NetworkManager/system-connections"]
        vc += ["--upload", "{}:/etc/NetworkManager/conf.d/99-no-auto-default.conf".format(nm_nodauto_f)]
        for _f in vls("/etc/NetworkManager/system-connections"):
            if _f.startswith("."): continue
            vc += ["--run-command", "rm -f '/etc/NetworkManager/system-connections/{}'".format(_f)]

    if nt == "cloud-init":
        # SLES 16 uses cloud-init + NetworkManager. Use cloud-init properly: write a NoCloud
        # seed with network-config v2 and force the NM renderer so netplan is never called.

        # Remove existing netplan YAMLs — they would trigger the netplan renderer whose
        # binary does not exist on SLES 16 (NM reads them directly without the CLI).
        for _yf in vls("/etc/netplan"):
            if _yf.endswith((".yaml", ".yml")):
                vc += ["--run-command", "rm -f '/etc/netplan/{}'".format(_yf)]
        # Remove legacy sysconfig routes — cloud-init fails parsing "default" as an IP address.
        vc += ["--run-command",
               "rm -f /etc/sysconfig/network/routes /etc/sysconfig/network/ifroute-* 2>/dev/null; true"]

        # Build network-config v2: use 0.0.0.0/0 (not "default") for the default route.
        _nc = ["version: 2", "ethernets:", "  id0:"]
        if mac:
            _nc += ["    match:", "      macaddress: \\"{}\\"".format(mac.lower())]
        _nc += ["    dhcp4: false", "    addresses:", "      - {}/{}".format(ip, prefix)]
        if gw:
            _nc += ["    routes:", "      - to: 0.0.0.0/0", "        via: {}".format(gw)]
        if dns or domain:
            _nc.append("    nameservers:")
            if dns:    _nc.append("      addresses: [{}]".format(dns))
            if domain: _nc.append("      search: [{}]".format(domain))

        _seed = "/var/lib/cloud/seed/nocloud"
        _nc_f  = os.path.join(tmp, "ci-network-config")
        open(_nc_f, "w").write("\\n".join(_nc) + "\\n")
        _meta_f = os.path.join(tmp, "ci-meta-data")
        open(_meta_f, "w").write("instance-id: lab-{}\\nlocal-hostname: {}\\n".format(vmname, vmname))
        _ud_f = os.path.join(tmp, "ci-user-data")
        open(_ud_f, "w").write("#cloud-config\\n")
        vc += ["--run-command", "mkdir -p {}".format(_seed)]
        vc += ["--upload", "{}:{}/network-config".format(_nc_f, _seed)]
        vc += ["--upload", "{}:{}/meta-data".format(_meta_f, _seed)]
        vc += ["--upload", "{}:{}/user-data".format(_ud_f, _seed)]

        # Force NoCloud datasource and NM renderer — prevents cloud-init from using netplan.
        _ci_f = os.path.join(tmp, "ci-lab-cfg")
        open(_ci_f, "w").write(
            "datasource_list: ['NoCloud', 'None']\\n"
            "system_info:\\n  network:\\n    renderers: ['network-manager']\\n"
        )
        vc += ["--run-command", "mkdir -p /etc/cloud/cloud.cfg.d"]
        vc += ["--upload", "{}:/etc/cloud/cloud.cfg.d/99-lab.cfg".format(_ci_f)]

    dest, content, chmod, extras = net_config(nt, iface)
    if dest and content:
        net_f = os.path.join(tmp, "net")
        open(net_f,"w").write(content)
        vc += ["--run-command","mkdir -p {}".format(os.path.dirname(dest))]
        vc += ["--upload","{}:{}".format(net_f,dest)]
        if chmod: vc += ["--chmod","{}:{}".format(chmod,dest)]

    for edest, econtent in extras:
        ef = os.path.join(tmp, os.path.basename(edest))
        open(ef,"w").write(econtent)
        vc += ["--upload","{}:{}".format(ef,edest)]

    if nt == "wicked" and (dns or domain):
        dl = []
        if dns:    dl.append("NETCONFIG_DNS_STATIC_SERVERS='{}'".format(dns))
        if domain: dl.append("NETCONFIG_DNS_STATIC_SEARCHLIST='{}'".format(domain))
        dns_f = os.path.join(tmp,"netconfig_dns")
        open(dns_f,"w").write("\\n".join(dl)+"\\n")
        vc += ["--upload","{}:/tmp/.vc_ncdns".format(dns_f)]
        vc += ["--run-command",
               r"""awk 'BEGIN{FS=OFS="="}"""
               r""" /^NETCONFIG_DNS_STATIC_SERVERS=/ || /^NETCONFIG_DNS_STATIC_SEARCHLIST=/ {next}"""
               r""" 1' /etc/sysconfig/network/config > /tmp/.vc_cfg"""
               r""" && cat /tmp/.vc_ncdns >> /tmp/.vc_cfg"""
               r""" && cat /tmp/.vc_cfg > /etc/sysconfig/network/config"""
               r""" && rm -f /tmp/.vc_cfg /tmp/.vc_ncdns"""]
        vc += ["--run-command","netconfig update -f 2>/dev/null || true"]

    fqdn = "{}.{}".format(vmname, domain) if domain else vmname
    vc += ["--run-command",
           "grep -qF ' {}' /etc/hosts || echo '{} {} {}' >> /etc/hosts".format(
               vmname, ip, vmname, fqdn)]
    vc += ["--run-command",
           "systemctl enable sshd 2>/dev/null || systemctl enable ssh 2>/dev/null || true"]
    vc += ["--run-command",
           r"""mkdir -p /etc/ssh/sshd_config.d"""
           r""" && printf 'PermitRootLogin yes\nPasswordAuthentication yes\n'"""
           r""" > /etc/ssh/sshd_config.d/99-lab.conf"""
           r""" ; sed -i -E"""
           r""" -e 's/^#?\s*PermitRootLogin\s+.*/PermitRootLogin yes/'"""
           r""" -e 's/^#?\s*PasswordAuthentication\s+.*/PasswordAuthentication yes/'"""
           r""" /etc/ssh/sshd_config 2>/dev/null; true"""]
    if nt != "cloud-init":
        # For non-cloud-init systems: disable cloud-init entirely so it doesn't interfere.
        ci_dis_f = os.path.join(tmp, "ci_disabled")
        open(ci_dis_f, "w").write("")
        vc += ["--run-command", "mkdir -p /etc/cloud"]
        vc += ["--upload", "{}:/etc/cloud/cloud-init.disabled".format(ci_dis_f)]

    if "config" in vls("/etc/selinux"):
        vc += ["--selinux-relabel"]

    print("    running virt-customize...", flush=True)
    r = subprocess.run(vc)
    sys.exit(r.returncode)
'''

    # Pipe the script to the hypervisor — all values are already baked in as
    # repr() literals so the remote python3 needs no env vars or extra args.
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "root@{}".format(remote_host), "python3 -"],
        input=script.encode(),
    )
    if result.returncode != 0:
        die("virt-customize failed for '{}'".format(vm_name))


def prepare_install_iso(
    vm_name, lab_setup_path, install_type, iso_image,
    mymac, myip, mymask, mygw, mydns, mydomain,
    root_pwd_hash, root_ssh_key=None,
):
    """
    Render the answer file (AutoYaST/Kickstart/Preseed/autoinstall) for a VM.
    Mirrors prepare_install_iso (bash).

    The rendered file is written to lab_setup_path/install_iso/, already
    served over HTTP by the automation VM's web server — nothing needs to be
    copied to the hypervisor.

    NOTE on ROOT_SSH_KEY vs ROOT_SSH_PUBKEY (a pre-existing bash inconsistency,
    preserved as-is since it isn't actually broken): `lab_creation.cfg`
    documents ROOT_SSH_KEY as the literal pubkey CONTENT (see
    templates/lab_creation.cfg.example: "REPLACE ME with cat ~/.ssh/<key>.pub"),
    and that's exactly how the kickstart/autoyast templates use it
    ($ROOT_SSH_KEY echoed straight into authorized_keys). But this function
    (and prepare_virt_customize_for_vm) ALSO probes "${ROOT_SSH_KEY}.pub" as if
    it were a file PATH — which, given ROOT_SSH_KEY normally holds key
    content rather than a path, is never actually a real file, so that branch
    is always false in practice and this always falls through to
    ~/.ssh/id_rsa.pub for ROOT_SSH_PUBKEY (used by the preseed template).
    Since admins are instructed to set ROOT_SSH_KEY to their id_rsa.pub
    content anyway, both end up injecting the same key in practice — kept
    faithful to bash rather than "fixed", since nothing is actually corrupted.

    NOTE on ROOT_PWD_HASH: bash used to escape '$' in the hash before this
    point — verified empirically (see lab_creation.bash's prepare_install_iso)
    that this corrupted the hash. Fixed in bash; the raw hash is used
    directly here, with no escaping needed at all.
    """
    itype = resolve_install_type(install_type, iso_image)

    root_ssh_pubkey = ""
    if root_ssh_key and Path(root_ssh_key + ".pub").is_file():
        root_ssh_pubkey = Path(root_ssh_key + ".pub").read_text().strip()
    elif Path.home().joinpath(".ssh", "id_rsa.pub").is_file():
        root_ssh_pubkey = Path.home().joinpath(".ssh", "id_rsa.pub").read_text().strip()

    log("- Rendering {} answer file for \"{}{}{}\"".format(itype, _RED, vm_name, _RESET))

    if itype == "autoinstall":
        # Ubuntu 22+ subiquity autoinstall — written directly (not via
        # process_template/eval) to keep this well away from any shell
        # re-interpretation of the password hash.
        out_dir = Path(lab_setup_path) / "install_iso" / vm_name
        out_dir.mkdir(parents=True, exist_ok=True)
        user_data = (
            "#cloud-config\n"
            "autoinstall:\n"
            "  version: 1\n"
            "  locale: en_US.UTF-8\n"
            "  keyboard:\n"
            "    layout: us\n"
            "  network:\n"
            "    network:\n"
            "      version: 2\n"
            "      ethernets:\n"
            "        id0:\n"
            "          match:\n"
            "            macaddress: {mymac}\n"
            "          set-name: eth0\n"
            "          dhcp4: no\n"
            "          addresses:\n"
            "            - {myip}/{mymask}\n"
            "          gateway4: {mygw}\n"
            "          nameservers:\n"
            "            addresses: [{mydns}]\n"
            "            search: [{mydomain}]\n"
            "  storage:\n"
            "    layout:\n"
            "      name: lvm\n"
            "  user-data:\n"
            "    disable_root: false\n"
            "    ssh_pwauth: true\n"
            "    users:\n"
            "      - name: root\n"
            "        lock_passwd: false\n"
            "        hashed_passwd: \"{root_pwd_hash}\"\n"
            "        ssh_authorized_keys:\n"
            "          - \"{root_ssh_pubkey}\"\n"
            "  ssh:\n"
            "    install-server: true\n"
            "    allow-pw: true\n"
            "  late-commands:\n"
            "    - mkdir -p /target/etc/ssh/sshd_config.d\n"
            "    - printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > /target/etc/ssh/sshd_config.d/99-lab.conf\n"
        ).format(
            mymac=mymac, myip=myip, mymask=mymask, mygw=mygw, mydns=mydns, mydomain=mydomain,
            root_pwd_hash=root_pwd_hash, root_ssh_pubkey=root_ssh_pubkey,
        )
        (out_dir / "user-data").write_text(user_data)
        (out_dir / "meta-data").write_text(
            "instance-id: {}\nlocal-hostname: {}\n".format(vm_name, vm_name))
    else:
        ext = {"autoyast": "xml", "kickstart": "ks", "preseed": "preseed"}.get(itype)
        tpl = Path(lab_setup_path) / "install_iso" / "template_{}".format(itype)
        out = Path(lab_setup_path) / "install_iso" / "{}.{}".format(vm_name, ext)
        if not tpl.is_file():
            die("install_iso template not found: {}".format(tpl))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(process_template(str(tpl), {
            "ROOT_PWD_HASH": root_pwd_hash,
            "ROOT_SSH_KEY": root_ssh_key or "",
            "ROOT_SSH_PUBKEY": root_ssh_pubkey,
            "_vm_name": vm_name,
            "myip": myip, "mymask": mymask, "mygw": mygw,
            "mydns": mydns, "mydomain": mydomain, "mymac": mymac,
        }))


# ── Helm ──────────────────────────────────────────────────────────────────────

def setup_helm(hostname, clu_name, online=False, automation_host="automation"):
    """
    Install Helm on a remote K8s node (mirrors setup_helm).

    online=True  → downloads directly from GitHub.
    online=False → downloads from a local automation VM.

    NOTE: default is False (offline/automation-VM path) to match bash's real
    default — bash checks `[[ "$online" == "1" ]]`, and the `online` JSON
    field is absent from every real lab config found in this repo, so an
    unset bash variable (empty string) takes the offline branch. An earlier
    version of this function defaulted to True, inverting that behaviour for
    any caller that didn't explicitly pass online=.
    """
    log("Setting up Helm on cluster '{}'".format(clu_name))
    if online:
        ssh_run(hostname,
                "curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash")
    else:
        ssh_run(hostname, "curl http://{}/helm/install_helm.sh | bash -".format(automation_host))


def helm_repo_add(hostname, repo_name, repo_url):
    """Add and update a Helm repository on a remote node (mirrors helm_repo_add)."""
    log("Adding Helm repo '{}'".format(repo_name))
    ssh_run(hostname, "helm repo add {} {}".format(repo_name, repo_url))
    ssh_run(hostname, "helm repo update")


# ── Template processing ───────────────────────────────────────────────────────

def process_template(template_file, variables):
    """
    Render a template file exactly as bash's process_templates does:
    `eval "cat <<EOF\n$(cat template_file)\nEOF\n"` — i.e. full unquoted-heredoc
    expansion: $VAR / ${VAR} substitution AND $(...) / `...` command
    substitution, using the given variables as environment.

    Shells out to bash to run that exact construct rather than hand-porting
    heredoc/eval semantics in Python. This matters: some templates (e.g.
    cloud-init.template_user-data, install_iso.template_kickstart) use $(...)
    command substitution, which Python's string.Template cannot express at
    all — a previous version of this function used string.Template and would
    have silently left those command substitutions un-expanded. Shelling out
    is the only way to guarantee identical output without a way to test this
    live.

    Args:
        template_file : path to the template file.
        variables     : dict of variable name -> value, exported into the
                         subshell's environment for the expansion.

    Returns the expanded text.
    """
    script = 'eval "cat <<EOF\n$(cat {})\nEOF\n"'.format(shlex.quote(str(template_file)))
    env = os.environ.copy()
    env.update({k: "" if v is None else str(v) for k, v in variables.items()})
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        die("process_template failed on '{}': {}".format(template_file, result.stderr.strip()))
    return result.stdout


# ── Salt ──────────────────────────────────────────────────────────────────────

def setup_salt(vm_name, salt_states, lab_setup_path):
    """
    Create a salt-ssh roster and state files for a VM (mirrors setup_salt).

    salt_states : Space-separated list of state names.
    """
    salt_dir = Path.home() / "salt-ssh"
    (salt_dir / "states").mkdir(parents=True, exist_ok=True)

    (salt_dir / "roster").write_text(
        "managed:\n"
        "  host: {}\n"
        "  user: root\n"
        "  sudo: False\n"
        "  priv: {}/.ssh/id_rsa\n".format(vm_name, Path.home())
    )

    for state in salt_states.split():
        tmpl = Path(lab_setup_path) / "salt-ssh" / state
        out  = salt_dir / "states" / state
        out.write_text(process_template(str(tmpl), {"_vm_name": vm_name}))


# ── OS detection (local) ──────────────────────────────────────────────────────

def find_os():
    """
    Return (os_id, version_id, arch) for the local machine (mirrors find_OS).
    Reads /etc/os-release and uname -m.
    """
    os_id = version_id = ""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            m = re.match(r'^ID="?([^"]+)"?', line)
            if m:
                os_id = m.group(1)
            m = re.match(r'^VERSION_ID="?([^"]+)"?', line)
            if m:
                version_id = m.group(1)
    except IOError:
        pass
    arch = subprocess.run(
        ["uname", "-m"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True
    ).stdout.strip()
    return os_id, version_id, arch


def boot_packages_for(os_id):
    """
    Return the space-separated BOOT_PACKAGES list for a detected OS id, or
    raise via die() for unsupported OSes. Mirrors install_packages (bash).

    NOTE: bash's install_packages only ever assigns the BOOT_PACKAGES variable
    — nothing in the bash codebase reads it afterwards (verified with grep);
    it's effectively dead code today. Kept here as a pure function (returning
    the value bash would have assigned) rather than a no-op, since a future
    caller may still want it and there's no behavioural risk either way.
    """
    if os_id == "sles":
        return "vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
    elif os_id == "sle-micro":
        return "vim-small iptables NetworkManager-cloud-setup wget git"
    elif os_id == "opensuse-leap":
        return "vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
    else:
        die("ERROR - OS not supported yet")


# ── Misc ──────────────────────────────────────────────────────────────────────

def check_exists(needle, haystack):
    """Return True if needle is a whole word in the space-separated haystack (mirrors check_exists)."""
    return " {} ".format(needle) in " {} ".format(haystack)


def reboot_vm(virt_srv, vm_name):
    """
    Reboot a VM, forcing a power cycle if it doesn't respond within 120s.
    Mirrors reboot_vm (bash).
    """
    subprocess.run(["virsh", "-c", virt_srv, "reboot", vm_name], check=False)
    event = subprocess.run(
        ["virsh", "-c", virt_srv, "event", vm_name, "--event", "lifecycle", "--timeout", "120"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if event.returncode != 0:
        log("- \"{}{}{}\" did not reboot — forcing power cycle".format(_RED, vm_name, _RESET))
        subprocess.run(["virsh", "-c", virt_srv, "reset", vm_name], check=False)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run(cmd, error_msg):
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die(error_msg)


def _remote(hostname, cmd, check=True):
    return ssh_run(hostname, cmd, check=check)


def _remote_dns_add(server, zone_file, record):
    _remote(server,
            "grep -qF '{}' {} 2>/dev/null || echo '{}' >> {}".format(
                record, zone_file, record, zone_file))


def _dns_add_line(zone_file, record):
    """
    Append `record` to zone_file unless a line exactly equal to it already
    exists. Mirrors the intent of bash's `grep -qi <pattern> || echo <record>
    >> zone_file` dedup checks in add_to_dns/add_dns_to_named_rr.

    NOTE: an earlier version of this took a separate `dedup_key` substring
    (e.g. just the short hostname or the last IP octet) and checked whether
    that fragment appeared ANYWHERE in the file — which is a real false-
    positive risk given this project's hostnames (node1/node10/node101,
    n1/n10/n11, …): adding "node1" would wrongly be skipped as a duplicate
    once "node10" was already present, since "node1" is a substring of
    "node10". This project's actual bash also had a related but different
    bug here (see lab_creation.bash's add_to_dns — a stray quote character
    meant the PTR dedup check never matched anything, causing duplicates
    instead of false-skips). Exact-line matching on the full record avoids
    both failure modes.
    """
    zone_file = Path(zone_file)
    zone_file.touch()
    lines = zone_file.read_text().splitlines()
    if record not in lines:
        zone_file.write_text("\n".join(lines + [record]) + "\n")


def _dns_remove_line(zone_file, record):
    """
    Remove any line exactly equal to `record` from zone_file. Mirrors bash's
    `sed "/<full record text>/d"` calls in del_from_dns/add_service_dns, which
    always passed the complete record text as the delete pattern (not a bare
    hostname/octet fragment) — exact-line matching here preserves that same
    precision, avoiding substring false-positives (see _dns_add_line's note).
    """
    zone_file = Path(zone_file)
    if zone_file.exists():
        lines = [l for l in zone_file.read_text().splitlines() if l != record]
        zone_file.write_text("\n".join(lines) + "\n")


def _dns_append_line(zone_file, record):
    zone_file = Path(zone_file)
    zone_file.touch()
    with zone_file.open("a") as f:
        f.write(record + "\n")


def _detect_dns():
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            m = re.match(r'^nameserver\s+([\d.]+)', line)
            if m:
                return m.group(1)
    except IOError:
        pass
    return ""


def _detect_gateway():
    result = subprocess.run(
        ["ip", "route", "list", "to", "default"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, check=False,
    )
    m = re.search(r'via\s+([\d.]+)', result.stdout)
    return m.group(1) if m else ""


def _detect_domain():
    import socket as _socket
    fqdn  = _socket.getfqdn()
    short = _socket.gethostname().split(".")[0]
    return fqdn[len(short) + 1:] if fqdn.startswith(short + ".") else ""


def _reverse_ip(ip):
    parts = ip.split(".")
    return ".".join(reversed(parts[:-1])) if len(parts) == 4 else ""


def _detect_netmask():
    """
    Return the CIDR prefix length of the default-route network device.
    Mirrors load_vm_vars' mymask auto-detect (bash used `ip -o -f inet addr
    show ${_default_dev} | egrep ... | ipcalc -p ... | cut -d= -f2` — same
    end result via a shorter path: the device is the same one carrying the
    default route, already needed for _detect_gateway, and its prefix length
    is directly in `ip -o addr show` output, no ipcalc round-trip needed).
    """
    route = subprocess.run(
        ["ip", "route", "list", "to", "default"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, check=False,
    ).stdout
    dev_match = re.search(r"\bdev\s+(\S+)", route)
    if not dev_match:
        return ""
    addr = subprocess.run(
        ["ip", "-o", "-f", "inet", "addr", "show", dev_match.group(1)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, check=False,
    ).stdout
    m = re.search(r"inet\s+[\d.]+/(\d{1,2})", addr)
    return m.group(1) if m else ""
