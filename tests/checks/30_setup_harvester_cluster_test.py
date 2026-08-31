#!/usr/bin/env python3
# Unit tests for scripts/setup_harvester_cluster.py (new, 2026-08-30) — the
# PXE-based Harvester HCI cluster bootstrap script. No podman/root/real
# network needed: urllib fetches, virt-install, and the VIP-wait poll are
# all mocked; template rendering uses the REAL bash-eval process_template()
# against the real shipped templates, so a real substitution bug would
# still be caught. Run from 30_setup_harvester_cluster.sh, in its own
# container — see tests/run_tests.sh.
import json
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import lab_creation as lc  # noqa: E402
import setup_harvester_cluster as shc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


_CLUSTER_CFG = {
    "harvester_version": "v1.7.1",
    "token": "test-token",
    "ssh_authorized_keys": ["ssh-rsa AAAAtest key1", "ssh-rsa AAAAtest key2"],
    "password_hash": "$6$fakehash",
    "management_interface": "ens3",
    "netmask": "255.255.255.0",
    "gateway": "192.168.88.1",
    "dns_nameservers": ["192.168.88.73", "192.168.88.1"],
    "hypervisor_bridge": "br0",
    "vip": "192.168.88.142",
    "device": "/dev/vda",
    "_templ_addons_loc": str(_REPO / "templates" / "addons"),
}

_CREATE_NODE = {"name": "harvester1.mydemo.lab", "mac": "52:54:00:AB:CD:E1", "ip": "192.168.88.143", "role": "create"}
_JOIN_NODE = {"name": "harvester2.mydemo.lab", "mac": "52:54:00:ab:cd:e2", "ip": "192.168.88.144", "role": "join"}

# ── _fetch_release_assets(): idempotent, correct URLs ────────────────────────
fetched = []


def _fake_urlretrieve(url, dest):
    fetched.append(url)
    Path(dest).write_bytes(b"fake")


shc.urllib.request.urlretrieve = _fake_urlretrieve

with tempfile.TemporaryDirectory() as tmp:
    dest_dir = Path(tmp) / "harvester" / "v1.7.1"
    shc._fetch_release_assets("v1.7.1", dest_dir)
    check("_fetch_release_assets downloads all 4 real Harvester release assets",
          fetched == [
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-amd64.iso",
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-vmlinuz-amd64",
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-initrd-amd64",
              "https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-rootfs-amd64.squashfs",
          ])
    check("_fetch_release_assets creates the destination directory",
          dest_dir.is_dir())

    fetched.clear()
    shc._fetch_release_assets("v1.7.1", dest_dir)
    check("_fetch_release_assets does not re-download assets already present",
          fetched == [])

