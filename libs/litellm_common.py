#!/usr/bin/env python3
"""
LiteLLM proxy (https://docs.litellm.ai): an OpenAI-compatible endpoint in front of one hosted model, installed from the
litellm-helm chart. Used by the anthropic, gemini, kimi and openai add-ons, and by every add-on in PROVIDERS below,
each of which is a script that names its provider and delegates the whole install to install_provider().

The proxy runs without a database (no virtual keys or spend tracking). Clients authenticate with the master key kept in
the <release>-master-key secret; the provider's API key is read from the <release>-provider secret.

Listed in a node's own addons[] instead, the proxy is a podman container on that node, "<section>-proxy", answering
at http://<node>:<section>_port/v1 (HOST_PORTS gives every add-on its own default, so several share a node). Its
configuration is /etc/litellm/<section>/config.yaml there, and both keys are in /etc/litellm/<section>/env, a root-only
file podman reads itself: the master key is generated once and kept, the provider key is the lab's.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import json
import secrets
import shlex

import placement
import podman_service as ps
from lab_creation import ssh_run

DEFAULT_CHART = "oci://ghcr.io/berriai/litellm-helm"
PORT = 4000
DEFAULT_IMAGE = "ghcr.io/berriai/litellm"
DEFAULT_IMAGE_TAG = "main-stable"
CONFIG_ROOT = "/etc/litellm"
# The default host port of each add-on's podman-mode proxy, one per add-on so that any of them can share a node.
HOST_PORTS = {
    "anthropic": 4000, "openai": 4001, "gemini": 4002, "kimi": 4003, "ai21": 4004, "cerebras": 4005, "cohere": 4006,
    "dashscope": 4007, "deepinfra": 4008, "ernie": 4009, "fireworks": 4010, "groq": 4011, "huggingface": 4012,
    "hyperbolic": 4013, "jina": 4014, "nebius": 4015, "novita": 4016, "openrouter": 4017, "perplexity": 4018,
    "replicate": 4019, "sambanova": 4020, "together": 4021, "voyage": 4022, "xai": 4023, "zai": 4024,
}
# How long a fresh proxy gets to answer.
START_WAIT = 300

# One hosted provider per add-on that delegates to install_provider(): its name as both the add-on and its JSON
# section, then (label, LiteLLM route, where its API key comes from).
#
# The route is the prefix LiteLLM normally expects in a model id, e.g. "groq/llama-3.3-70b-versatile". It is
# documentation, not a rule: no add-on validates a model against it, because LiteLLM's own routes move and a
# provider may also be reached as an OpenAI-compatible endpoint, with an "openai/" model and <name>_api_base. Each
# add-on's <name>_model field is MANDATORY for the same reason: a model id this project guessed would fail at the
# provider, long after the install reported success. Confirm both at https://docs.litellm.ai/docs/providers.
PROVIDERS = {
    "ai21": ("AI21", "ai21/", "https://studio.ai21.com"),
    "cerebras": ("Cerebras", "cerebras/", "https://cloud.cerebras.ai"),
    "cohere": ("Cohere", "cohere/", "https://dashboard.cohere.com"),
    "dashscope": ("Alibaba Cloud DashScope", "dashscope/", "https://dashscope.console.aliyun.com"),
    "deepinfra": ("DeepInfra", "deepinfra/", "https://deepinfra.com/dash/api_keys"),
    "ernie": ("Baidu ERNIE", "ernie/", "https://console.bce.baidu.com"),
    "fireworks": ("Fireworks AI", "fireworks_ai/", "https://fireworks.ai"),
    "groq": ("Groq", "groq/", "https://console.groq.com"),
    "huggingface": ("Hugging Face Inference", "huggingface/", "https://huggingface.co/settings/tokens"),
    "hyperbolic": ("Hyperbolic", "hyperbolic/", "https://app.hyperbolic.xyz"),
    "jina": ("Jina AI embeddings", "jina_ai/", "https://jina.ai"),
    "nebius": ("Nebius AI Studio", "nebius/", "https://studio.nebius.com"),
    "novita": ("Novita AI", "novita/", "https://novita.ai"),
    "openrouter": ("OpenRouter", "openrouter/", "https://openrouter.ai/keys"),
    "perplexity": ("Perplexity", "perplexity/", "https://www.perplexity.ai/settings/api"),
    "replicate": ("Replicate", "replicate/", "https://replicate.com/account/api-tokens"),
    "sambanova": ("SambaNova Cloud", "sambanova/", "https://cloud.sambanova.ai"),
    "together": ("Together AI", "together_ai/", "https://api.together.ai"),
    "voyage": ("Voyage AI embeddings", "voyage/", "https://dash.voyageai.com"),
    "xai": ("xAI Grok", "xai/", "https://console.x.ai"),
    "zai": ("Z.ai GLM", "zai/", "https://z.ai"),
}


def provider(name: str) -> tuple:
    """(label, route, console URL) of the hosted provider `name`, from PROVIDERS."""
    if name not in PROVIDERS:
        raise KeyError("no LiteLLM provider '{}' — add it to litellm_common.PROVIDERS".format(name))
    return PROVIDERS[name]


def validate_provider(v, section: str) -> None:
    """
    Validate the JSON section of a provider add-on: its key and its model are both required, and the rest follows
    the shapes every add-on's fields do. The model is required rather than defaulted, see PROVIDERS.
    """
    v.vreq(section, "{}_api_key".format(section))
    v.vreq(section, "{}_model".format(section))
    v.vns(section)
    v.vver(section)
    v.vurl_field(section, "{}_api_base".format(section))
    validate_podman(v, section)


def validate_podman(v, section: str) -> None:
    """The preflight of the podman-mode fields every proxy add-on carries."""
    placement.validate(v, section)
    v.vport(section, "{}_port".format(section))


def _require(section: str, cfg: dict, default_model: str = "") -> None:
    """Stop when `cfg` lacks the provider key, or the model when the add-on has no default for it."""
    from lab_creation import die

    label, console = (provider(section)[0], provider(section)[2]) if section in PROVIDERS else (section, "")
    if not cfg.get("{}_api_key".format(section)):
        die("{}: {}_api_key is mandatory{}".format(section, section,
                                                  ": the {} key comes from {}".format(label, console) if console else ""))
    if not default_model and not cfg.get("{}_model".format(section)):
        die("{}: {}_model is mandatory: the model is named rather than defaulted because a guessed model id fails at "
            "the provider, not here".format(section, section))


def install_provider(definition: dict, section: str) -> str:
    """
    Install the LiteLLM proxy for the provider add-on `section` on the lab's kcluster, register its hostname in
    DNS, and return that hostname; or, in podman mode, on every node that lists it, returning "". The whole body of
    such an add-on's main(), so each one is its own header, PLUGIN and provider name and nothing else.
    """
    import addon_common as ac
    import k8s
    from lab_creation import die, setup_helm

    label, _route, console = provider(section)
    cfg = definition.get(section, {}) or {}
    if placement.deployment(definition, section, cfg) != placement.KUBERNETES:
        install_podman_nodes(definition, section, section)
        return ""
    _require(section, cfg)

    target = k8s.first_server_node(definition)
    if not target:
        die("{}: this lab has no kcluster server node to install the proxy on".format(section))
    vm_name, _ssh_cmd = target
    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}

    setup_helm(vm_name, clu_name, online=definition.get("common", {}).get("online") == "1")
    fqdn = ".".join(p for p in (str(cfg.get("{}_shorthn".format(section)) or section), clu_name,
                                clu_cfg.get("mydomain", "")) if p)
    setup_litellm_proxy(vm_name, "{}-proxy".format(section.replace("_", "-")),
                        cfg.get("{}_ns".format(section)) or section, fqdn, section,
                        str(cfg["{}_model".format(section)]), str(cfg["{}_api_key".format(section)]),
                        chart=cfg.get("{}_chart".format(section)) or "",
                        version=cfg.get("{}_version".format(section)) or "",
                        api_base=cfg.get("{}_api_base".format(section)) or "")
    print("{} is proxied at http://{}/v1 — any OpenAI client reaches it there".format(label, fqdn))
    ac.register_service_dns(definition, clu_name, fqdn)
    return fqdn


def _env_cmd(var: str, value: str, cmd: str) -> str:
    """`cmd` run with `value` in environment variable `var`, so the value appears in no command's arguments."""
    return "{}={} bash -c {}".format(var, shlex.quote(value), shlex.quote(cmd))


