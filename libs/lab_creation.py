"""
lab_creation.py — VM management, DNS, Helm, SSH, templates, and variable loading.

Python equivalent of lab_creation.bash.

Typical usage:
    from lab_creation import (
        ssh_run, ssh_output, wait_for_ssh,
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
import os
import re
import socket
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ── Output helpers ────────────────────────────────────────────────────────────

_RED    = "\033[1;91m"
_YELLOW = "\033[1;33m"
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


def wait_for_ssh(hostname, timeout=300, interval=5):
    """
    Poll TCP port 22 on hostname until it accepts a connection (mirrors check_ssh_conn).

    Raises RuntimeError if the timeout is exceeded.
    """
    log("Waiting for {}{}{} to come online …".format(_RED, hostname, _RESET))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((hostname, 22), timeout=3)
            s.close()
            log("{}{}{} is online".format(_RED, hostname, _RESET))
            return
        except OSError:
            time.sleep(interval)
    raise RuntimeError("Timeout ({}s) exceeded waiting for {}".format(timeout, hostname))


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

    return vars


# ── VM management ─────────────────────────────────────────────────────────────

def copy_vm_image(remote_host, iso_loc, iso_image, vm_img_loc, vm_name, vm_dsk_gb):
    """
    Copy a QCOW2 source image and resize it on the hypervisor (mirrors copy_vm_img).
    """
    log("Copying image for VM '{}'".format(vm_name))
    _remote(remote_host, "cp {}/{} {}/{}.qcow2".format(iso_loc, iso_image, vm_img_loc, vm_name))
    log("Resizing to {}G".format(vm_dsk_gb))
    _remote(remote_host, "qemu-img resize -f qcow2 {}/{}.qcow2 {}G".format(vm_img_loc, vm_name, vm_dsk_gb))


def create_vm(
    virt_srv, vm_name, vm_cpu, vm_mem, vm_dsk_gb, vm_img_loc, network,
    os_variant="slem5.4", boot="uefi", config_method="",  # boot: "uefi", "firmware=bios", "hd", …
    lab_setup_path="/srv/www/htdocs/lab_creation",
    extra_disks=None, extra_filesystems=None,
    ign_file=None, com_file=None, salt_states="",
):
    """
    Create a VM on a KVM hypervisor via virt-install (mirrors create_vm).

    config_method:
        ""           → Ignition + Combustion (SLE Micro default)
        "cloud-init" → cloud-init ISO
    """
    log("Creating VM '{}'".format(vm_name))

    # Normalise boot flag: "uefi=off" / "bios" / "legacy" → "firmware=bios"
    _BIOS_ALIASES = {"uefi=off", "bios", "legacy"}
    boot_flag = "firmware=bios" if boot in _BIOS_ALIASES else boot

    extra_disk_args = []
    for dsk in (extra_disks or []):
        extra_disk_args += ["--disk", "path={}".format(dsk.split(",")[0])]

    extra_fs_args = []
    for fs in (extra_filesystems or []):
        extra_fs_args += ["--filesystem", fs]

    base_args = [
        "virt-install", "--connect", virt_srv,
        "--name", vm_name, "--autostart",
        "--boot", boot_flag, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
        "--os-variant", os_variant, "--import",
        "--disk", "size={},path={}/{}.qcow2,sparse=no,boot.order=1".format(
            vm_dsk_gb, vm_img_loc, vm_name),
        "--graphics", "spice,listen=0.0.0.0",
        "--network", network, "--noautoconsole",
    ] + extra_fs_args + extra_disk_args

    if not config_method:
        ign = ign_file or vm_name
        com = com_file or vm_name
        qemu_args = (
            "-fw_cfg name=opt/com.coreos/config,"
            "file={}/ignition/{} "
            "-fw_cfg name=opt/org.opensuse.combustion/script,"
            "file={}/combustion/{}".format(lab_setup_path, ign, lab_setup_path, com)
        )
        _run(base_args + ["--qemu-commandline", qemu_args],
             "virt-install failed for '{}'".format(vm_name))

    elif config_method == "cloud-init":
        ci_iso = "{}/{}_ci.iso".format(vm_img_loc, vm_name)
        _run(base_args + ["--disk", "{},device=cdrom".format(ci_iso)],
             "virt-install (cloud-init) failed for '{}'".format(vm_name))

        log("Waiting 3 minutes for cloud-init …")
        time.sleep(180)

        if salt_states:
            setup_salt(vm_name, salt_states, lab_setup_path)

        subprocess.run(["virsh", "--connect", virt_srv,
                        "change-media", vm_name, "--eject", ci_iso], check=False)
        subprocess.run(["virsh", "--connect", virt_srv, "reboot", vm_name], check=False)


def delete_vm(virt_srv, vm_name):
    """Remove a VM and all its storage from the hypervisor (mirrors delete_vm)."""
    log("Deleting VM '{}'".format(vm_name))
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

    for server in (remote_dns_servers or []):
        _remote_dns_add(server, "{}/{}.lan".format(NAMED_ZONE_DIR, mydomain), a_record)
        _remote_dns_add(server, "{}/{}.db".format(NAMED_ZONE_DIR, mynet_reverse), ptr_record)
        _remote(server, "systemctl restart named")

    _dns_add_line(NAMED_ZONE_DIR / "{}.lan".format(mydomain), a_record, short)
    _dns_add_line(NAMED_ZONE_DIR / "{}.db".format(mynet_reverse), ptr_record, last_octet)
    restart_named()


def del_from_dns(vm_name, myip, mydomain, mynet_reverse, remote_dns_servers=None):
    """Remove forward and reverse DNS records for a VM (mirrors del_from_dns)."""
    log("Removing DNS entry for '{}'".format(vm_name))
    short      = vm_name.split(".")[0]
    last_octet = myip.split(".")[-1]

    for server in (remote_dns_servers or []):
        _remote(server, "sed '/{}/d' -i {}/{}.db".format(last_octet, NAMED_ZONE_DIR, mynet_reverse))
        _remote(server, "sed '/{}/d' -i {}/{}.lan".format(short, NAMED_ZONE_DIR, mydomain))
        _remote(server, "systemctl restart named")

    _dns_remove_line(NAMED_ZONE_DIR / "{}.db".format(mynet_reverse), last_octet)
    _dns_remove_line(NAMED_ZONE_DIR / "{}.lan".format(mydomain), short)
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


# ── Provisioning files ────────────────────────────────────────────────────────

def prepare_ignition_combustion(
    vm_name, lab_setup_path, root_pwd_hash, root_ssh_key,
    mysource, sourcepath, mydns, myip, mymask, mygw,
    suse_email="", suse_regcode="", suse_url="",
):
    """
    Create Ignition + Combustion provisioning files for a VM
    (mirrors prepare_ign_and_cmb).
    """
    base = Path(lab_setup_path)
    log("Creating Ignition + Combustion files for '{}'".format(vm_name))

    ign_out = base / "ignition" / "{}.ign".format(vm_name)
    text = (base / "ignition" / "template").read_text()
    text = (text
            .replace("TEMPLATE_HN", vm_name)
            .replace("ROOT_PWD_HASH", root_pwd_hash)
            .replace("ROOT_SSH_KEY", root_ssh_key))
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
    Create cloud-init user-data, network-config, and meta-data files
    (mirrors prepare_cloud-init).

    variables : dict of values to substitute into the template files.
    """
    base = Path(lab_setup_path) / "cloud-init"
    log("Creating cloud-init files for '{}'".format(vm_name))
    for kind in ("user-data", "network-config", "meta-data"):
        tmpl = base / "template_{}".format(kind)
        out  = base / "{}_{}".format(vm_name, kind)
        out.write_text(process_template(str(tmpl), variables))


