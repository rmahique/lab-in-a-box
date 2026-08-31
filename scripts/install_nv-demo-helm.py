#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install nv-demo-helm
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "nv-demo-helm" — NeuVector Helm-based demo workloads
#
#   nv_demo_helm_ns   : [OPTIONAL] Kubernetes namespace                (default: demo)
#   nv_demo_helm_name : [OPTIONAL] Helm release name                  (default: nvdemohelm)
#   nv_demo_helm_tag  : [OPTIONAL] Demo container image tag           (default: 0.4)

__version__ = "ca2d2d5"

PLUGIN = {
    "name": "nv-demo-helm",
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
from lab_creation import ssh_run  # noqa: E402


def setup_nv_demo_helm(hostname, clu_name, mydomain, cfg):
    """
    Clone and helm-install the nv-demo-helm demo workloads. Mirrors
    setup_nv-demo-helm (bash).

    NOTE: bash uses plain `helm install` (not `helm upgrade --install`, unlike
    almost every other addon in this repo) — non-idempotent, will fail if the
    release already exists. Preserved exactly; there is no lab-in-a-box rule
    against `helm install` (that convention belongs to a different project).
    """
    ns = cfg.get("nv_demo_helm_ns") or "demo"
    name = cfg.get("nv_demo_helm_name") or "nvdemohelm"
    tag = cfg.get("nv_demo_helm_tag") or "0.4"

    ssh_run(hostname, "git clone https://github.com/horantj/nv-demo-helm.git /var/tmp/nv-demo-helm")
    ssh_run(hostname,
            "helm install -n {} --create-namespace "
            "--set exploit.image_tag={} "
            "--set struts.ingress.enabled=true "
            "--set struts.ingress.host=struts-{}.{}.{} "
            "demo-release /var/tmp/nv-demo-helm/nv-demo".format(ns, tag, name, clu_name, mydomain))
    print("NV demo helm should be available in a few minutes, for instructions please visit: "
          "https://github.com/horantj/nv-demo-helm/tree/main")


def main():
    # bash's --validate block here defines the usual helpers but never calls
    # any of them — always exits 0.
    ac.handle_common_args(__file__, __version__, validate_fn=None, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    cfg = definition.get("nv-demo-helm", {}) or {}

    # Same pattern as install_insecure_app/install_struts_demo: bash's loop
    # has an `exit 1` inside it, so only the first node is ever processed.
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        clu_name = node_cfg.get("kcluster", "")
        clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}

        print("# Using node: {}".format(vm_name))
        setup_nv_demo_helm(vm_name, clu_name, clu_cfg.get("mydomain", ""), cfg)
        time.sleep(60)
        break


if __name__ == "__main__":
    main()