def provider_secret(hostname: str, ns: str, release: str, api_key: str) -> None:
    """Create or update <release>-provider with PROVIDER_API_KEY."""
    ssh_run(hostname, _env_cmd("PROVIDER_API_KEY", api_key,
                               "kubectl create secret generic {}-provider -n {} --from-literal=PROVIDER_API_KEY=\"$PROVIDER_API_KEY\" "
                               "--dry-run=client -o yaml | kubectl apply -f -".format(release, shlex.quote(ns))))


def master_key_secret(hostname: str, ns: str, release: str) -> None:
    """Create <release>-master-key with a random sk-… key when it does not exist; an existing key is kept."""
    name = "{}-master-key".format(release)
    ssh_run(hostname, _env_cmd("LITELLM_MASTER_KEY", "sk-" + secrets.token_hex(16),
                               "kubectl get secret {0} -n {1} >/dev/null 2>&1 || kubectl create secret generic {0} -n {1} "
                               "--from-literal=masterkey=\"$LITELLM_MASTER_KEY\"".format(name, shlex.quote(ns))))


def chart_values(release: str, fqdn: str, model_name: str, model: str, api_base: str = "") -> list:
    """--set flags: one model reading its key from the provider secret, no database, the master-key secret, an ingress."""
    pairs = [
        ("db.deployStandalone", "false"),
        ("migrationJob.enabled", "false"),
        ("masterkeySecretName", "{}-master-key".format(release)),
        ("masterkeySecretKey", "masterkey"),
        ("environmentSecrets[0]", "{}-provider".format(release)),
        ("proxy_config.model_list[0].model_name", model_name),
        ("proxy_config.model_list[0].litellm_params.model", model),
        ("proxy_config.model_list[0].litellm_params.api_key", "os.environ/PROVIDER_API_KEY"),
    ]
    if api_base:
        pairs.append(("proxy_config.model_list[0].litellm_params.api_base", api_base))
    pairs += [
        ("ingress.enabled", "true"),
        ("ingress.className", ""),
        ("ingress.hosts[0].host", fqdn),
        ("ingress.hosts[0].paths[0].path", "/"),
        ("ingress.hosts[0].paths[0].pathType", "Prefix"),
    ]
    return ["--set {}".format(shlex.quote("{}={}".format(k, v))) for k, v in pairs]


