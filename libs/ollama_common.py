#!/usr/bin/env python3
"""
libs/ollama_common.py: the one Ollama a kcluster runs, shared by the ollama add-on and by every add-on that serves a
model through it (MODEL_ADDONS).

All of them install or upgrade the same Helm release, "ollama", and each hands the chart the whole list of models
the kcluster's add-ons ask for, read from the lab definition rather than from what ran before. Whichever of them
runs last, the release pulls every model of the kcluster and none replaces another's.

The release's own settings (namespace, chart source, chart version, ingress hostname) are the ollama add-on's when
the kcluster lists it, else those of the first model add-on the kcluster lists.

Listed in a node's own addons[] instead, the same add-ons share one podman container on that node, "ollama", which
the ollama add-on's section configures (image, port, model directory). Each add-on starts it when it is not running
yet and pulls its own model into it; the models live in a host directory, so a new container keeps them.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import shlex

import apps
import appco_common as appco
import placement
import podman_service as ps
from lab_creation import die, helm_repo_add, log, ssh_output, ssh_run, warn

RELEASE = "ollama"
OLLAMA = "ollama"
DEFAULT_NS = "ollama"
DEFAULT_REL = "ollama-helm"
DEFAULT_REPO_URL = "https://otwld.github.io/ollama-helm/"
DEFAULT_MODEL = "llama3.2"
PORT = 11434
DEFAULT_IMAGE = "docker.io/ollama/ollama"
DEFAULT_IMAGE_TAG = "latest"
DEFAULT_DATA_DIR = "/var/lib/ollama"
CONTAINER = "ollama"
# Where the image keeps its models; fixed by the image.
CONTAINER_DATA = "/root/.ollama"
# How long a fresh container gets to answer before a pull is attempted anyway.
START_WAIT = 300

# Every add-on that serves a model through Ollama: (its model field, that field's default, the Ollama model name
# the field's value becomes).
MODEL_ADDONS = {
    "apertus": ("apertus_tag", "8b-instruct-2509-q4_k_m", "MichelRosselli/apertus:{}"),
    "codellama": ("codellama_model", "codellama", "{}"),
    "deepseek": ("deepseek_model", "deepseek-r1:7b", "{}"),
    "mistral": ("mistral_model", "mistral", "{}"),
    "qwen": ("qwen_model", "qwen2.5:7b", "{}"),
    "starcoder2": ("starcoder2_model", "starcoder2", "{}"),
}

# Releases that earlier versions of a model add-on installed beside "ollama", one Ollama of their own each.
SEPARATE_RELEASES = ("ollama-deepseek",)


def model_of(addon: str, cfg: dict) -> str:
    """The Ollama model name the add-on `addon` asks for with its own section `cfg`."""
    if addon == OLLAMA:
        return str((cfg or {}).get("ollama_model") or DEFAULT_MODEL)
    field, default, name = MODEL_ADDONS[addon]
    return name.format((cfg or {}).get(field) or default)


def kcluster_addons(definition: dict, clu_name: str) -> list:
    """The add-on names the kcluster `clu_name` lists in its own addons[], in that order."""
    clu = (definition.get("kclusters") or {}).get(clu_name) or {}
    return [apps.addon_entry_name(entry) for entry in clu.get("addons") or []]


def kcluster_models(definition: dict, clu_name: str, addon: str = "") -> list:
    """
    Every model the add-ons of the kcluster `clu_name` ask Ollama for, each once, in the order the kcluster lists
    them. `addon`, the one running now, is counted even when the kcluster does not list it, so a manual run of a
    model add-on still pulls its own model.
    """
    names = kcluster_addons(definition, clu_name)
    if addon and addon not in names:
        names.append(addon)
    models = []
    for name in names:
        if name == OLLAMA or name in MODEL_ADDONS:
            model = model_of(name, definition.get(name) or {})
            if model not in models:
                models.append(model)
    return models


def release_owner(definition: dict, clu_name: str, addon: str) -> str:
    """The add-on whose section sets the release's own fields: ollama when listed, else the first model add-on."""
    names = kcluster_addons(definition, clu_name)
    if OLLAMA in names:
        return OLLAMA
    for name in names:
        if name in MODEL_ADDONS:
            return name
    return addon


def release_settings(definition: dict, owner: str) -> dict:
    """The release's namespace, Helm repo alias and URL, chart version, hostname prefix and chart source."""
    cfg = definition.get(owner) or {}

    def field(name: str, default: str = "") -> str:
        return str(cfg.get("{}_{}".format(owner, name)) or default)

    source = appco.SOURCE_UPSTREAM
    if owner == OLLAMA:
        source = appco.chart_source(cfg, "ollama", appco.SOURCE_APPCOLLECTION)
    return {
        "ns": field("ns", DEFAULT_NS),
        "rel": field("rel", DEFAULT_REL),
        "repo_url": field("repo_url", DEFAULT_REPO_URL),
        "version": field("version"),
        "shorthn": field("shorthn", OLLAMA),
        "source": source,
    }