# ── virt-customize ───────────────────────────────────────────────────────────

def _virt_ls(img, path):
    """List entries inside a guest image path. Returns [] on failure."""
    r = subprocess.run(["virt-ls", "-a", img, path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout.decode("utf-8", "replace").splitlines() if r.returncode == 0 else []


def _virt_cat(img, path):
    """Read a file from inside a guest image. Returns '' on failure."""
    r = subprocess.run(["virt-cat", "-a", img, path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def _vc_detect_net_type(img):
    if any(f.endswith((".yaml", ".yml")) for f in _virt_ls(img, "/etc/netplan")):
        return "netplan"
    nm_conns = _virt_ls(img, "/etc/NetworkManager/system-connections")
    if any(not f.startswith(".") for f in nm_conns):
        return "nm-keyfile"
    if subprocess.run(["virt-ls", "-a", img, "/etc/sysconfig/network"],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0:
        return "wicked"
    ns = _virt_ls(img, "/etc/sysconfig/network-scripts")
    if any(f.startswith("ifcfg-") and f != "ifcfg-lo" for f in ns):
        return "network-scripts"
    if "interfaces" in _virt_ls(img, "/etc/network"):
        return "ifupdown"
    if any(f.endswith(".network") for f in _virt_ls(img, "/etc/systemd/network")):
        return "systemd-networkd"
    return "unknown"


def _vc_detect_iface(img, net_type, mac):
    """Return the interface name to configure, matched by MAC where possible."""
    mac_lower = mac.lower() if mac else ""
    mac_upper = mac.upper() if mac else ""

    if net_type == "wicked":
        ifcfg = [f for f in _virt_ls(img, "/etc/sysconfig/network")
                 if f.startswith("ifcfg-") and f != "ifcfg-lo"]
        if mac_lower:
            for f in ifcfg:
                c = _virt_cat(img, "/etc/sysconfig/network/{}".format(f))
                if "LLADDR" in c.upper() and mac_lower in c.lower():
                    return f[len("ifcfg-"):]
        return ifcfg[0][len("ifcfg-"):] if ifcfg else "lab"

    if net_type == "network-scripts":
        ifcfg = [f for f in _virt_ls(img, "/etc/sysconfig/network-scripts")
                 if f.startswith("ifcfg-") and f != "ifcfg-lo"]
        if mac_upper:
            for f in ifcfg:
                c = _virt_cat(img, "/etc/sysconfig/network-scripts/{}".format(f))
                if "HWADDR" in c.upper() and mac_upper in c.upper():
                    return f[len("ifcfg-"):]
        return ifcfg[0][len("ifcfg-"):] if ifcfg else "eth0"

    if net_type == "nm-keyfile":
        conns = [f for f in _virt_ls(img, "/etc/NetworkManager/system-connections")
                 if not f.startswith(".")]
        c = conns[0]; return c[:-len(".nmconnection")] if c.endswith(".nmconnection") else c

    if net_type == "ifupdown":
        for line in _virt_cat(img, "/etc/network/interfaces").splitlines():
            parts = line.split()
            if parts[:1] == ["iface"] and len(parts) >= 2 and parts[1] != "lo":
                return parts[1]
        return "eth0"

    if net_type == "systemd-networkd":
        nets = [f for f in _virt_ls(img, "/etc/systemd/network") if f.endswith(".network")]
        if nets:
            for line in _virt_cat(img, "/etc/systemd/network/{}".format(nets[0])).splitlines():
                if line.startswith("Name="):
                    return line.split("=", 1)[1].strip()
        return "eth0"

    if net_type == "netplan":
        yamls = [f for f in _virt_ls(img, "/etc/netplan")
                 if f.endswith((".yaml", ".yml"))]
        if yamls:
            in_eth = False
            for line in _virt_cat(img, "/etc/netplan/{}".format(yamls[0])).splitlines():
                if "ethernets:" in line:
                    in_eth = True
                    continue
                if in_eth and line.startswith("    ") and not line.startswith("     "):
                    return line.strip().rstrip(":")
        return "eth0"

    return "eth0"


def _vc_net_config(net_type, iface, mac, ip, prefix, gw, dns, domain):
    """Return (dest, content, chmod_str_or_None, extra_files).

    extra_files: list of (dest, content) tuples for wicked routes.
    """
    extra = []
    chmod = None

    if net_type == "wicked":
        lines = ["STARTMODE='auto'", "BOOTPROTO='static'",
                 "IPADDR='{}/{}'".format(ip, prefix)]
        if mac:
            lines.append("LLADDR='{}'".format(mac))
        if domain:
            lines.append("DOMAIN='{}'".format(domain))
        if gw:
            extra.append(("/etc/sysconfig/network/routes",
                          "default {} - -\n".format(gw)))
        return ("/etc/sysconfig/network/ifcfg-{}".format(iface),
                "\n".join(lines) + "\n", chmod, extra)

    if net_type == "network-scripts":
        lines = ["DEVICE={}".format(iface), "TYPE=Ethernet",
                 "BOOTPROTO=none", "ONBOOT=yes"]
        if mac:
            lines.append("HWADDR={}".format(mac))
        lines += ["IPADDR={}".format(ip), "PREFIX={}".format(prefix)]
        if gw:     lines.append("GATEWAY={}".format(gw))
        if dns:    lines.append("DNS1={}".format(dns))
        if domain: lines.append("DOMAIN={}".format(domain))
        return ("/etc/sysconfig/network-scripts/ifcfg-{}".format(iface),
                "\n".join(lines) + "\n", chmod, extra)

    if net_type == "nm-keyfile":
        lines = ["[connection]", "id={}".format(iface), "type=ethernet",
                 "interface-name={}".format(iface), "autoconnect=true",
                 "", "[ethernet]"]
        if mac: lines.append("mac-address={}".format(mac))
        lines += ["", "[ipv4]", "method=manual",
                  "addresses={}/{}".format(ip, prefix)]
        if gw:     lines.append("gateway={}".format(gw))
        if dns:    lines.append("dns={};".format(dns))
        if domain: lines.append("dns-search={};".format(domain))
        lines += ["", "[ipv6]", "method=disabled", ""]
        return ("/etc/NetworkManager/system-connections/{}.nmconnection".format(iface),
                "\n".join(lines), "0600", extra)

    if net_type == "ifupdown":
        lines = ["auto lo", "iface lo inet loopback", "",
                 "auto {}".format(iface), "iface {} inet static".format(iface),
                 "    address {}/{}".format(ip, prefix)]
        if gw:     lines.append("    gateway {}".format(gw))
        if dns:    lines.append("    dns-nameservers {}".format(dns))
        if domain: lines.append("    dns-search {}".format(domain))
        return ("/etc/network/interfaces", "\n".join(lines) + "\n", chmod, extra)

    if net_type == "systemd-networkd":
        lines = ["[Match]",
                 "MACAddress={}".format(mac) if mac else "Name={}".format(iface),
                 "", "[Network]", "Address={}/{}".format(ip, prefix)]
        if gw:     lines.append("Gateway={}".format(gw))
        if dns:    lines.append("DNS={}".format(dns))
        if domain: lines.append("Domains={}".format(domain))
        return ("/etc/systemd/network/10-{}.network".format(iface),
                "\n".join(lines) + "\n", chmod, extra)

    if net_type == "netplan":
        yamls = [f for f in _virt_ls("", "/etc/netplan")
                 if f.endswith((".yaml", ".yml"))]
        npfile = yamls[0] if yamls else "50-lab.yaml"
        lines = ["network:", "  version: 2", "  ethernets:",
                 "    {}:".format(iface), "      dhcp4: no",
                 "      addresses:", "        - {}/{}".format(ip, prefix)]
        if gw: lines.append("      gateway4: {}".format(gw))
        lines.append("      nameservers:")
        if dns:    lines.append("        addresses: [{}]".format(dns))
        if domain: lines.append("        search: [{}]".format(domain))
        return ("/etc/netplan/{}".format(npfile), "\n".join(lines) + "\n", chmod, extra)

    return (None, None, None, extra)


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


# ── Helm ──────────────────────────────────────────────────────────────────────

def setup_helm(hostname, clu_name, online=True, automation_host="automation"):
    """
    Install Helm on a remote K8s node (mirrors setup_helm).

    online=True  → downloads directly from GitHub.
    online=False → downloads from a local automation VM.
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
    Expand $VAR and ${VAR} placeholders in a template file (mirrors process_templates).

    Uses Python's string.Template with safe_substitute so unknown variables are
    left as-is rather than raising an error.

    Args:
        template_file : Path to the template file.
        variables     : Dict of variable names to values.

    Returns the expanded string.
    """
    text = Path(template_file).read_text()
    return string.Template(text).safe_substitute(variables)


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


# ── Misc ──────────────────────────────────────────────────────────────────────

def check_exists(needle, haystack):
    """Return True if needle is a whole word in the space-separated haystack (mirrors check_exists)."""
    return " {} ".format(needle) in " {} ".format(haystack)


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


def _dns_add_line(zone_file, record, dedup_key):
    zone_file = Path(zone_file)
    zone_file.touch()
    text = zone_file.read_text()
    if dedup_key not in text:
        zone_file.write_text(text.rstrip() + "\n" + record + "\n")


def _dns_remove_line(zone_file, pattern):
    zone_file = Path(zone_file)
    if zone_file.exists():
        lines = [l for l in zone_file.read_text().splitlines() if pattern not in l]
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