def setup_litellm_proxy(hostname: str, release: str, ns: str, fqdn: str, model_name: str, model: str, api_key: str,
                        chart: str = "", version: str = "", api_base: str = "") -> None:
    """Install or upgrade the proxy `release` in `ns`, serving `model` as `model_name` at http://<fqdn>/v1."""
    ssh_run(hostname, "kubectl create namespace {0} --dry-run=client -o yaml | kubectl apply -f -".format(shlex.quote(ns)))
    provider_secret(hostname, ns, release, api_key)
    master_key_secret(hostname, ns, release)
    ver_arg = " --version {}".format(shlex.quote(version)) if version else ""
    ssh_run(hostname, "helm upgrade --install {} {} --namespace {}{} --wait --timeout 10m {}".format(
        release, shlex.quote(chart or DEFAULT_CHART), shlex.quote(ns), ver_arg,
        " ".join(chart_values(release, fqdn, model_name, model, api_base))))
    print("LiteLLM proxy installed. Namespace: {}".format(ns))
    print("OpenAI-compatible endpoint: http://{}/v1  (model: {})".format(fqdn, model_name))
    print("API key: kubectl -n {} get secret {}-master-key -o jsonpath='{{.data.masterkey}}' | base64 -d".format(ns, release))
    print("Usage: curl http://{}/v1/chat/completions -H \"Authorization: Bearer <API key>\" -H 'Content-Type: application/json' "
          "-d '{{\"model\": \"{}\", \"messages\": [{{\"role\": \"user\", \"content\": \"Hello\"}}]}}'".format(fqdn, model_name))