# ── _render_node_files(): real template rendering, "create" role ────────────
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    url = shc._render_node_files(_CLUSTER_CFG, _CREATE_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    check("_render_node_files returns this node's own ipxe script URL",
          url == "http://10.0.0.1/lab_creation/harvester/v1.7.1/ipxe-harvester1.mydemo.lab")

    ipxe_text = (web_root / "ipxe-harvester1.mydemo.lab").read_text()
    check("rendered ipxe script starts with #!ipxe", ipxe_text.startswith("#!ipxe"))
    check("rendered ipxe script references the real vmlinuz/initrd/squashfs asset names",
          "harvester-v1.7.1-vmlinuz-amd64" in ipxe_text
          and "harvester-v1.7.1-initrd-amd64" in ipxe_text
          and "harvester-v1.7.1-rootfs-amd64.squashfs" in ipxe_text)
    check("rendered ipxe script's config_url points at this node's own rendered config file",
          "harvester.install.config_url=http://10.0.0.1/lab_creation/harvester/v1.7.1/config-harvester1.mydemo.lab.yaml"
          in ipxe_text)

    config_text = (web_root / "config-harvester1.mydemo.lab.yaml").read_text()
    check("rendered create config has mode: create", "mode: create" in config_text)
    check("rendered create config has this node's own hostname/IP (not DHCP-guessed)",
          "hostname: harvester1.mydemo.lab" in config_text and "ip: 192.168.88.143" in config_text)
    check("rendered create config carries the cluster VIP", "vip: 192.168.88.142" in config_text)
    check("rendered create config includes both SSH keys, one per line",
          "  - ssh-rsa AAAAtest key1" in config_text and "  - ssh-rsa AAAAtest key2" in config_text)
    check("rendered create config still requires iso_url even under PXE (real Harvester requirement)",
          "iso_url: http://10.0.0.1/lab_creation/harvester/v1.7.1/harvester-v1.7.1-amd64.iso" in config_text)
    check("rendered create config includes dns_nameservers — confirmed live 2026-08-30 that Harvester's "
          "own installer refuses to proceed without it for a static-IP management_interface "
          "('Invalid configuration: DNS servers are required for static IP address')",
          "dns_nameservers:" in config_text
          and "  - 192.168.88.73" in config_text and "  - 192.168.88.1" in config_text)

# _render_node_files() dies clearly if dns_nameservers is missing, rather than
# rendering a config that would fail partway through a real install.
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    no_dns_cfg = dict(_CLUSTER_CFG)
    del no_dns_cfg["dns_nameservers"]
    try:
        shc._render_node_files(no_dns_cfg, _CREATE_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
        check("_render_node_files dies clearly when dns_nameservers is missing", False)
    except SystemExit:
        check("_render_node_files dies clearly when dns_nameservers is missing", True)

# ── _render_node_files(): "join" role points at the VIP, not itself ─────────
with tempfile.TemporaryDirectory() as tmp:
    web_root = Path(tmp)
    shc._render_node_files(_CLUSTER_CFG, _JOIN_NODE, "http://10.0.0.1/lab_creation/harvester/v1.7.1", web_root)
    config_text = (web_root / "config-harvester2.mydemo.lab.yaml").read_text()
    check("rendered join config has mode: join", "mode: join" in config_text)
    check("rendered join config's server_url points at the cluster VIP",
          "server_url: https://192.168.88.142:443" in config_text)
    check("rendered join config has the join node's own hostname/IP",
          "hostname: harvester2.mydemo.lab" in config_text and "ip: 192.168.88.144" in config_text)

# ── _create_netboot_vm(): correct disk-before-network boot order, no OS image ──
captured_calls = []


def _fake_run_libvirt_tool(binary, remote_host, virt_srv, args, **kwargs):
    captured_calls.append((binary, remote_host, virt_srv, list(args)))
    return lc.subprocess.CompletedProcess(args=[], returncode=0)


shc.run_libvirt_tool = _fake_run_libvirt_tool
vm_cluster_cfg = dict(_CLUSTER_CFG, vm_cpu=12, vm_mem=36864, vm_dsk=260)
shc._create_netboot_vm(_CREATE_NODE, vm_cluster_cfg, {"REMOTE_HOST": "hv1.mydemo.lab", "VM_IMG_LOC": "/var/lib/libvirt/images"})

check("_create_netboot_vm calls virt-install exactly once", len(captured_calls) == 1)
binary, remote_host, virt_srv, args = captured_calls[0]
check("_create_netboot_vm invokes virt-install (not virsh)", binary == "virt-install")
check("_create_netboot_vm passes the configured hypervisor as remote_host", remote_host == "hv1.mydemo.lab")
boot_arg = args[args.index("--boot") + 1]
check("_create_netboot_vm's --boot flag selects the plain (non-secure-boot) OVMF loader/nvram — "
      "confirmed live 2026-08-30 that virt-install's bare 'uefi' shorthand auto-selected the "
      "SECURE BOOT OVMF variant, which silently blocks loading an unsigned ipxe.efi ('Access Denied')",
      "--boot" in args and boot_arg.startswith("uefi,loader=")
      and "ovmf-x86_64-code.bin" in boot_arg and "ovmf-x86_64-vars.bin" in boot_arg
      and "ovmf-x86_64-ms-" not in boot_arg)
disk_arg = next(a for a in args if "path=" in a)
net_arg = next(a for a in args if "mac.address=" in a)
check("_create_netboot_vm's disk has boot.order=1 (avoids the ISO path's reboot-loop bug)",
      "boot.order=1" in disk_arg)
check("_create_netboot_vm's network device has boot.order=2 — confirmed live 2026-08-30 that "
      "giving the disk a boot.order without also giving the network device one produces a domain "
      "with no usable network boot entry at all (per-device boot.order excludes any device "
      "without one, silently dropping --boot's hd,network device-order tokens)",
      "boot.order=2" in net_arg)
check("_create_netboot_vm creates a blank disk (no --import, no source OS image)",
      "--import" not in args)
check("_create_netboot_vm wires the node's own MAC into the network arg",
      any("mac.address=52:54:00:AB:CD:E1" in a for a in args if a.startswith("--network") or "bridge=" in a))

# cluster_cfg's own hypervisor_host/hypervisor_virt_srv override lab_creation.cfg's
# REMOTE_HOST/VIRT_SRV — needed since a Harvester cluster can reasonably target a
# different hypervisor than whatever "normal" lab VMs currently use.
captured_calls.clear()
override_cfg = dict(vm_cluster_cfg, hypervisor_host="nuc6.mydemo.lab",
                     hypervisor_virt_srv="qemu+ssh://root@nuc6.mydemo.lab/system")
shc._create_netboot_vm(_CREATE_NODE, override_cfg, {"REMOTE_HOST": "hv1.mydemo.lab", "VIRT_SRV": "qemu+ssh://root@hv1.mydemo.lab/system"})
_, override_remote_host, override_virt_srv, _ = captured_calls[0]
check("cluster_cfg's hypervisor_host overrides lab_creation.cfg's REMOTE_HOST",
      override_remote_host == "nuc6.mydemo.lab")
check("cluster_cfg's hypervisor_virt_srv overrides lab_creation.cfg's VIRT_SRV",
      override_virt_srv == "qemu+ssh://root@nuc6.mydemo.lab/system")

# A failed virt-install must die(), not silently continue.
captured_calls.clear()


def _failing_run_libvirt_tool(binary, remote_host, virt_srv, args, **kwargs):
    return lc.subprocess.CompletedProcess(args=[], returncode=1)


shc.run_libvirt_tool = _failing_run_libvirt_tool
try:
    shc._create_netboot_vm(_CREATE_NODE, vm_cluster_cfg, {})
    check("a failed virt-install call raises SystemExit via die()", False)
except SystemExit:
    check("a failed virt-install call raises SystemExit via die()", True)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_harvester_cluster checks passed")
