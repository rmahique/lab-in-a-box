#!/usr/bin/env python3.11
# libs/ollama_common.py and the add-ons that serve a model through Ollama — see 155_ollama_models.sh.
import importlib.util
import re
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import ollama_common as oc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _load(name):
    loader = SourceFileLoader(name, str(_REPO / "scripts" / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def lab(addons, **sections):
    definition = {"common": {}, "nodes": {"n1.mydemo.lab": {"kcluster": "ai", "KUBERNETES_NODE_TYPE": "server"}},
                  "kclusters": {"ai": {"clu_type": "k3s", "mydomain": "mydemo.lab", "addons": addons}}}
    definition.update(sections)
    return definition


def run_release(definition, addon, helm_list=""):
    """install_release() with SSH faked; returns (commands, warnings, fqdn)."""
    calls, warnings = [], []
    with mock.patch.object(oc, "ssh_run", side_effect=lambda host, cmd, **kw: calls.append(cmd)), \
            mock.patch.object(oc, "helm_repo_add", side_effect=lambda h, n, u: calls.append("repo " + n + " " + u)), \
            mock.patch.object(oc, "ssh_output", return_value=helm_list), \
            mock.patch.object(oc, "warn", side_effect=warnings.append), \
            mock.patch.object(oc.appco, "prepare_appcollection_repo",
                              return_value=("oci://dp.apps.rancher.io/charts", " --set global.imagePullSecrets[0]=s")), \
            mock.patch.object(oc.appco, "credentials", return_value=("user", "token")), \
            mock.patch("builtins.print"):
        fqdn = oc.install_release("n1.mydemo.lab", definition, "ai", "mydemo.lab", addon)
    return calls, warnings, fqdn


def helm_of(calls):
    return next(c for c in calls if c.startswith("helm upgrade"))


# ── the models of a kcluster ─────────────────────────────────────────────────
upstream = {"ollama_source": "upstream"}
d = lab(["ollama", "codellama", "starcoder2"], ollama=upstream)
check("every model the kcluster's add-ons ask for, in their order",
      oc.kcluster_models(d, "ai") == ["llama3.2", "codellama", "starcoder2"])
check("each model add-on's own field names its model",
      oc.kcluster_models(lab(["qwen", "apertus"], qwen={"qwen_model": "qwen2.5:14b"},
                             apertus={"apertus_tag": "8b-instruct-2509-bf16"}), "ai")
      == ["qwen2.5:14b", "MichelRosselli/apertus:8b-instruct-2509-bf16"])
check("a model asked for twice is pulled once",
      oc.kcluster_models(lab(["ollama", "mistral"], ollama={"ollama_model": "mistral"}), "ai") == ["mistral"])
check("a manual run of a model add-on the kcluster does not list still pulls its own model",
      oc.kcluster_models(lab(["ollama"]), "ai", "deepseek") == ["llama3.2", "deepseek-r1:7b"])
check("add-ons that serve no model are not counted",
      oc.kcluster_models(lab(["open_webui", "codellama"]), "ai") == ["codellama"])
check("every model add-on is known, so its default model is the one its header documents",
      sorted(oc.MODEL_ADDONS) == ["apertus", "codellama", "deepseek", "mistral", "qwen", "starcoder2"])

# ── who sets the release's fields ────────────────────────────────────────────
check("the ollama add-on owns the release when the kcluster lists it",
      oc.release_owner(lab(["codellama", "ollama"]), "ai", "codellama") == "ollama")
check("else the first model add-on the kcluster lists",
      oc.release_owner(lab(["open_webui", "starcoder2", "codellama"]), "ai", "codellama") == "starcoder2")

# ── one release, every model, whichever add-on runs ──────────────────────────
for addon in ("ollama", "codellama", "starcoder2"):
    calls, _w, fqdn = run_release(d, addon)
    helm = helm_of(calls)
    check("{}: installs the release named ollama".format(addon), helm.startswith("helm upgrade --install ollama "))
    check("{}: hands the chart every model of the kcluster".format(addon),
          "'ollama.models.pull={llama3.2,codellama,starcoder2}'" in helm)
    check("{}: the ollama add-on's chart and ingress are kept".format(addon),
          "ollama-helm/ollama" in helm and fqdn == "ollama.ai.mydemo.lab")

d2 = lab(["ollama", "codellama"], ollama={"ollama_source": "upstream", "ollama_ns": "llm", "ollama_shorthn": "llm",
                                          "ollama_version": "1.2.3"},
         codellama={"codellama_ns": "elsewhere", "codellama_shorthn": "code"})
calls, _w, fqdn = run_release(d2, "codellama")
helm = helm_of(calls)
check("a model add-on keeps to the ollama add-on's namespace, version and hostname",
      "--namespace llm" in helm and "--version 1.2.3" in helm and fqdn == "llm.ai.mydemo.lab"
      and "elsewhere" not in helm)

d3 = lab(["codellama"], codellama={"codellama_ns": "code", "codellama_shorthn": "code"})
calls, _w, fqdn = run_release(d3, "codellama")
check("without the ollama add-on, the model add-on's own fields set the release",
      "--namespace code" in helm_of(calls) and fqdn == "code.ai.mydemo.lab")

calls, _w, _f = run_release(lab(["ollama", "codellama"]), "codellama")
check("the ollama add-on's Application Collection source is kept by a model add-on",
      "oci://dp.apps.rancher.io/charts/ollama" in helm_of(calls)
      and "global.imagePullSecrets[0]" in helm_of(calls))

calls, warnings, _f = run_release(lab(["deepseek"]), "deepseek", helm_list="ollama\nollama-deepseek\n")
check("deepseek no longer installs an Ollama of its own", helm_of(calls).startswith("helm upgrade --install ollama "))
check("a separate release an earlier run left is named, with how to remove it",
      any("ollama-deepseek" in w and "helm uninstall" in w for w in warnings))

# ── where other add-ons reach it ─────────────────────────────────────────────
check("the in-cluster URL is the release's Service, in the release's namespace",
      oc.service_url(lab(["ollama"], ollama={"ollama_ns": "llm"}), "ai") == "http://ollama.llm.svc.cluster.local:11434")
check("a kcluster with only a model add-on has that Ollama too",
      oc.service_url(lab(["starcoder2"]), "ai") == "http://ollama.ollama.svc.cluster.local:11434")
check("a kcluster with none of them has no Ollama to reach", oc.service_url(lab(["open_webui"]), "ai") == "")

# ── podman mode ──────────────────────────────────────────────────────────────


def on_node(addons, **sections):
    definition = {"common": {}, "kclusters": {},
                  "nodes": {"ws1.mydemo.lab": {"addons": addons}, "ws2.mydemo.lab": {"addons": ["xfce"]}}}
    definition.update(sections)
    return definition


def run_podman(definition, addon, running=False):
    """install_podman() on ws1 with SSH and podman faked; returns (ssh commands, deploy calls)."""
    calls, deploys = [], []
    with mock.patch.object(oc, "ssh_run", side_effect=lambda host, cmd, **kw: calls.append(cmd)), \
            mock.patch.object(oc.ps, "deploy", side_effect=lambda *a, **kw: deploys.append((a, kw))), \
            mock.patch.object(oc.ps, "wait_for_http", return_value=True), \
            mock.patch.object(oc.ps, "is_running", return_value=running), \
            mock.patch.object(oc, "log"), mock.patch("builtins.print"):
        url = oc.install_podman(definition, addon, "ws1.mydemo.lab")
    return calls, deploys, url


calls, deploys, url = run_podman(on_node(["ollama"], ollama={"ollama_port": "11500",
                                                             "ollama_image_tag": "0.12.6"}), "ollama")
args, kw = deploys[0]
check("podman: the ollama add-on runs the container from its own section",
      args[1:3] == ("ollama", "docker.io/ollama/ollama:0.12.6") and kw["ports"] == ["11500:11434"]
      and kw["volumes"] == ["/var/lib/ollama:/root/.ollama:Z"])
check("podman: and pulls its own model into it", "podman exec ollama ollama pull llama3.2" in calls)
check("podman: other add-ons reach it at the node's own name and port", url == "http://ws1.mydemo.lab:11500")

calls, deploys, url = run_podman(on_node(["ollama", "codellama"], codellama={"codellama_model": "codellama:13b"}),
                                 "codellama", running=True)
check("podman: a model add-on leaves a running Ollama alone and pulls its model into it",
      deploys == [] and calls == ["podman exec ollama ollama pull codellama:13b"])
calls, deploys, url = run_podman(on_node(["starcoder2"], ollama={"ollama_port": "11600"}), "starcoder2")
check("podman: a model add-on starts Ollama, from the ollama section, when none runs yet",
      len(deploys) == 1 and deploys[0][1]["ports"] == ["11600:11434"]
      and "podman exec ollama ollama pull starcoder2" in calls)
calls, _d, _u = run_podman(on_node([{"ollama": {"ollama_model": "phi3"}}]), "ollama")
check("podman: a node's own override of the section is used", "podman exec ollama ollama pull phi3" in calls)

seen = []
with mock.patch.object(oc, "install", side_effect=lambda d, a: seen.append(("kubernetes", a))), \
        mock.patch.object(oc, "install_podman", side_effect=lambda d, a, h: seen.append(("podman", h))), \
        mock.patch.dict(oc.os.environ, {}, clear=True):
    oc.run(on_node(["ollama"]), "ollama")
    oc.run(lab(["ollama"]), "ollama")
check("podman: run() installs on every node that lists it, and on the kcluster when a kcluster does",
      seen == [("podman", "ws1.mydemo.lab"), ("kubernetes", "ollama")])

ol = _load("install_ollama")
import addon_common as ac  # noqa: E402


def ollama_errors(definition):
    v = ac.Validator(definition)
    ol._validate(v)
    return v.errors


with mock.patch.object(ac.Validator, "vreq_or_credential", autospec=True,
                       side_effect=lambda self, section, field, *a, **kw: self.errors.append(field)):
    check("validate: the Application Collection account is only asked for when a kcluster installs it",
          ollama_errors(on_node(["ollama"], ollama={})) == []
          and "ollama_appcollection_user" in ollama_errors(lab(["ollama"], ollama={})))
check("validate: an unknown deployment and a non-numeric port are rejected",
      len(ollama_errors(on_node(["ollama"], ollama={"ollama_deployment": "docker", "ollama_port": "x"}))) == 2)
check("ollama: offered on nodes too", {"vm", "baremetal"} <= set(ol.PLUGIN["targets"])
      and "standalone-container" in ol.PLUGIN["layers"])

for addon in sorted(oc.MODEL_ADDONS):
    module = _load("install_" + addon)
    if "vm" not in module.PLUGIN["targets"]:
        continue
    v = ac.Validator(on_node([addon], **{addon: {"{}_deployment".format(addon): "docker"}}))
    module._validate(v)
    check("{}: an unknown deployment is rejected".format(addon), len(v.errors) == 1)
    check("{}: offered as a podman container too".format(addon),
          "standalone-container" in module.PLUGIN["layers"] and "baremetal" in module.PLUGIN["targets"])
    calls, deploys, _u = run_podman(on_node(["ollama", addon]), addon, running=True)
    check("{}: on a node, pulls its default model into that node's Ollama".format(addon),
          calls == ["podman exec ollama ollama pull {}".format(oc.model_of(addon, {}))] and deploys == [])

# ── the add-ons all go through it ────────────────────────────────────────────
for addon in ("ollama", "apertus", "codellama", "deepseek", "mistral", "qwen", "starcoder2"):
    text = (_REPO / "scripts" / ("install_" + addon)).read_text()
    check("{}: installs through ollama_common".format(addon),
          re.search(r'ollama_common\.(install|run)\(primary\.load_definition\(sys\.argv\[1\]\), "{}"\)'.format(addon),
                    text) is not None and "helm upgrade" not in text)
    module = _load("install_" + addon)
    check("{}: still loads with its PLUGIN".format(addon), module.PLUGIN["name"] == addon)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all ollama model checks passed")
