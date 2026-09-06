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
#   home_assistant_image_tag     : [OPTIONAL] Home Assistant image tag (default: 2026.7.0 —
#                                  DELIBERATELY pinned below "stable"; see the live-test note in
#                                  setup_home_assistant()'s own body for the real reason why)
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
# LIVE-TESTED 2026-09-06 on a disposable single-node RKE2 cluster on nuc6.mydemo.lab — full
# success end-to-end (a real 302 to /onboarding.html through the real Traefik ingress, not just
# "helm said deployed"), but only after finding and fixing THREE real bugs along the way (none
# guessable from code review alone): (1) `configuration.enabled=true` alone doesn't put Home
# Assistant behind this addon's own ingress — it also needs `configuration.forceInit=true` and an
# explicit `configuration.templateConfig` override with a real http:/trusted_proxies block,
# otherwise a "400: Bad Request" comes back for every request; (2) `forceInit` specifically matters
# on any RE-run against an already-provisioned PVC (a first-ever install onto a fresh volume would
# have worked either way — this only bit an iterate-and-redeploy cycle, but that's a real scenario
# this project's own `setup_lab.py` re-runs hit routinely); (3) the image tag needed pinning below
# "stable" entirely — see that same in-body comment for the full chase (Home Assistant 2026.7.5+
# stopped honoring YAML-based trusted-proxy config at runtime even though it accepts the YAML
# without error, a genuine, currently-open upstream transition gap).

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
                          home_assistant_storage_size=None, home_assistant_storage_class=None,
                          home_assistant_image_tag=None):
    """Install Home Assistant."""
    rel = home_assistant_rel or "pajikos"
    ns = home_assistant_ns or "home-assistant"
    ver_arg = "--version {}".format(shlex.quote(home_assistant_version)) if home_assistant_version else ""
    fqdn = "{}.{}.{}".format(home_assistant_shorthn or "home-assistant", clu_name, mydomain)
    size = home_assistant_storage_size or "5Gi"
    # Pinned below the version where this genuinely broke — see the long comment right below.
    image_tag = home_assistant_image_tag or "2026.7.0"

    set_args = [
        "--set ingress.enabled=true",
        "--set ingress.hosts[0].host={}".format(shlex.quote(fqdn)),
        "--set ingress.hosts[0].paths[0].path=/",
        "--set ingress.hosts[0].paths[0].pathType=Prefix",
        "--set persistence.enabled=true",
        "--set persistence.size={}".format(shlex.quote(size)),
        "--set image.tag={}".format(shlex.quote(image_tag)),
    ]
    if home_assistant_storage_class:
        set_args.append("--set persistence.storageClass={}".format(shlex.quote(home_assistant_storage_class)))

    # The image tag is DELIBERATELY PINNED below "stable" — a real, live-confirmed root cause,
    # chased down 2026-09-06, not guessed: Home Assistant 2026.7.5+ deprecated YAML `http:`
    # config (trusted_proxies/use_x_forwarded_for) in favor of a Settings > System > Network UI
    # screen. On the actual current "stable" release (confirmed live: 2026.9.1), the YAML http:
    # block is written to configuration.yaml correctly (this addon's own
    # configuration.templateConfig override below does force it onto disk either way) but is NO
    # LONGER HONORED AT RUNTIME — every request through this addon's own ingress still comes back
    # "400: Bad Request" ("HTTP integration is not set-up for reverse proxies" in the pod's own
    # logs), because trusted-proxy config now genuinely lives in Home Assistant's own internal
    # `.storage/` state, not configuration.yaml, on that release. That Network UI screen is itself
    # unreachable through a reverse proxy until trusted proxies are already configured — a real,
    # currently-open chicken-and-egg gap Home Assistant's own community has flagged (see
    # community.home-assistant.io "HTTP YAML Deprecation: how to access new install behind proxy").
    # Confirmed live, cleanly (fresh PVC, no downgrade-storage-version confound): pinning to
    # 2026.7.0 — the last release before that deprecation — starts cleanly and DOES still honor
    # this YAML config; a real `curl` through the ingress gets a normal 302 to /onboarding.html
    # (not a 400). Override home_assistant_image_tag once Home Assistant ships a scriptable way to
    # set trusted proxies without the UI (or once this chart adds one), not before.
    values_yaml = (
        "configuration:\n"
        "  enabled: true\n"
        # Without this, an already-provisioned PVC (e.g. a previous run of this addon, or any
        # earlier install) keeps its OLD configuration.yaml forever — confirmed live 2026-09-06:
        # the setup-config init container only writes the template on first init, so upgrading
        # this addon's own Helm values alone silently has no effect on an existing volume.
        "  forceInit: true\n"
        "  templateConfig: |-\n"
        "    default_config:\n"
        "    frontend:\n"
        "      themes: !include_dir_merge_named themes\n"
        "    automation: !include automations.yaml\n"
        "    script: !include scripts.yaml\n"
        "    scene: !include scenes.yaml\n"
        "    http:\n"
        "      use_x_forwarded_for: true\n"
        "      trusted_proxies:\n"
        "        - 10.0.0.0/8\n"
        "        - 172.16.0.0/12\n"
        "        - 192.168.0.0/16\n"
        "        - 127.0.0.0/8\n"
    )
    ssh_run(hostname, "cat > /tmp/home-assistant-values.yaml", input_text=values_yaml)

    ssh_run(hostname,
            "helm upgrade -i home-assistant {}/home-assistant --namespace {} --create-namespace "
            "-f /tmp/home-assistant-values.yaml {} {}; rm -f /tmp/home-assistant-values.yaml".format(
                rel, ns, " ".join(set_args), ver_arg))

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
                          home_assistant_storage_class=cfg.get("home_assistant_storage_class"),
                          home_assistant_image_tag=cfg.get("home_assistant_image_tag"))


if __name__ == "__main__":
    main()
