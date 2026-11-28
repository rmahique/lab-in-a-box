#!/usr/bin/env python3.11
# libs/litellm_common.py and its add-ons — see 87_litellm.sh.
import os
import re
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402
import litellm_common as lc  # noqa: E402

failures = []


import apps  # noqa: E402


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def load(name):
    """An add-on's module. It has no .py extension, so importlib cannot infer a loader from the path."""
    return apps.load_addon_module(name, str(_REPO / "scripts"))


def run(fn, *args, **kwargs):
    cmds = []
    with mock.patch.object(lc, "ssh_run", side_effect=lambda host, cmd, **kw: cmds.append(cmd)), \
            mock.patch("builtins.print"):
        result = fn(*args, **kwargs)
    return cmds, result


cmds, _ = run(lc.setup_litellm_proxy, "node1", "gemini-proxy", "gemini", "gemini.c1.lab", "gemini",
              "gemini/gemini-2.5-flash", "AIzaSECRET")
helm = [c for c in cmds if c.startswith("helm ")]
check("one helm upgrade --install from the OCI chart, waiting",
      len(helm) == 1 and helm[0].startswith("helm upgrade --install gemini-proxy oci://ghcr.io/berriai/litellm-helm --namespace gemini")
      and "--wait" in helm[0])
check("the API key is on no command line except as an environment variable assignment",
      all("AIzaSECRET" not in c or c.startswith("PROVIDER_API_KEY=AIzaSECRET bash -c ") for c in cmds))
check("the provider secret is applied idempotently",
      any("create secret generic gemini-proxy-provider" in c and "kubectl apply -f -" in c for c in cmds))
master = [c for c in cmds if "gemini-proxy-master-key" in c and "create secret" in c]
check("the master key is created only when missing",
      len(master) == 1 and "kubectl get secret gemini-proxy-master-key" in master[0] and "||" in master[0])
check("the secrets exist before the chart is installed", cmds.index(master[0]) < cmds.index(helm[0]))
for flag in ("db.deployStandalone=false", "migrationJob.enabled=false", "masterkeySecretName=gemini-proxy-master-key",
             "environmentSecrets[0]=gemini-proxy-provider", "proxy_config.model_list[0].model_name=gemini",
             "proxy_config.model_list[0].litellm_params.model=gemini/gemini-2.5-flash",
             "proxy_config.model_list[0].litellm_params.api_key=os.environ/PROVIDER_API_KEY",
             "ingress.enabled=true", "ingress.hosts[0].host=gemini.c1.lab", "ingress.hosts[0].paths[0].pathType=Prefix"):
    check("helm sets " + flag, "--set " + flag in helm[0] or "--set '{}'".format(flag) in helm[0])
check("no api_base unless given", "api_base" not in helm[0])

cmds, _ = run(lc.setup_litellm_proxy, "node1", "kimi-proxy", "kimi", "kimi.c1.lab", "kimi", "moonshot/kimi-k3", "k",
              chart="oci://mirror/litellm-helm", version="1.105.0", api_base="https://api.moonshot.cn/v1")
helm = [c for c in cmds if c.startswith("helm ")][0]
check("chart, version and api_base are passed",
      "oci://mirror/litellm-helm" in helm and "--version 1.105.0" in helm
      and "proxy_config.model_list[0].litellm_params.api_base=https://api.moonshot.cn/v1" in helm)

for name, model_name, model in (("gemini", "gemini", "gemini/gemini-2.5-flash"), ("openai", "gpt", "openai/gpt-5"),
                                ("anthropic", "claude", "anthropic/claude-sonnet-5"), ("kimi", "kimi", "moonshot/kimi-k3")):
    addon = load(name)
    with mock.patch.object(addon.litellm_common, "setup_litellm_proxy") as proxy:
        fqdn = getattr(addon, "setup_" + name)("node1", "c1", "lab", {name + "_api_key": "k"})
    args, kwargs = proxy.call_args
    check("{}: release {}-proxy, default hostname and model".format(name, name),
          args[1:7] == (name + "-proxy", name, "{}.c1.lab".format(name), model_name, model, "k") and fqdn == "{}.c1.lab".format(name))
    if name == "kimi":
        check("kimi: Moonshot's global endpoint by default", kwargs.get("api_base") == "https://api.moonshot.ai/v1")

# ── podman mode ──────────────────────────────────────────────────────────────
import yaml  # noqa: E402

