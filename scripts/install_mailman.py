#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install GNU Mailman 3 (mailing-list management + web interface)
# Author/s: Raul Mahiques
# License: GPLv3
#
# JSON section: "mailman" — configurable keys:
#   mailman_ns          : [OPTIONAL] namespace (default: mailman)
#   mailman_shorthn     : [OPTIONAL] hostname prefix for mailman-web's ingress (default: mailman)
#   mailman_version     : [OPTIONAL] maxking/mailman-core & mailman-web image tag (default: 0.5)
#   mailman_admin_user  : [OPTIONAL] admin username (default: admin)
#   mailman_admin_email : [OPTIONAL] admin email (default: admin@lab.local)
#
# Deploys the real, official maxking/docker-mailman image set (mailman-core, mailman-web) plus a
# Postgres database, translated from that project's own documented docker-compose topology into
# plain Kubernetes manifests (no official Helm chart is currently published for Mailman 3 itself —
# confirmed live 2026-09-05; a community Helm chart exists, danil-smirnov/mailman-helm-chart, but
# isn't published to a fetchable chart repo URL this addon could `helm repo add`, so this uses raw
# manifests instead, same pattern as install_nv_testing.py/install_games.py).
#
# Secrets (HyperKitty API key, Django SECRET_KEY, Postgres password) are generated once and stored
# in a "mailman-secrets" Kubernetes Secret — idempotent (checked before generating, so a re-run of
# this addon doesn't rotate credentials and break an already-running deployment).
#
# KNOWN LIMITATION, confirmed against the community Helm chart's own notes rather than discovered
# the hard way: Mailman's host-key whitelisting mechanism doesn't play well with dynamic Kubernetes
# pod IPs/hostnames on a pod restart — this addon does not attempt to work around that; expect to
# need a manual fix if mailman-core's REST connection to mailman-web (or vice versa) starts
# refusing a previously-trusted host after a pod reschedule.
#
# NOT live-tested (no cluster available in this session) — image names/tags/env vars verified
# against maxking/docker-mailman's own documented compose file, 2026-09-05, not guessed. This
# lab-sized topology uses emptyDir (not PersistentVolumeClaims) for all data — deliberately
# ephemeral/quick-demo, not durable; add real PVCs (and a StorageClass — confirmed elsewhere in
# this project that a bare RKE2 cluster has none by default) before relying on this beyond a demo.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "mailman",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import secrets as _secrets
import shlex
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run, process_template  # noqa: E402

_TEMPLATES = ("namespace.yml.tmpl", "postgres.yml.tmpl", "core.yml.tmpl", "web.yml.tmpl")


def _validate(v):
    v.vns("mailman")
    v.vver("mailman")


def _get_or_create_secret_value(hostname, ns, key, length=32):
    """Idempotently fetch an existing mailman-secrets key, or generate a new random one."""
    existing = ssh_run(hostname,
                        "kubectl get secret mailman-secrets -n {} -o jsonpath='{{.data.{}}}' 2>/dev/null "
                        "| base64 -d".format(shlex.quote(ns), key),
                        check=False, capture=True)
    if existing.returncode == 0 and existing.stdout.strip():
        return existing.stdout.strip()
    return _secrets.token_urlsafe(length)


def setup_mailman(hostname, templ_addons_loc, cfg):
    """Deploy Mailman 3 (core + web + postgres)."""
    ns = cfg.get("mailman_ns") or "mailman"
    ssh_run(hostname, "kubectl create namespace {} 2>/dev/null || true".format(shlex.quote(ns)), check=False)

    db_password = _get_or_create_secret_value(hostname, ns, "db_password")
    hyperkitty_api_key = _get_or_create_secret_value(hostname, ns, "hyperkitty_api_key")
    django_secret_key = _get_or_create_secret_value(hostname, ns, "django_secret_key", length=50)

    secret_cmd = (
        "kubectl create secret generic mailman-secrets "
        "--from-literal=db_password=$MAILMAN_DB_PASSWORD "
        "--from-literal=hyperkitty_api_key=$MAILMAN_HYPERKITTY_KEY "
        "--from-literal=django_secret_key=$MAILMAN_DJANGO_KEY "
        "--namespace {} --dry-run=client -o yaml | kubectl apply -f -"
    ).format(shlex.quote(ns))
    ssh_run(hostname, "MAILMAN_DB_PASSWORD={} MAILMAN_HYPERKITTY_KEY={} MAILMAN_DJANGO_KEY={} bash -c {}".format(
        shlex.quote(db_password), shlex.quote(hyperkitty_api_key), shlex.quote(django_secret_key),
        shlex.quote(secret_cmd)))

    render_cfg = dict(cfg)
    render_cfg["mailman_db_password"] = db_password

    for tmpl_name in _TEMPLATES:
        tmpl = "{}/mailman/{}".format(str(templ_addons_loc).rstrip("/"), tmpl_name)
        ssh_run(hostname, "kubectl apply -f -", input_text=process_template(tmpl, render_cfg))

    shorthn = cfg.get("mailman_shorthn") or "mailman"
    print("Mailman 3 deployed. Namespace: {}".format(ns))
    print("Web UI available at: http://{}.{}.{}".format(shorthn, cfg.get("clu_name"), cfg.get("mydomain")))


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    defaults = primary.load_defaults()

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = dict(definition.get("mailman", {}) or {})
    cfg["clu_name"] = clu_name
    cfg["mydomain"] = clu_cfg.get("mydomain")

    templ_addons_loc = defaults.get("_templ_addons_loc", "/usr/share/lab_creation/templates/addons/")
    setup_mailman(vm_name, templ_addons_loc, cfg)


if __name__ == "__main__":
    main()