def service_url(definition: dict, clu_name: str) -> str:
    """
    The in-cluster URL of the kcluster's Ollama, "" when none of the kcluster's add-ons installs one: the release's
    Service, named after it, in the release's namespace.
    """
    names = kcluster_addons(definition, clu_name)
    if not any(name == OLLAMA or name in MODEL_ADDONS for name in names):
        return ""
    settings = release_settings(definition, release_owner(definition, clu_name, OLLAMA))
    return "http://{}.{}.svc.cluster.local:{}".format(RELEASE, settings["ns"], PORT)


def ingress_url(definition: dict) -> str:
    """
    The URL of a kcluster's Ollama from outside that kcluster, its ingress hostname: the first kcluster one of
    whose add-ons installs it. "" when no kcluster does.
    """
    for clu_name, clu in (definition.get("kclusters") or {}).items():
        if any(name == OLLAMA or name in MODEL_ADDONS for name in kcluster_addons(definition, clu_name)):
            settings = release_settings(definition, release_owner(definition, clu_name, OLLAMA))
            return "http://" + ".".join(p for p in (settings["shorthn"], clu_name, (clu or {}).get("mydomain")) if p)
    return ""


def _chart(hostname: str, definition: dict, settings: dict) -> tuple:
    """(chart reference, extra --set flags) for the release, with its repository or registry made ready first."""
    if settings["source"] != appco.SOURCE_APPCOLLECTION:
        helm_repo_add(hostname, settings["rel"], settings["repo_url"])
        return "{}/ollama".format(settings["rel"]), ""
    user, password = appco.credentials(definition.get(OLLAMA) or {}, "ollama")
    if not user or not password:
        die("ollama: ollama_source is \"appcollection\", so ollama_appcollection_user and "
            "ollama_appcollection_password are needed: set them, set ollama_appcollection_account "
            "to an \"appcollection\" credential file, or leave all three unset with exactly "
            "one such file in the store (https://apps.rancher.io)")
    repo, extra_set = appco.prepare_appcollection_repo(hostname, settings["ns"], user, password)
    return "{}/ollama".format(repo), extra_set


def helm_command(chart: str, ns: str, models: list, fqdn: str, version: str = "", extra_set: str = "") -> str:
    """The helm upgrade --install of the release: every model to pull, and an ingress at `fqdn`."""
    pairs = [
        ("ollama.models.pull", "{{{}}}".format(",".join(models))),
        ("ingress.enabled", "true"),
        ("ingress.hosts[0].host", fqdn),
        ("ingress.hosts[0].paths[0].path", "/"),
        ("ingress.hosts[0].paths[0].pathType", "Prefix"),
    ]
    sets = " ".join("--set {}".format(shlex.quote("{}={}".format(k, v))) for k, v in pairs)
    ver_arg = " --version {}".format(shlex.quote(version)) if version else ""
    return "helm upgrade --install {} {} --namespace {} --create-namespace {}{}{}".format(
        RELEASE, shlex.quote(chart), shlex.quote(ns), sets, ver_arg, extra_set)


def _warn_separate_releases(hostname: str, ns: str) -> None:
    """Name any release from SEPARATE_RELEASES still in `ns`: its models are in "ollama" now, so it only costs."""
    listed = ssh_output(hostname, "helm list -n {} -q 2>/dev/null".format(shlex.quote(ns))).split()
    for name in SEPARATE_RELEASES:
        if name in listed:
            warn("ollama: release {} from an earlier run is still in namespace {}, and its models are now in "
                 "release {}. Remove it with: helm uninstall {} -n {}".format(name, ns, RELEASE, name, ns))


def install_release(hostname: str, definition: dict, clu_name: str, mydomain: str, addon: str) -> str:
    """
    Install or upgrade the kcluster's Ollama release from `hostname`, on behalf of the add-on `addon`, with every
    model of the kcluster. Returns the hostname its ingress answers on.
    """
    settings = release_settings(definition, release_owner(definition, clu_name, addon))
    models = kcluster_models(definition, clu_name, addon)
    fqdn = ".".join(p for p in (settings["shorthn"], clu_name, mydomain) if p)
    chart, extra_set = _chart(hostname, definition, settings)
    ssh_run(hostname, helm_command(chart, settings["ns"], models, fqdn, settings["version"], extra_set))
    _warn_separate_releases(hostname, settings["ns"])
    print("Ollama release {} in namespace {} pulls: {}".format(RELEASE, settings["ns"], ", ".join(models)))
    print("API available at: http://{}".format(fqdn))
    return fqdn