conf = yaml.safe_load(lc.config_yaml("claude", "anthropic/claude-sonnet-5", "https://api.example/v1"))
check("podman: config.yaml serves the model under its name, reading both keys from the environment",
      conf["model_list"][0]["model_name"] == "claude"
      and conf["model_list"][0]["litellm_params"] == {"model": "anthropic/claude-sonnet-5",
                                                      "api_key": "os.environ/PROVIDER_API_KEY",
                                                      "api_base": "https://api.example/v1"}
      and conf["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY")
check("podman: no api_base unless given",
      "api_base" not in yaml.safe_load(lc.config_yaml("gpt", "openai/gpt-5"))["model_list"][0]["litellm_params"])


def run_podman(cfg, existing_master="", section="anthropic"):
    cmds, stdins, deploys = [], [], []

    def fake_ssh(host, cmd, **kw):
        cmds.append(cmd)
        stdins.append(kw.get("input_text") or "")

    with mock.patch.object(lc, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(lc.ps, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(lc.ps, "ssh_output", return_value=existing_master), \
            mock.patch.object(lc.ps, "deploy", side_effect=lambda *a, **kw: deploys.append((a, kw))), \
            mock.patch.object(lc.ps, "wait_for_http", return_value=True), mock.patch("builtins.print"):
        url = lc.setup_litellm_podman("ws1.mydemo.lab", section, "claude", "anthropic/claude-sonnet-5", "sk-ant-SECRET",
                                      cfg)
    return cmds, stdins, deploys, url


cmds, stdins, deploys, url = run_podman({})
args, kw = deploys[0]
check("podman: one container per add-on, from the LiteLLM image, on the add-on's own default port",
      args[1:3] == ("anthropic-proxy", "ghcr.io/berriai/litellm:main-stable") and kw["ports"] == ["4000:4000"]
      and url == "http://ws1.mydemo.lab:4000/v1")
check("podman: the keys reach the container through a root-only env file podman reads itself",
      kw["env_file"] == "/etc/litellm/anthropic/env"
      and any(c.startswith("umask 077 && cat > /etc/litellm/anthropic/env") for c in cmds))
check("podman: the provider key is on no command line, only in that file's contents",
      all("sk-ant-SECRET" not in c for c in cmds) and "sk-ant-SECRET" not in repr(kw)
      and any("PROVIDER_API_KEY=sk-ant-SECRET\n" in i for i in stdins))
check("podman: the config is mounted read-only and named on the command line",
      kw["volumes"] == ["/etc/litellm/anthropic/config.yaml:/app/config.yaml:ro,Z"]
      and kw["args"] == ["--config", "/app/config.yaml", "--port", "4000"])
_c, stdins, _d, _u = run_podman({}, existing_master="sk-kept")
check("podman: a master key already on the node is kept", any("LITELLM_MASTER_KEY=sk-kept\n" in i for i in stdins))
_c, stdins, _d, _u = run_podman({})
check("podman: else one is generated", any(re.search(r"LITELLM_MASTER_KEY=sk-[0-9a-f]{32}\n", i) for i in stdins))
_c, _s, deploys, url = run_podman({"anthropic_port": "4400", "anthropic_image": "mirror/litellm",
                                   "anthropic_image_tag": "v1"})
check("podman: the port and the image the lab names are used",
      deploys[0][0][2] == "mirror/litellm:v1" and deploys[0][1]["ports"] == ["4400:4000"]
      and url.endswith(":4400/v1"))

check("podman: every proxy add-on has its own default port",
      len(set(lc.HOST_PORTS.values())) == len(lc.HOST_PORTS)
      and set(lc.HOST_PORTS) == set(lc.PROVIDERS) | {"anthropic", "gemini", "kimi", "openai"})

nodes_def = {"common": {}, "kclusters": {},
             "nodes": {"ws1.mydemo.lab": {"addons": [{"anthropic": {"anthropic_model": "anthropic/claude-opus-5"}}]},
                       "ws2.mydemo.lab": {"addons": ["anthropic"]}},
             "anthropic": {"anthropic_api_key": "k"}}
seen = []
with mock.patch.object(lc, "setup_litellm_podman",
                       side_effect=lambda h, s, n, m, k, c, api_base="": seen.append((h, m))), \
        mock.patch.dict(os.environ, {}, clear=True), mock.patch("k8s.log"):
    lc.install_podman_nodes(nodes_def, "anthropic", "claude", default_model="anthropic/claude-sonnet-5")
check("podman: every node listing it gets a proxy, with that node's own override",
      seen == [("ws1.mydemo.lab", "anthropic/claude-opus-5"), ("ws2.mydemo.lab", "anthropic/claude-sonnet-5")])
try:
    with mock.patch.dict(os.environ, {}, clear=True), mock.patch("k8s.log"):
        lc.install_podman_nodes(dict(nodes_def, anthropic={}), "anthropic", "claude", default_model="m")
    check("podman: a node without the provider key stops the install", False)
except SystemExit:
    pass

for name in ("anthropic", "gemini", "kimi", "openai"):
    mod = load("install_" + name)
    if "vm" not in mod.PLUGIN["targets"]:
        continue
    v = ac.Validator({name: {"{}_deployment".format(name): "docker", "{}_port".format(name): "x",
                             "{}_api_key".format(name): "k"}})
    mod._validate(v)
    check("{}: an unknown deployment and a non-numeric port are rejected".format(name), len(v.errors) == 2)
    calls = []
    with mock.patch.object(mod.litellm_common, "install_podman_nodes",
                           side_effect=lambda d, s, n, **kw: calls.append((s, n, kw))), \
            mock.patch.object(mod.primary, "load_definition", return_value={"nodes": {"ws1": {"addons": [name]}}}), \
            mock.patch.object(sys, "argv", ["install_" + name, "lab.json"]), \
            mock.patch.dict(os.environ, {}, clear=True):
        mod.main()
    check("{}: listed only on a node, it installs with podman".format(name), calls and calls[0][0] == name)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all litellm checks passed")
