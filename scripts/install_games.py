#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will deploy a small "arcade" landing page linking to curated
# open-source, browser-playable games
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "games" — configurable keys:
#   games_ns      : [OPTIONAL] Kubernetes namespace          (default: games)
#   games_shorthn : [OPTIONAL] hostname prefix for ingress    (default: games)
#
# Deploys a single nginx pod serving a small static page that LINKS to a curated shortlist of
# real, open-source, browser-playable games on itch.io — it does NOT self-host or redistribute
# any game's assets (checked each game's own itch.io page live before listing it here, rather than
# mirroring files whose redistribution terms weren't individually confirmed). Curated shortlist
# (confirmed real itch.io URLs, 2026-09-05): SuperTux Classic (GPL, 2D platformer),
# A Dark Forest (incremental/idle), Starcatcher (one-button space platformer),
# Skynet Simulator (MIT-licensed puzzle/idle), Open Saber (rhythm/block-cutting). See
# https://itch.io/games/html5/tag-open-source for more.
#
# NOT live-tested (no cluster available in this session) — the itch.io URLs themselves were
# verified live (each game's own page fetched/confirmed to exist), the Kubernetes manifests follow
# this project's existing install_nv_testing.py pattern exactly (ConfigMap + nginx Deployment +
# Service + Ingress via process_template()), not a new mechanism.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "games",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run, process_template  # noqa: E402

_TEMPLATES = ("namespace.yml.tmpl", "configmap.yml.tmpl", "deployment.yml.tmpl",
              "service.yml.tmpl", "ingress.yml.tmpl")


def _validate(v):
    v.vns("games")


def setup_games(hostname, templ_addons_loc, cfg):
    """Deploy the games-arcade landing page. Mirrors install_nv_testing.py's own pattern."""
    for tmpl_name in _TEMPLATES:
        tmpl = "{}/games/{}".format(str(templ_addons_loc).rstrip("/"), tmpl_name)
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, cfg))

    ns = cfg.get("games_ns") or "games"
    shorthn = cfg.get("games_shorthn") or "games"
    print("Games arcade deployed. Namespace: {}".format(ns))
    print("Available at: http://{}.{}.{}".format(shorthn, cfg.get("clu_name"), cfg.get("mydomain")))


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

    defaults = primary.load_defaults()
    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = dict(definition.get("games", {}) or {})
    cfg["clu_name"] = clu_name
    cfg["mydomain"] = clu_cfg.get("mydomain")

    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    setup_games(vm_name, templ_addons_loc, cfg)


if __name__ == "__main__":
    main()
