#!/usr/bin/env python3.11
# libs/static_webgame_common.py and install_skynet_simulator — see 157_static_webgame.sh.
import os
import shlex
import sys
from pathlib import Path
from unittest import mock

import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402
import static_webgame_common as sw  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


docs = [d for d in yaml.safe_load_all(sw.render_static_webgame("g", "ns1", "g.c1.lab", "echo hi", args=["u"])) if d]
deployment = next(d for d in docs if d["kind"] == "Deployment")
check("kubernetes: the fetch script runs in the initContainer with its arguments",
      deployment["spec"]["template"]["spec"]["initContainers"][0]["command"] == ["sh", "-c", "echo hi", "_", "u"])
check("kubernetes: the ingress answers for the host", next(d for d in docs if d["kind"] == "Ingress")
      ["spec"]["rules"][0]["host"] == "g.c1.lab")

check("an image naming no registry gets Docker Hub's, spelled out",
      sw.qualified("alpine:3") == "docker.io/library/alpine:3" and sw.qualified("me/img") == "docker.io/me/img")
check("an image naming its registry is left alone",
      sw.qualified("ghcr.io/x/y:1") == "ghcr.io/x/y:1" and sw.qualified("localhost:5000/x") == "localhost:5000/x")

cmd = sw.fetch_command("/var/lib/g/html", "set -e\ncurl \"$1\" | tar -x -C /shared", args=["https://e/x.tgz"])
argv = shlex.split(cmd)
check("podman: the fetch script runs once, in a throwaway container, with the web directory at /shared",
      argv[:6] == ["podman", "run", "--rm", "-v", "/var/lib/g/html:/shared:Z", "docker.io/library/alpine:3"]
      and argv[6:] == ["sh", "-c", "set -e\ncurl \"$1\" | tar -x -C /shared", "_", "https://e/x.tgz"])

calls, deploys = [], []
with mock.patch.object(sw, "ssh_run", side_effect=lambda host, c, **kw: calls.append(c)), \
        mock.patch.object(sw.ps, "deploy", side_effect=lambda *a, **kw: deploys.append((a, kw))), \
        mock.patch.object(sw.ps, "wait_for_http", return_value=True):
    url = sw.deploy_podman("ws1.mydemo.lab", "skynet-simulator", "echo hi", 8090)
check("podman: the files are fetched before nginx serves them, read-only, on the port asked for",
      calls[0] == "mkdir -p /var/lib/skynet-simulator/html" and calls[1].startswith("podman run --rm ")
      and deploys[0][0][1:3] == ("skynet-simulator", "docker.io/library/nginx:stable")
      and deploys[0][1]["ports"] == ["8090:80"]
      and deploys[0][1]["volumes"] == ["/var/lib/skynet-simulator/html:/usr/share/nginx/html:ro,Z"]
      and url == "http://ws1.mydemo.lab:8090/")

sk = apps.load_addon_module("install_skynet_simulator", str(_REPO / "scripts"))
seen = []
with mock.patch.object(sk.primary, "load_definition", return_value={
            "kclusters": {}, "nodes": {"ws1.mydemo.lab": {"addons": [{"skynet_simulator": {
                "skynet_simulator_port": "9000"}}]}}}), \
        mock.patch.object(sys, "argv", ["install_skynet_simulator", "lab.json"]), \
        mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sk.k8s, "log"), \
        mock.patch.object(sk.static_webgame_common, "deploy_podman",
                          side_effect=lambda h, n, s, p, **kw: seen.append((h, n, p)) or "u"), \
        mock.patch("builtins.print"):
    sk.main()
check("skynet_simulator: listed on a node, it is fetched and served there, on that node's own port",
      seen == [("ws1.mydemo.lab", "skynet-simulator", 9000)])
v = ac.Validator({"skynet_simulator": {"skynet_simulator_deployment": "docker", "skynet_simulator_port": "x"}})
sk._validate(v)
check("skynet_simulator: an unknown deployment and a non-numeric port are rejected", len(v.errors) == 2)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all static web game checks passed")
