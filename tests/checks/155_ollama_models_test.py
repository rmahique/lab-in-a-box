#!/usr/bin/env python3.11
# libs/ollama_common.py and the add-ons that serve a model through Ollama — see 155_ollama_models.sh.
import importlib.util
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

# ── the add-ons all go through it ────────────────────────────────────────────
for addon in ("ollama", "apertus", "codellama", "deepseek", "mistral", "qwen", "starcoder2"):
    text = (_REPO / "scripts" / ("install_" + addon)).read_text()
    check("{}: installs through ollama_common".format(addon),
          'ollama_common.install(primary.load_definition(sys.argv[1]), "{}")'.format(addon) in text
          and "helm upgrade" not in text)
    module = _load("install_" + addon)
    check("{}: still loads with its PLUGIN".format(addon), module.PLUGIN["name"] == addon)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all ollama model checks passed")