def install(definition: dict, addon: str) -> str:
    """
    The whole install of `addon` on the lab's kcluster: Helm on its first server node, the shared release, and
    the release's hostname in DNS. Returns that hostname.
    """
    import addon_common as ac
    import k8s
    from lab_creation import setup_helm

    target = k8s.first_server_node(definition)
    if not target:
        die("{}: this lab has no kcluster server node to install Ollama on".format(addon))
    vm_name, _ssh_cmd = target
    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    setup_helm(vm_name, clu_name, online=(definition.get("common") or {}).get("online") == "1")
    fqdn = install_release(vm_name, definition, clu_name, clu_cfg.get("mydomain", ""), addon)
    ac.register_service_dns(definition, clu_name, fqdn)
    return fqdn


# ── podman mode ──────────────────────────────────────────────────────────────

def podman_port(cfg: dict) -> int:
    """The host port the podman-mode Ollama answers on, from the ollama add-on's section `cfg`."""
    return int((cfg or {}).get("ollama_port") or PORT)


def podman_url(node: str, cfg: dict) -> str:
    """Where other add-ons reach the podman-mode Ollama of `node`."""
    return placement.node_url(node, podman_port(cfg))


def deploy_podman(hostname: str, cfg: dict) -> str:
    """Run the Ollama container on `hostname` from the ollama add-on's section `cfg`, wait for it, return its URL."""
    port = podman_port(cfg)
    data_dir = str(cfg.get("ollama_data_dir") or DEFAULT_DATA_DIR)
    image = "{}:{}".format(cfg.get("ollama_image") or DEFAULT_IMAGE, cfg.get("ollama_image_tag") or DEFAULT_IMAGE_TAG)
    ssh_run(hostname, "mkdir -p {}".format(shlex.quote(data_dir)))
    ps.deploy(hostname, CONTAINER, image, description="Ollama (lab-in-a-box)",
              ports=["{}:{}".format(port, PORT)], volumes=["{}:{}:Z".format(data_dir, CONTAINER_DATA)])
    ps.wait_for_http(hostname, "http://127.0.0.1:{}/api/version".format(port), timeout=START_WAIT, what="Ollama")
    return podman_url(hostname, cfg)


def pull_podman(hostname: str, model: str) -> None:
    """Pull `model` into the Ollama container on `hostname`."""
    log("- Pulling {} into Ollama on {}".format(model, hostname))
    ssh_run(hostname, "podman exec {} ollama pull {}".format(CONTAINER, shlex.quote(model)))


def install_podman(definition: dict, addon: str, hostname: str) -> str:
    """
    Install `addon` with podman on `hostname`: the ollama add-on (re)creates the container, a model add-on starts it
    only when it is not running yet. Either then pulls its own model. Returns the URL Ollama answers on.
    """
    import k8s

    ollama_cfg = k8s.addon_node_config(definition, OLLAMA, hostname)
    if addon == OLLAMA or not ps.is_running(hostname, CONTAINER):
        url = deploy_podman(hostname, ollama_cfg)
    else:
        url = podman_url(hostname, ollama_cfg)
    pull_podman(hostname, model_of(addon, k8s.addon_node_config(definition, addon, hostname)))
    print("Ollama API available at: {}".format(url))
    print("The node itself must allow traffic on port {}: this add-on manages no firewall or security group".format(
        podman_port(ollama_cfg)))
    return url


def run(definition: dict, addon: str) -> None:
    """
    The whole of `addon`'s main(): on the kcluster's shared release (install()), or with podman on every node
    that lists it, as libs/placement.py decides.
    """
    cfg = definition.get(addon) or {}
    if placement.deployment(definition, addon, cfg) == placement.KUBERNETES:
        install(definition, addon)
        return
    for vm_name in placement.podman_nodes(definition, addon):
        install_podman(definition, addon, vm_name)


def podman_nodes_running(definition: dict) -> list:
    """The nodes that run a podman Ollama: those listing ollama or a model add-on that is not forced onto Kubernetes."""
    nodes = []
    for addon in (OLLAMA,) + tuple(sorted(MODEL_ADDONS)):
        if placement.asked(addon, definition.get(addon) or {}) == placement.KUBERNETES:
            continue
        nodes += [n for n in placement.nodes_listing(definition, addon) if n not in nodes]
    return nodes


def podman_url_for(definition: dict, prefer: str = "") -> str:
    """
    The URL of a podman Ollama another add-on reaches: the one on `prefer` when that node runs one, else the first
    node's that does. "" when no node runs one.
    """
    import k8s

    nodes = podman_nodes_running(definition)
    if not nodes:
        return ""
    node = prefer if prefer in nodes else nodes[0]
    return podman_url(node, k8s.addon_node_config(definition, OLLAMA, node))


def validate_podman(v, addon: str) -> None:
    """The preflight of the fields every add-on of this module adds for podman mode."""
    placement.validate(v, addon)
    if addon == OLLAMA:
        v.vport(OLLAMA, "ollama_port")
