#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install LongHorn
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "longhorn" — SUSE Longhorn distributed block storage
#
#   lh_shorthn   : [OPTIONAL] Short hostname for the UI ingress        (default: longhorn)
#   lh_rel       : [OPTIONAL] Helm repo alias                          (default: longhorn)
#   lh_repo_url  : [OPTIONAL] Helm repo URL                           (default: https://charts.longhorn.io)
#   lh_version   : [OPTIONAL] Helm chart version                       (empty = latest)

__version__ = "ca2d2d5"

PLUGIN = {
    "name": "longhorn",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402


def _validate(v):
    v.vns("longhorn")
    v.vver("longhorn")
    v.vurl("longhorn")


def setup_lh_repo(hostname, lh_rel=None, lh_repo_url=None):
    """Add the SUSE Longhorn Helm repo. Mirrors setup_lh_repo (bash)."""
    helm_repo_add(hostname, lh_rel or "longhorn", lh_repo_url or "https://charts.longhorn.io")


def setup_lh(hostname, clu_name, mydomain, lh_rel=None, lh_shorthn=None, lh_version=None):
    """
    Install SUSE Longhorn. Mirrors setup_lh (bash).

    NOTE: lh_version is accepted (and its format is checked by --validate) but,
    matching the current bash exactly, is never actually passed to helm as a
    --version flag — the field is documented but not wired up in bash either.
    Fixed the repo alias bug (bash used the literal "longhorn/longhorn" repo
    reference regardless of lh_rel, inconsistent with setup_lh_repo which
    always added the repo under lh_rel — would fail if lh_rel were ever
    customized) — uses lh_rel consistently here.
    """
    rel = lh_rel or "longhorn"
    fqdn = "{}.{}.{}".format(lh_shorthn or "longhorn", clu_name, mydomain)

    ssh_run(hostname, "zypper install -y open-iscsi cryptsetup; "
                       "systemctl enable --now iscsid.service ; modprobe iscsi_tcp")
    ssh_run(hostname, "kubectl create namespace longhorn-system")
    ssh_run(hostname,
            "helm upgrade -i longhorn {}/longhorn --namespace longhorn-system "
            "--set ingress.enabled=true --set ingress.host={} "
            "--set persistence.migratable=true --set longhornUI.replicas=1".format(rel, fqdn))
    print("Longhorn should be available in a few minutes in: {}".format(fqdn))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = definition.get("longhorn", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    # bash also does `load_rancher_vars` here — confirmed unused by setup_lh/
    # setup_lh_repo (neither references any rancher_* variable); not ported.
    setup_helm(vm_name, clu_name, online=online)
    setup_lh_repo(vm_name, lh_rel=cfg.get("lh_rel"), lh_repo_url=cfg.get("lh_repo_url"))
    setup_lh(vm_name, clu_name, clu_cfg.get("mydomain", ""), lh_rel=cfg.get("lh_rel"),
             lh_shorthn=cfg.get("lh_shorthn"), lh_version=cfg.get("lh_version"))

    time.sleep(60)


if __name__ == "__main__":
    main()
