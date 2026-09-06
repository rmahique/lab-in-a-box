#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Home Assistant (open-source home automation platform)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "home_assistant" — configurable keys:
#   home_assistant_version : [OPTIONAL] Helm chart version (empty = latest, tracks Home Assistant's
#                            own release automatically per this chart's own CI)
#   home_assistant_ns      : [OPTIONAL] namespace (default: home-assistant)
#   home_assistant_shorthn : [OPTIONAL] hostname prefix (default: home-assistant)
#   home_assistant_rel     : [OPTIONAL] Helm repo alias (default: pajikos)
#   home_assistant_repo_url: [OPTIONAL] Helm repo URL
#                            (default: http://pajikos.github.io/home-assistant-helm-chart/)
#   home_assistant_storage_size  : [OPTIONAL] PersistentVolumeClaim size (default: 5Gi)
#   home_assistant_storage_class : [OPTIONAL] StorageClass name (default: cluster default)
#
# Home Assistant itself has no official Helm chart (confirmed live, 2026-09-06) — this uses
# pajikos/home-assistant-helm-chart, a real, actively-maintained community chart (confirmed: its
# own CI auto-updates the chart on every new Home Assistant release) wrapping the project's own
# official image (ghcr.io/home-assistant/home-assistant, confirmed at home-assistant/docker).
#
# PREREQUISITE this addon does NOT provide itself, confirmed elsewhere in this project: a
# StorageClass — a bare RKE2 cluster has none by default (see install_open_webui.py's own header
# for the live-confirmed finding). Home Assistant needs its PVC to actually bind, or the pod sits
# Pending indefinitely.
#
# NOT live-tested (no cluster available in this session) — chart repo/values keys
# (image.repository, ingress.hosts[].host, persistence.size/storageClass) verified against the
# chart's own values.yaml, 2026-09-06, not guessed.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "home_assistant",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run  # noqa: E402


def _validate(v):
    v.vns("home_assistant")
    v.vver("home_assistant")


def setup_home_assistant_repo(hostname, home_assistant_rel=None, home_assistant_repo_url=None):
    """Add the Home Assistant Helm repo."""
    helm_repo_add(hostname, home_assistant_rel or "pajikos",
                  home_assistant_repo_url or "http://pajikos.github.io/home-assistant-helm-chart/")


def setup_home_assistant(hostname, clu_name, mydomain, home_assistant_rel=None, home_assistant_ns=None,
                          home_assistant_version=None, home_assistant_shorthn=None,
                          home_assistant_storage_size=None, home_assistant_storage_class=None):
    """Install Home Assistant."""
    rel = home_assistant_rel or "pajikos"
    ns = home_assistant_ns or "home-assistant"
    ver_arg = "--version {}".format(shlex.quote(home_assistant_version)) if home_assistant_version else ""
    fqdn = "{}.{}.{}".format(home_assistant_shorthn or "home-assistant", clu_name, mydomain)
    size = home_assistant_storage_size or "5Gi"

    set_args = [
        "--set ingress.enabled=true",
        "--set ingress.hosts[0].host={}".format(shlex.quote(fqdn)),
        "--set ingress.hosts[0].paths[0].path=/",
        "--set ingress.hosts[0].paths[0].pathType=Prefix",
        "--set persistence.enabled=true",
        "--set persistence.size={}".format(shlex.quote(size)),
    ]
    if home_assistant_storage_class:
        set_args.append("--set persistence.storageClass={}".format(shlex.quote(home_assistant_storage_class)))

    ssh_run(hostname,
            "helm upgrade -i home-assistant {}/home-assistant --namespace {} --create-namespace "
            "{} {}".format(rel, ns, " ".join(set_args), ver_arg))

    print("Home Assistant installed. Namespace: {}".format(ns))
    print("Available at: http://{}".format(fqdn))


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
    cfg = definition.get("home_assistant", {}) or {}
    online = definition.get("common", {}).get("online") == "1"

    setup_helm(vm_name, clu_name, online=online)
    setup_home_assistant_repo(vm_name, home_assistant_rel=cfg.get("home_assistant_rel"),
                               home_assistant_repo_url=cfg.get("home_assistant_repo_url"))
    setup_home_assistant(vm_name, clu_name, clu_cfg.get("mydomain"),
                          home_assistant_rel=cfg.get("home_assistant_rel"),
                          home_assistant_ns=cfg.get("home_assistant_ns"),
                          home_assistant_version=cfg.get("home_assistant_version"),
                          home_assistant_shorthn=cfg.get("home_assistant_shorthn"),
                          home_assistant_storage_size=cfg.get("home_assistant_storage_size"),
                          home_assistant_storage_class=cfg.get("home_assistant_storage_class"))


if __name__ == "__main__":
    main()