# ── podman mode ──────────────────────────────────────────────────────────────

def podman_port(section: str, cfg: dict) -> int:
    """The host port `section`'s podman-mode proxy answers on: <section>_port, else its HOST_PORTS default."""
    return int((cfg or {}).get("{}_port".format(section)) or HOST_PORTS.get(section, PORT))


def config_yaml(model_name: str, model: str, api_base: str = "") -> str:
    """The proxy's config.yaml: one model reading the provider key, and the master key, both from the environment."""
    params = [("model", model), ("api_key", "os.environ/PROVIDER_API_KEY")]
    if api_base:
        params.append(("api_base", api_base))
    lines = ["model_list:", "  - model_name: {}".format(json.dumps(model_name)), "    litellm_params:"]
    lines += ["      {}: {}".format(k, json.dumps(v)) for k, v in params]
    lines += ["general_settings:", "  master_key: \"os.environ/LITELLM_MASTER_KEY\"", ""]
    return "\n".join(lines)


def setup_litellm_podman(hostname: str, section: str, model_name: str, model: str, api_key: str, cfg: dict,
                         api_base: str = "") -> str:
    """Run `section`'s proxy as a podman container on `hostname`, serving `model` as `model_name`; returns its URL."""
    conf_dir = "{}/{}".format(CONFIG_ROOT, section)
    env_file = "{}/env".format(conf_dir)
    port = podman_port(section, cfg)
    image = "{}:{}".format(cfg.get("{}_image".format(section)) or DEFAULT_IMAGE,
                           cfg.get("{}_image_tag".format(section)) or DEFAULT_IMAGE_TAG)
    master_key = ps.env_value(hostname, env_file, "LITELLM_MASTER_KEY") or "sk-" + secrets.token_hex(16)
    ps.write_env_file(hostname, env_file, {"PROVIDER_API_KEY": api_key, "LITELLM_MASTER_KEY": master_key})
    ssh_run(hostname, "cat > {}/config.yaml".format(shlex.quote(conf_dir)),
            input_text=config_yaml(model_name, model, api_base))
    ps.deploy(hostname, "{}-proxy".format(section.replace("_", "-")), image,
              description="LiteLLM proxy for {} (lab-in-a-box)".format(section),
              ports=["{}:{}".format(port, PORT)], env_file=env_file,
              volumes=["{}/config.yaml:/app/config.yaml:ro,Z".format(conf_dir)],
              args=["--config", "/app/config.yaml", "--port", str(PORT)])
    ps.wait_for_http(hostname, "http://127.0.0.1:{}/health/liveliness".format(port), timeout=START_WAIT,
                     what="LiteLLM proxy for {}".format(section))
    url = placement.node_url(hostname, port, "/v1")
    print("LiteLLM proxy for {} on {}. OpenAI-compatible endpoint: {}  (model: {})".format(
        section, hostname, url, model_name))
    print("API key: sed -n 's/^LITELLM_MASTER_KEY=//p' {} on {}".format(env_file, hostname))
    print("The node itself must allow traffic on port {}: this add-on manages no firewall or security group".format(
        port))
    return url


def install_podman_nodes(definition: dict, section: str, model_name: str, default_model: str = "",
                         default_api_base: str = "") -> list:
    """
    Run `section`'s proxy on every node a podman-mode run installs on, each with its own view of the section
    (k8s.addon_node_config()). `default_model` and `default_api_base` are the add-on's own defaults, "" for a
    PROVIDERS add-on, whose model is mandatory. Returns the URLs.
    """
    import k8s

    urls = []
    for node in placement.podman_nodes(definition, section):
        cfg = k8s.addon_node_config(definition, section, node)
        _require(section, cfg, default_model)
        urls.append(setup_litellm_podman(
            node, section, model_name, str(cfg.get("{}_model".format(section)) or default_model),
            str(cfg["{}_api_key".format(section)]), cfg,
            api_base=str(cfg.get("{}_api_base".format(section)) or default_api_base)))
    return urls
