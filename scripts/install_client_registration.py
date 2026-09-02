#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will register a host as a Salt client of an
# existing Uyuni/SMLM server
# Author/s: Raul Mahiques
# License: GPLv3
#
# Reference: https://www.uyuni-project.org/uyuni-docs/en/uyuni/client-configuration/registration-bootstrap.html
#            https://documentation.suse.com/multi-linux-manager/5.2/en/docs/client-configuration/registration-bootstrap.html
# (verified live 2026-08-28 — identical mechanism across Uyuni and every
# current SMLM version; see libs/spacecmd_common.py's
# module docstring, "Client registration" section, for the full research
# notes and caveats)
#
# This is the CLIENT side — install_uyuni.py/install_smlm.py install the
# SERVER. This addon runs on a plain VM/baremetal node (nodes[x].addons, not
# a kcluster addon) and points it at an already-running server elsewhere in
# the lab (or outside it entirely — client_registration_server just needs to
# be reachable by FQDN).
#
# ─── JSON section: "client_registration" ────────────────────────────────────
#
# MANDATORY
#   client_registration_server           : FQDN of the target Uyuni/SMLM server
#                                           (the bootstrap URL is built from this:
#                                           https://<server>/pub/bootstrap/bootstrap.sh)
#   client_registration_activation_key   : activation key label to bootstrap with
#                                           (e.g. "1-mykey") — created on the server
#                                           if it doesn't already exist, see below
#
# OPTIONAL – server access (for the "ensure the key/channels exist" preflight
#            and salt-key acceptance; same two deployment shapes install_uyuni.py/
#            install_smlm.py themselves use)
#   client_registration_server_type      : "uyuni" (default, single-VM podman/mgradm)
#                                           or "smlm" (Kubernetes, kubectl exec)
#                                           (options: uyuni, smlm)
#   client_registration_server_node      : SSH host to run spacecmd/mgrctl/kubectl
#                                           commands on (default: same as
#                                           client_registration_server for "uyuni";
#                                           REQUIRED for "smlm" — the k8s node
#                                           running kubectl isn't necessarily the
#                                           server's own ingress FQDN)
#   client_registration_server_ns        : Kubernetes namespace ("smlm" only,
#                                           default: uyuni-server)
#   client_registration_admin_user       : spacecmd admin user (default: admin)
#   client_registration_admin_pass       : spacecmd admin password
#                                           (default: Uyuni12345 for "uyuni",
#                                           admin123 for "smlm" — each product's
#                                           own install default)
#
# OPTIONAL – activation key auto-creation, if client_registration_activation_key
#            doesn't already exist on the server (same fields
#            ensure_activation_key already expects under any prefix — see
#            install_uyuni.py's own uyuni_activation_key_* for the full set)
#   client_registration_activation_key_base_channel   : required to CREATE the key
#   client_registration_activation_key_child_channels : space-separated
#   client_registration_activation_key_desc           : default: the key name
#   client_registration_sync_channels                 : space-separated channels
#                                           to mgr-sync before creating the key,
#                                           if they aren't already synced
#
# OPTIONAL – bootstrap behavior
#   client_registration_reactivation_key : passed as REACTIVATION_KEY, for
#                                           re-registering a previously
#                                           registered system
#   client_registration_retry_limit      : how many times to poll for the
#                                           minion's key to appear pending
#                                           after bootstrap (default: 30)
#   client_registration_retry_interval   : seconds between polls (default: 10)

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "client_registration",
    "targets": ["vm", "baremetal"],
    "layers": ["os-native"],
    "requires_kubernetes": None,
    "aux_services": [],
}

import os
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
import spacecmd_common as sc  # noqa: E402
from lab_creation import die  # noqa: E402


def _validate(v):
    v.vreq("client_registration", "client_registration_server")
    v.vreq("client_registration", "client_registration_activation_key")
    cfg = v.definition.get("client_registration", {}) or {}
    server_type = cfg.get("client_registration_server_type") or "uyuni"
    if server_type not in ("uyuni", "smlm"):
        v.errors.append(
            "[ERROR] client_registration.client_registration_server_type='{}': "
            "must be 'uyuni' or 'smlm'".format(server_type))
    if server_type == "smlm":
        v.vreq("client_registration", "client_registration_server_node")


def _server_access(cfg):
    """
    Returns (server_node, exec_prefix, admin_user, admin_pass) for the two
    deployment shapes install_uyuni.py/install_smlm.py themselves use.
    """
    server_type = cfg.get("client_registration_server_type") or "uyuni"
    server_fqdn = cfg.get("client_registration_server")
    admin_user = cfg.get("client_registration_admin_user") or "admin"

    if server_type == "smlm":
        server_node = cfg.get("client_registration_server_node")
        if not server_node:
            die("client_registration_server_node is required when "
                "client_registration_server_type is 'smlm'")
        ns = cfg.get("client_registration_server_ns") or "uyuni-server"
        exec_prefix = "kubectl exec -n {} deploy/uyuni -c uyuni --".format(ns)
        admin_pass = cfg.get("client_registration_admin_pass") or "admin123"
    else:
        server_node = cfg.get("client_registration_server_node") or server_fqdn
        exec_prefix = "mgrctl exec --"
        admin_pass = cfg.get("client_registration_admin_pass") or "Uyuni12345"

    return server_node, exec_prefix, admin_user, admin_pass


def register_client(vm_name, cfg):
    server_fqdn = cfg.get("client_registration_server")
    activation_key = cfg.get("client_registration_activation_key")
    if not server_fqdn or not activation_key:
        die("client_registration_server and client_registration_activation_key are required")

    server_node, exec_prefix, admin_user, admin_pass = _server_access(cfg)

    sc.ensure_spacecmd_config(server_node, exec_prefix, admin_user, admin_pass)
    sync_channels = (cfg.get("client_registration_sync_channels") or "").split()
    sc.ensure_channels_synced(server_node, exec_prefix, sync_channels)
    sc.ensure_activation_key(server_node, exec_prefix, cfg, "client_registration")

    sc.ensure_client_registered(
        server_node, exec_prefix, vm_name, server_fqdn, activation_key,
        reactivation_key=cfg.get("client_registration_reactivation_key"),
        retry_limit=int(cfg.get("client_registration_retry_limit") or 30),
        retry_interval=int(cfg.get("client_registration_retry_interval") or 10),
    )


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    cfg = definition.get("client_registration", {}) or {}

    env_vm_name = os.environ.get("_vm_name") or None
    for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "client_registration", vm_name=env_vm_name):
        register_client(vm_name, cfg)


if __name__ == "__main__":
    main()
