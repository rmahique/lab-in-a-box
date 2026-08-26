#!/usr/bin/env python3
# Part of lab-in-a-box, it will install Uyuni server on a dedicated host VM
# Author/s: Raul Mahiques
# License: GPLv3
#
# Uyuni is an open-source systems management solution (upstream of SUSE Manager).
# This script installs it on a dedicated host VM using mgradm (container-based install).
# The target VM must run openSUSE Leap 15.6 / SLE Micro with podman available.
#
# JSON section: "uyuni" — configurable keys:
#   uyuni_admin         : [OPTIONAL] admin username (default: admin)
#   uyuni_password      : [OPTIONAL] admin password (default: Uyuni12345)
#   uyuni_email         : [OPTIONAL] admin email (default: admin@lab.local)
#   uyuni_org           : [OPTIONAL] default organisation name (default: lab)
#   uyuni_ssl_password  : [OPTIONAL] SSL certificate password (default: same as uyuni_password)
#   uyuni_channels      : [OPTIONAL] space-separated list of channels to sync after install
#   uyuni_extra_dsk     : [OPTIONAL] extra disk to mount for storage (e.g. /dev/vdb,/srv/mirror)
#
# The target node must have "uyuni" in its addons[] list in the JSON definition:
#   "nodes": { "uyuni.lab": { "myip": "...", "addons": ["uyuni"] } }

__version__ = "__LABVERSION__"

import os
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run, reboot_vm, check_ssh_conn  # noqa: E402


def setup_uyuni(hostname, virt_srv, cfg):
    """Install Uyuni on a host VM via mgradm. Mirrors setup_uyuni (bash)."""
    print("- Installing mgradm tooling")
    ssh_run(hostname, "transactional-update --quiet pkg install -y mgradm mgradm-bash-completion "
                       "mgrctl mgrctl-bash-completion")
    reboot_vm(virt_srv, hostname)
    time.sleep(5)
    check_ssh_conn(hostname)

    extra_dsk = cfg.get("uyuni_extra_dsk") or ""
    if extra_dsk:
        for dsk in extra_dsk.split():
            print("- Mounting extra disk {}".format(dsk))
            device, mountpoint = dsk.split(",", 1)
            ssh_run(hostname, "echo '{} {} xfs defaults,nofail 1 2' >> /etc/fstab".format(device, mountpoint))
        reboot_vm(virt_srv, hostname)
        time.sleep(5)
        check_ssh_conn(hostname)

    print("- Installing Uyuni server")
    admin = cfg.get("uyuni_admin") or "admin"
    password = cfg.get("uyuni_password") or "Uyuni12345"
    ssh_run(hostname,
            "mgradm install podman "
            "--admin-login {} "
            "--admin-password {} "
            "--admin-email {} "
            "--ssl-password {} "
            "--organization {}".format(
                admin, password, cfg.get("uyuni_email") or "admin@lab.local",
                cfg.get("uyuni_ssl_password") or password, cfg.get("uyuni_org") or "lab"))

    time.sleep(60)
    ssh_run(hostname, "reboot", check=False)
    time.sleep(5)
    check_ssh_conn(hostname)

    print("Uyuni available at: https://{}  ({} / {})".format(hostname, admin, password))

    channels = cfg.get("uyuni_channels") or ""
    if channels:
        count = 0
        print("- Waiting for channel list to sync")
        while True:
            time.sleep(10)
            count += 1
            print("Retry {}".format(count), end="\r")
            out = ssh_run(hostname, "mgrctl exec -- mgr-sync list channels 2>/dev/null",
                          check=False, capture=True).stdout or ""
            if any("no channels found." not in line.lower() for line in out.splitlines()):
                break
        time.sleep(300)
        ssh_run(hostname, "mgrctl exec -- mgr-sync add channels {}".format(channels))


def main():
    # bash's --validate block here defines the usual helpers but never calls
    # any of them — always exits 0.
    ac.handle_common_args(__file__, __version__, validate_fn=None)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    config = primary.load_config()

    cfg = definition.get("uyuni", {}) or {}
    virt_srv = config.get("VIRT_SRV", "")

    # on_addon_nodes semantics: respect an inherited _vm_name, else scan for
    # nodes with "uyuni" in their addons[] list.
    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "uyuni", vm_name=env_vm_name):
        setup_uyuni(vm_name, virt_srv, cfg)


if __name__ == "__main__":
    main()
