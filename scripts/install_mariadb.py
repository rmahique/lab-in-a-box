#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install mariadb
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "mariadb" — MariaDB database on Kubernetes
#
#   mariadb_ns      : [OPTIONAL] Kubernetes namespace                  (default: db)
#   mariadb_name    : [OPTIONAL] Deployment and service name           (default: mariadb)
#   mariadb_version : [OPTIONAL] Helm chart version                    (empty = latest)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "mariadb",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
from lab_creation import ssh_run, process_template  # noqa: E402


def _validate(v):
    v.vns("mariadb")
    v.vver("mariadb")
    v.vurl("mariadb")


def setup_mariadb(hostname, templ_addons_loc, mariadb_cfg):
    """
    Delete any pre-existing deployment/service, then render+apply the
    mariadb manifest template. Mirrors setup_mariadb (bash).

    mariadb_cfg : the raw "mariadb" JSON section dict — passed straight
    through to process_template, whose bash-side ${VAR:-default} defaults in
    the template itself supply the fallback for anything not set here (same
    mechanism bash used, since this shells out to the identical eval/heredoc
    primitive).
    """
    ns = mariadb_cfg.get("mariadb_ns") or "db"
    name = mariadb_cfg.get("mariadb_name") or "mariadb"
    # No error check in bash either — deleting a deployment/service that
    # doesn't exist yet (first run) is expected to "fail" harmlessly.
    ssh_run(hostname, "kubectl delete -n {} deployment.apps/{} service/{}".format(ns, name, name), check=False)

    tmpl = "{}/mariadb/install.yml.tmpl".format(str(templ_addons_loc).rstrip("/"))
    rendered = process_template(tmpl, mariadb_cfg)
    ssh_run(hostname, "kubectl apply -f -", input_text=rendered)


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()

    # bash uses the first node in the JSON unconditionally here — no
    # INSTALL_RKE2_TYPE=="server" filter like most other addons (and always
    # `exit 1` at the end, by design or oversight; setup_lab.sh never checks
    # an addon's exit code either way, so this has no observable effect on
    # the pipeline — see MIGRATION_TODO.md).
    nodes = list(definition.get("nodes", {}))
    if not nodes:
        sys.exit(1)
    vm_name = nodes[0]

    mariadb_cfg = definition.get("mariadb", {}) or {}
    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")

    print("# Using node: {}".format(vm_name))
    setup_mariadb(vm_name, templ_addons_loc, mariadb_cfg)

    time.sleep(60)
    sys.exit(1)


if __name__ == "__main__":
    main()
