#!/usr/bin/env python3.11
# Part of lab-in-a-box — stands up a Harvester HCI cluster via PXE netboot,
# using PXEService's new "ipxe-uefi" mode (libs/services.py). This is NOT a
# lab-JSON addon: standing up the Harvester cluster itself is infrastructure
# bootstrap (the same category as setup_kvm_node.py — provisioning the
# hypervisor a lab later runs on top of), not "define a lab", so it takes
# its own small config file instead of growing lab.json's schema.
#
# Design + the real Harvester PXE requirements behind this script were
# researched from Harvester's own docs (docs.harvesterhci.io) and the
# harvester/ipxe-examples repo's libvirt-specific PXE guide (a real,
# already-nested-KVM-VM walkthrough, i.e. this project's exact environment)
# — see TODO's "Give Harvester itself a lab-standard, repeatable install
# path" entry for the full research notes and the lessons learned live-
# testing the ISO-based install path this supersedes.
#
# Real findings that shaped this design:
#   - Harvester publishes vmlinuz/initrd/rootfs.squashfs as separate
#     release assets (releases.rancher.com/harvester/<version>/...)
#     alongside the ISO — no loop-mount/kernel-extraction dance needed,
#     unlike the ISO-based install path.
#   - `harvester.install.iso_url` is still required even under PXE (the
#     installer fetches the full ISO separately from the live squashfs) —
#     confirmed in harvester/ipxe-examples' own config-create.yaml.
#   - A `--boot uefi,hd,network` boot order (disk before network) makes the
#     reboot-loop problem the ISO-based path hit (a persistent
#     `--boot kernel=/initrd=` domain override) simply not exist: the first
#     boot (empty disk) falls through to PXE, every boot after install uses
#     the VM's own bootloader and never touches PXE again.
#   - dnsmasq (already this project's PXE engine) supports the two-stage
#     iPXE UEFI handshake natively via dhcp-userclass tagging — no new PXE
#     technology needed, just a second PXEService config mode.
#
# Usage:
#   setup_harvester_cluster.py <cluster.json>
#
# <cluster.json> is a small, standalone config — NOT a lab.json — see
# templates/harvester-cluster.json.example for every key.
__version__ = "fcbef10"

import subprocess
import sys
import urllib.request
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import services  # noqa: E402
from lab_creation import (  # noqa: E402
    log, die, run_libvirt_tool, check_ssh_conn, process_template, ssh_run, purge_known_host,
    yaml_scalar as _yaml_scalar,
)

# Real Harvester release-asset naming, confirmed via github.com/harvester/
# harvester's own releases page — one ISO plus 3 separate boot files per
# version, all under the same releases.rancher.com path.
_RELEASE_BASE = "https://releases.rancher.com/harvester"
_ASSETS = ("amd64.iso", "vmlinuz-amd64", "initrd-amd64", "rootfs-amd64.squashfs")


