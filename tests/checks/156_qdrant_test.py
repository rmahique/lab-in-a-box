#!/usr/bin/env python3.11
# install_qdrant — see 156_qdrant.sh.
import os
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402

qd = apps.load_addon_module("install_qdrant", str(_REPO / "scripts"))
failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def run_podman(cfg):
    calls, deploys = [], []
    with mock.patch.object(qd, "ssh_run", side_effect=lambda host, cmd, **kw: calls.append(cmd)), \
            mock.patch.object(qd.ps, "deploy", side_effect=lambda *a, **kw: deploys.append((a, kw))), \
            mock.patch.object(qd.ps, "wait_for_http", return_value=True), mock.patch("builtins.print"):
        url = qd.setup_qdrant_podman("ws1.mydemo.lab", cfg)
    return calls, deploys, url


calls, deploys, url = run_podman({})
args, kw = deploys[0]
check("podman: the upstream image, both APIs published, the collections in a host directory",
      args[1:3] == ("qdrant", "docker.io/qdrant/qdrant:latest") and kw["ports"] == ["6333:6333", "6334:6334"]
      and kw["volumes"] == ["/var/lib/qdrant:/qdrant/storage:Z"] and "mkdir -p /var/lib/qdrant" in calls)
check("podman: reached at the node's own name", url == "http://ws1.mydemo.lab:6333")
_c, deploys, url = run_podman({"qdrant_port": "7333", "qdrant_grpc_port": "7334", "qdrant_image_tag": "v1.15.0",
                               "qdrant_data_dir": "/srv/qdrant"})
check("podman: the ports, the tag and the directory the lab names are used",
      deploys[0][0][2] == "docker.io/qdrant/qdrant:v1.15.0" and deploys[0][1]["ports"] == ["7333:6333", "7334:6334"]
      and deploys[0][1]["volumes"] == ["/srv/qdrant:/qdrant/storage:Z"] and url.endswith(":7333"))


def run_main(definition, env):
    podman, helm = [], []
    with mock.patch.object(qd.primary, "load_definition", return_value=definition), \
            mock.patch.object(sys, "argv", ["install_qdrant", "lab.json"]), \
            mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(qd, "setup_qdrant_podman", side_effect=lambda h, c: podman.append((h, c))), \
            mock.patch.object(qd, "setup_qdrant", side_effect=lambda h, **kw: helm.append(h)), \
            mock.patch.object(qd, "setup_qdrant_repo"), mock.patch.object(qd, "setup_helm"), \
            mock.patch.object(qd.k8s, "first_server_node", return_value=("n1.mydemo.lab", "ssh")), \
            mock.patch.object(qd.k8s, "get_vm_kcluster", return_value="c1"), mock.patch.object(qd.k8s, "log"):
        qd.main()
    return podman, helm


on_node = {"nodes": {"ws1.mydemo.lab": {"addons": [{"qdrant": {"qdrant_port": "7000"}}]}}, "kclusters": {},
           "qdrant": {}}
podman, helm = run_main(on_node, {})
check("main: listed on a node, it runs there with that node's own override",
      podman == [("ws1.mydemo.lab", {"qdrant_port": "7000"})] and helm == [])
podman, helm = run_main({"nodes": {"n1.mydemo.lab": {"kcluster": "c1"}}, "kclusters": {"c1": {"addons": ["qdrant"]}}},
                        {"clu_name": "c1"})
check("main: listed on a kcluster, the chart is installed there", podman == [] and helm == ["n1.mydemo.lab"])


def errors(cfg):
    v = ac.Validator({"qdrant": cfg})
    qd._validate(v)
    return v.errors


check("validate: an empty section is valid", errors({}) == [])
check("validate: an unknown deployment and non-numeric ports are rejected",
      len(errors({"qdrant_deployment": "docker", "qdrant_port": "x", "qdrant_grpc_port": "y"})) == 3)
check("PLUGIN: offered on nodes as a podman container",
      {"vm", "baremetal"} <= set(qd.PLUGIN["targets"]) and "standalone-container" in qd.PLUGIN["layers"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all qdrant checks passed")