def _fetch_release_assets(version, dest_dir):
    """Idempotently download this version's 4 release assets."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for suffix in _ASSETS:
        fname = "harvester-{}-{}".format(version, suffix)
        dest = dest_dir / fname
        if dest.exists():
            log("- {} already present, skipping download".format(fname))
            continue
        url = "{}/{}/{}".format(_RELEASE_BASE, version, fname)
        log("- downloading {}".format(url))
        try:
            urllib.request.urlretrieve(url, str(dest))
        except OSError as e:
            die("failed to download {}: {}".format(url, e))


# _yaml_scalar (used below) is lab_creation.yaml_scalar, imported above —
# moved there 2026-09-05 after finding the identical unescaped-YAML-value
# bug in lab_creation.prepare_install_iso()'s Ubuntu autoinstall
# cloud-config, so both places share one implementation instead of two
# copies of the same fix.


def _build_system_settings_block(cluster_cfg):
    """
    Optional top-level `system_settings:` — a generic passthrough for any
    Harvester Setting overridable at install time (docs.harvesterhci.io's
    config reference lists this as a top-level key, alongside scheme_
    version/token). Returns "" (nothing) when cluster.json's
    "system_settings" is omitted, so this is a no-op unless the operator
    opts in. See _apply_post_install_settings() below for the
    post-install-only counterpart — which of Harvester's Settings actually
    work at install time vs. only post-install isn't fully mapped out (see
    TODO's "improvement of the Harvester installer" entry), so both
    mechanisms are offered rather than guessing.
    """
    settings = cluster_cfg.get("system_settings")
    if not settings:
        return ""
    lines = ["system_settings:"]
    for key, value in settings.items():
        lines.append("  {}: {}".format(key, _yaml_scalar(value)))
    return "\n".join(lines) + "\n"


def _build_os_extra_lines(cluster_cfg):
    """
    Optional os.* keys beyond what every cluster always sets (hostname/
    ssh_authorized_keys/password/ntp_servers/dns_nameservers — handled
    directly in _render_node_files, always present). Returns a fully-
    indented block ending in its own trailing newline, or "" if nothing to
    add — process_template() has no conditionals of its own, so this is
    the same "build the block in Python, substitute one marker" pattern
    the existing ssh_keys_block/dns_nameservers_block already use.
    """
    lines = []
    environment = cluster_cfg.get("os_environment")
    if environment:
        lines.append("  environment:")
        for key, value in environment.items():
            lines.append("    {}: {}".format(key, _yaml_scalar(value)))
    return "\n".join(lines) + "\n" if lines else ""


def _build_install_extra_lines(cluster_cfg, node):
    """
    Optional install.* keys beyond what every cluster always sets. Same
    block-substitution pattern as _build_os_extra_lines(). `node` supplies
    the one genuinely per-node option (harvester_role) — deliberately a
    different cluster.json key from the existing per-node "role"
    (create/join, an install MODE) to avoid confusing the two: Harvester's
    own "install.role" is a different axis entirely (default/management/
    worker/witness — node classification within an already-decided
    create/join cluster topology).
    """
    lines = []
    if cluster_cfg.get("data_disk"):
        lines.append("  data_disk: {}".format(cluster_cfg["data_disk"]))
    if "wipe_all_disks" in cluster_cfg:
        lines.append("  wipe_all_disks: {}".format(_yaml_scalar(bool(cluster_cfg["wipe_all_disks"]))))
    for key in ("cluster_pod_cidr", "cluster_service_cidr", "cluster_dns"):
        if cluster_cfg.get(key):
            lines.append("  {}: {}".format(key, cluster_cfg[key]))
    if node.get("harvester_role"):
        lines.append("  role: {}".format(node["harvester_role"]))
    # Real, high-value knob for THIS project specifically: Harvester
    # defaults to a 3-replica StorageClass, which silently degrades (or
    # never reaches Healthy) on a cluster with fewer than 3 nodes — exactly
    # the shape of templates/harvester-cluster.json.example's own 2-node
    # (create+join) sample.
    replica_count = cluster_cfg.get("storage_class_replica_count")
    if replica_count is not None:
        lines.append("  harvester:")
        lines.append("    storage_class:")
        lines.append("      replica_count: {}".format(int(replica_count)))
    return "\n".join(lines) + "\n" if lines else ""


def _render_node_files(cluster_cfg, node, http_base, web_root):
    """
    Render this node's own config-<name>.yaml (Harvester's install-config
    scheme) and ipxe-<name> boot script — one pair PER NODE rather than one
    shared script per role: every other node type in this project is always
    statically addressed (myip/mymac are explicit, never DHCP-guessed), so
    an explicit per-node hostname/IP config is the natural fit here too,
    not a per-role one with hostname/IP omitted for DHCP to fill in.

    Returns the HTTP URL of the rendered ipxe script — what PXEService's
    ipxe-uefi mode needs per node (its "pxe_ipxe_url" key).
    """
    templ_dir = Path(cluster_cfg["_templ_addons_loc"]) / "harvester_pxe"
    version = cluster_cfg["harvester_version"]
    keys_block = "\n".join("  - {}".format(k) for k in cluster_cfg["ssh_authorized_keys"])

    ntp_servers = cluster_cfg.get("ntp_servers") or ["0.suse.pool.ntp.org", "1.suse.pool.ntp.org"]

    common_vars = {
        "node_hostname": node["name"],
        "node_ip": node["ip"],
        "harvester_ssh_keys_block": keys_block,
        "harvester_password_hash": cluster_cfg["password_hash"],
        "harvester_token": cluster_cfg["token"],
        "harvester_iface": cluster_cfg["management_interface"],
        "harvester_netmask": cluster_cfg["netmask"],
        "harvester_gateway": cluster_cfg["gateway"],
        "harvester_ntp_servers_block": "\n".join("  - {}".format(s) for s in ntp_servers),
        "harvester_system_settings_block": _build_system_settings_block(cluster_cfg),
        "harvester_os_extra_lines": _build_os_extra_lines(cluster_cfg),
        "harvester_install_extra_lines": _build_install_extra_lines(cluster_cfg, node),
        # Required by Harvester itself for static IP, not merely
        # recommended: confirmed live 2026-08-30, the installer's own
        # config-validation step refuses to proceed at all ("Invalid
        # configuration: DNS servers are required for static IP address")
        # without this — dies below if the operator forgot it, rather than
        # rendering a config that fails partway through a real install.
        "harvester_dns_nameservers_block": "\n".join(
            "  - {}".format(d) for d in (cluster_cfg.get("dns_nameservers") or
                                          die("cluster config has no 'dns_nameservers' — required by "
                                              "Harvester's own installer for a static-IP management_interface"))),
        "harvester_device": cluster_cfg.get("device", "/dev/vda"),
        "harvester_iso_url": "{}/harvester-{}-amd64.iso".format(http_base, version),
        "harvester_vip": cluster_cfg["vip"],
        "harvester_vip_mode": cluster_cfg.get("vip_mode", "static"),
        "harvester_persistent_size": cluster_cfg.get("persistent_partition_size", "150Gi"),
    }

    role_template = "config-create.yaml.tmpl" if node["role"] == "create" else "config-join.yaml.tmpl"
    config_text = process_template(str(templ_dir / role_template), common_vars)
    config_name = "config-{}.yaml".format(node["name"])
    (web_root / config_name).write_text(config_text)

    ipxe_vars = {
        "harvester_http_base": http_base,
        "harvester_version": version,
        "harvester_config_url": "{}/{}".format(http_base, config_name),
    }
    ipxe_text = process_template(str(templ_dir / "ipxe.tmpl"), ipxe_vars)
    ipxe_name = "ipxe-{}".format(node["name"])
    (web_root / ipxe_name).write_text(ipxe_text)

    return "{}/{}".format(http_base, ipxe_name)


def _create_netboot_vm(node, cluster_cfg, config):
    """
    Define and start a VM with an empty disk (boot.order=1) and a network
    device (boot.order=2) — disk before network, so the first (empty-disk)
    boot falls through to PXE and every boot after install uses the
    freshly-installed disk instead. This is what makes the ISO-based
    install path's reboot-loop workaround (install.poweroff + virsh
    undefine/redefine) unnecessary here: no persistent kernel-boot override
    is ever set on the domain.

    Both boot.order values are required, not just the disk's: confirmed
    live 2026-08-30 that giving the disk alone a boot.order (with `--boot
    uefi,hd,network`'s device-order tokens left as-is) produces a domain
    with NO usable network boot entry at all ("No bootable option or
    device was found") — once any device specifies libvirt's per-device
    boot.order, it takes over from the global <os><boot dev=.../> list
    entirely, silently dropping the "hd,network" tokens on the floor. The
    `--boot` flag below is now just "uefi" (firmware selection only); the
    actual disk-before-network precedence comes entirely from the two
    boot.order values.

    Explicit non-secure-boot OVMF loader/nvram paths, not just "uefi":
    confirmed live 2026-08-30 that virt-install's plain `--boot uefi`
    shorthand auto-selected the SECURE BOOT OVMF variant for os-variant
    "generic" on this host — the domain came up with secure-boot enabled,
    which silently blocks loading ipxe.efi at all ("Access Denied": it
    isn't signed with a certificate enrolled in this VM's Secure Boot DB).
    harvester/ipxe-examples' own libvirt guide sidesteps this by pointing
    at the plain (non "-ms-") OVMF files directly, which this mirrors.
    """
    # cluster_cfg's own "hypervisor_*" keys take priority over
    # lab_creation.cfg's REMOTE_HOST/VIRT_SRV/VM_IMG_LOC: a Harvester
    # cluster is reasonably built on a different hypervisor than whatever
    # host that shared config currently points at for "normal" lab VMs
    # (confirmed a real, not hypothetical, concern live — this project's own
    # automation VM had REMOTE_HOST pointed at an unrelated host during
    # this feature's live test).
    remote_host = cluster_cfg.get("hypervisor_host") or config.get("REMOTE_HOST", "")
    virt_srv = cluster_cfg.get("hypervisor_virt_srv") or config.get("VIRT_SRV", "qemu:///system")
    vm_img_loc = cluster_cfg.get("vm_img_loc") or config.get("VM_IMG_LOC", "/var/lib/libvirt/images")

    # Same known_hosts purge setup_lab.py/destroy_lab.py already do for every
    # normal lab VM, applied here too: this project's lab IPs get reused
    # across many disposable test VMs over time, so a stale host key from
    # whatever PREVIOUSLY held this IP would otherwise make ssh_run() refuse
    # to connect ("REMOTE HOST IDENTIFICATION HAS CHANGED") — confirmed live
    # 2026-09-04, the first time anything in this script actually SSHed into
    # a freshly-created node (_fetch_harvester_kubeconfig() below): the node
    # itself was up and sshd was genuinely answering with a real host key,
    # StrictHostKeyChecking=accept-new still refused it outright because an
    # unrelated older VM's key for the same IP was already on file.
    purge_known_host(node["ip"], node["name"])

    args = [
        "--name", node["name"], "--autostart",
        "--boot", "uefi,loader={0},loader.readonly=yes,loader.type=pflash,"
        "nvram.template={1}".format(
            cluster_cfg.get("ovmf_code", "/usr/share/qemu/ovmf-x86_64-code.bin"),
            cluster_cfg.get("ovmf_vars", "/usr/share/qemu/ovmf-x86_64-vars.bin")),
        "--vcpus", str(cluster_cfg.get("vm_cpu", 12)),
        "--memory", str(cluster_cfg.get("vm_mem", 36864)),
        "--os-variant", cluster_cfg.get("os_variant", "generic"),
        "--disk", "size={},path={}/{}.qcow2,sparse=no,bus=virtio,boot.order=1".format(
            cluster_cfg.get("vm_dsk", 260), vm_img_loc, node["name"]),
        "--network", "bridge={},mac.address={},model=virtio,boot.order=2".format(
            cluster_cfg.get("hypervisor_bridge", "br0"), node["mac"]),
        "--graphics", "spice,listen=0.0.0.0", "--noautoconsole",
    ]
    r = run_libvirt_tool("virt-install", remote_host, virt_srv, args)
    if r.returncode != 0:
        die("virt-install failed for '{}'".format(node["name"]))


def _fetch_harvester_kubeconfig(cluster_cfg, create_node):
    """
    Fetch a real kubeconfig from the 'create' node once the cluster is
    Active, so _apply_post_install_settings() below can reach it via
    kubectl — mirrors libs/backends.py's HarvesterBackend, which also
    expects a local kubeconfig file (HARVESTER_KUBECONFIG) rather than
    reaching the cluster any other way.

    Harvester's default admin user is "rancher", not root — root SSH is
    disabled by Harvester's own default hardening (confirmed live
    2026-08-30 during HarvesterBackend's own live test: "PermitRootLogin
    no, AllowGroups admin — root isn't in that group"). The kubeconfig
    itself is the standard RKE2 path Harvester's underlying Kubernetes
    distribution always writes — but "rancher" (though it has NOPASSWD:ALL
    sudo — confirmed live 2026-09-04) can't read that root-owned 0600 file
    directly; a first live-test attempt without `sudo` here got a plain
    "Permission denied", not an SSH failure.

    LIVE-TESTED 2026-09-04 against a real single-node cluster
    (harvtest1.mydemo.lab, nuc6): the fetched kubeconfig's server: line was
    genuinely https://127.0.0.1:6443 as assumed, the rewritten VIP:6443
    address was genuinely reachable, and `kubectl get nodes` against it
    came back Ready. Also surfaced a real, previously-latent gap this was
    the first code path to ever trigger: _create_netboot_vm() never purged
    stale known_hosts entries the way setup_lab.py/destroy_lab.py already
    do for every normal lab VM — fixed there (see its own comment) once a
    reused lab IP's old host key made ssh_run() refuse this connection
    outright even though sshd was genuinely up and answering correctly.
    """
    kubeconfig_text = ssh_run(
        create_node["ip"], "sudo cat /etc/rancher/rke2/rke2.yaml", user="rancher", capture=True).stdout
    # rke2.yaml points at 127.0.0.1 by default — rewrite to the cluster's
    # own VIP so the fetched kubeconfig is usable from the automation VM,
    # not just from the node itself.
    kubeconfig_text = kubeconfig_text.replace(
        "https://127.0.0.1:6443", "https://{}:6443".format(cluster_cfg["vip"]))
    dest = Path(cluster_cfg.get("kubeconfig_path") or
                "/etc/lab_creation/harvester-{}.kubeconfig".format(cluster_cfg["harvester_version"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(kubeconfig_text)
    dest.chmod(0o600)
    return dest


def _apply_post_install_settings(cluster_cfg, kubeconfig_path):
    """
    Apply cluster.json's optional "post_install_settings" dict as
    harvesterhci.io/v1beta1 Setting objects — the post-install counterpart
    to _build_system_settings_block()'s install-time passthrough, for
    whichever Settings turn out not to be settable at install time. Same
    "operator pre-configures it, we don't validate Setting semantics"
    stance throughout this file.
    """
    settings = cluster_cfg.get("post_install_settings")
    if not settings:
        return
    for name, value in settings.items():
        manifest = (
            "apiVersion: harvesterhci.io/v1beta1\n"
            "kind: Setting\n"
            "metadata:\n"
            "  name: {}\n"
            "value: {}\n"
        ).format(name, _yaml_scalar(value))
        log("- applying Harvester Setting '{}'".format(name))
        r = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig_path), "apply", "-f", "-"],
            input=manifest, universal_newlines=True)
        if r.returncode != 0:
            die("failed to apply Harvester Setting '{}'".format(name))


def main():
    if len(sys.argv) < 2:
        print("Usage: {} <cluster.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)

    cluster_cfg = primary.load_definition(sys.argv[1])
    defaults = primary.load_defaults()
    config = primary.load_config()

    cluster_cfg["_templ_addons_loc"] = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")

    # Harvester's release assets are multi-GB (the ISO alone is ~7-8GB) —
    # confirmed live 2026-08-30 that the default LAB_SETUP_PATH location can
    # sit on a disk far too small for that (this project's own automation
    # VM had well under 5GB free there). "web_root"/"http_base" let a
    # deployment point this at wherever it actually keeps large source
    # images and serves them over HTTP instead (e.g. the same tree
    # ISO_LOC-based installs already use) — both default to today's
    # LAB_SETUP_PATH-based behavior when omitted.
    web_root = Path(cluster_cfg["web_root"]) if cluster_cfg.get("web_root") else \
        Path(lab_setup_path) / "harvester" / cluster_cfg["harvester_version"]

    if cluster_cfg.get("http_base"):
        http_base = cluster_cfg["http_base"]
    else:
        # The netbooting VMs need to reach THIS automation VM over HTTP (it
        # serves the rendered configs/scripts/release assets) — NOT the
        # hypervisor (config's REMOTE_HOST), which is a different machine
        # and runs no HTTP server for any of this. "mysource" is
        # lab_creation.cfg's own existing key for "the hostname of the lab
        # automation server".
        http_host = cluster_cfg.get("automation_ip") or config.get("mysource")
        if not http_host:
            die("no 'mysource' in lab_creation.cfg and no 'automation_ip' set in the cluster config — "
                "need a real address the netbooting VMs can reach this automation VM at")
        http_base = "http://{}/lab_creation/harvester/{}".format(http_host, cluster_cfg["harvester_version"])

    if not any(n.get("role") == "create" for n in cluster_cfg["nodes"]):
        die("cluster config has no node with role 'create' — exactly one is required to bootstrap the cluster")

    log("Fetching Harvester {} release assets".format(cluster_cfg["harvester_version"]))
    _fetch_release_assets(cluster_cfg["harvester_version"], web_root)

    log("Rendering per-node install config + iPXE boot scripts")
    definition_nodes = {}
    for node in cluster_cfg["nodes"]:
        ipxe_url = _render_node_files(cluster_cfg, node, http_base, web_root)
        definition_nodes[node["name"]] = {"mymac": node["mac"], "pxe_ipxe_url": ipxe_url}

    definition = {
        "nodes": definition_nodes,
        "pxe": {
            "pxe_mode": "ipxe-uefi",
            # NOT the same as hypervisor_bridge above: this is the
            # interface PXEService's dnsmasq binds to on THIS automation
            # VM (where it actually runs), not the hypervisor's own bridge
            # name for the VM's network device — confirmed live 2026-08-30
            # these are genuinely different values whenever the automation
            # VM is itself a nested VM (its own interface, e.g. "eth1", is
            # not the hypervisor's bridge name, e.g. "br0", even though
            # both sit on the same L2 segment). No safe universal default;
            # must be set explicitly to this automation VM's own real
            # interface (check with `ip -o link show` on it).
            "pxe_bridge": cluster_cfg.get("pxe_bridge") or die(
                "cluster config has no 'pxe_bridge' — set it to this automation VM's own "
                "network interface name (see `ip -o link show`), not the hypervisor's bridge name"),
            # "proxy" default, matching PXEService's own existing stance
            # (its module docstring already calls this "the recommended
            # default for 'don't take over DHCP'"): this project's lab
            # bridges are commonly bridged straight onto the real physical
            # LAN (confirmed live 2026-08-30 on nuc6 — br0 has the host's
            # own eth0 enslaved to it), so a "full" dnsmasq would broadcast
            # real IP leases to every device on that shared network, not
            # just the netbooting test VM. Proxy mode only answers PXE-
            # tagged requests and leaves real IP leasing to whatever real
            # DHCP server already serves that LAN. Override to "full" only
            # for a genuinely isolated bridge with no other DHCP server.
            "pxe_dhcp_mode": cluster_cfg.get("dhcp_mode", "proxy"),
            "pxe_dhcp_range_start": cluster_cfg.get("dhcp_range_start"),
            "pxe_dhcp_range_end": cluster_cfg.get("dhcp_range_end"),
            # required by "proxy" mode (the default) — see
            # PXEService's own _dnsmasq_conf() docstring for why this
            # must be a network address, not an interface name.
            "pxe_dhcp_proxy_subnet": cluster_cfg.get("dhcp_proxy_subnet"),
        },
    }

    log("Configuring PXE service (ipxe-uefi mode)")
    svc = services.get("pxe", lab_setup_path=lab_setup_path)
    svc.install()
    svc.configure(definition, config)
    svc.enable()

    if cluster_cfg.get("create_vms", True):
        log("Creating netboot VMs")
        for node in cluster_cfg["nodes"]:
            _create_netboot_vm(node, cluster_cfg, config)

    log("Waiting for the 'create' node's VIP ({}) to come up — this can take 15-30+ minutes "
        "(install + first-boot cluster bootstrap)".format(cluster_cfg["vip"]))
    check_ssh_conn(cluster_cfg["vip"], tcp_port=443, retry_interval=15, retry_limit=240)
    log("Harvester VIP is responding — check https://{}/ to confirm cluster health".format(cluster_cfg["vip"]))

    if cluster_cfg.get("post_install_settings"):
        create_node = next(n for n in cluster_cfg["nodes"] if n.get("role") == "create")
        log("Fetching kubeconfig from '{}' to apply post-install settings".format(create_node["name"]))
        kubeconfig_path = _fetch_harvester_kubeconfig(cluster_cfg, create_node)
        _apply_post_install_settings(cluster_cfg, kubeconfig_path)


if __name__ == "__main__":
    main()
