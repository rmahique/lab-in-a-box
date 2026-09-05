"""
k8s.py — Kubernetes cluster setup and addon execution helpers.

Python equivalent of k8s_functions.bash.

Typical usage:
    from k8s import (
        list_kclusters, get_vm_kcluster, load_kclu_vars,
        setup_k3s, setup_rke2,
        add_kclu_dns,
        first_server_node, addon_nodes, iter_cluster_nodes,
    )
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import subprocess
import sys
import textwrap
from pathlib import Path

from lab_creation import (
    die, log, warn,
    ssh_run, ssh_output,
    add_service_dns,
    prepare_local_as_kubeclient,
)


# ── Cluster metadata ──────────────────────────────────────────────────────────

def list_kclusters(definition):
    """
    Return a list of all kcluster names defined in the lab definition
    (mirrors list_kclusters).
    """
    return list(definition.get("kclusters", {}).keys())


def get_vm_kcluster(definition, vm_name):
    """
    Return the kcluster name that vm_name belongs to, or '' if not set
    (mirrors get_vm_kcluster).
    """
    return definition.get("nodes", {}).get(vm_name, {}).get("kcluster", "")


def load_kclu_vars(definition, clu_name):
    """
    Return all scalar key-value pairs for a kcluster (mirrors load_kclu_vars).

    Args:
        definition : Loaded lab definition dict.
        clu_name   : The kcluster name to load.

    Returns a dict. Raises SystemExit if clu_name is not found.
    """
    if not clu_name:
        return {}
    kclusters = definition.get("kclusters", {})
    if clu_name not in kclusters:
        die("kcluster '{}' not found in definition".format(clu_name))
    return {
        k: v
        for k, v in kclusters[clu_name].items()
        if isinstance(v, (str, int, float, bool))
    }


# ── DNS ───────────────────────────────────────────────────────────────────────

def add_kclu_dns(definition, clu_name, clu_type, mydomain, remote_dns_servers=None):
    """
    Register the cluster API/service DNS entry (mirrors add_kclu_dns).

    Prefers agent nodes for round-robin; falls back to all nodes in the cluster.
    """
    add_service_dns(
        definition=definition,
        clu_name=clu_name,
        clu_type=clu_type,
        dns_entry=clu_name,
        mydomain=mydomain,
        remote_dns_servers=remote_dns_servers,
    )


# ── K3s ───────────────────────────────────────────────────────────────────────

def setup_k3s(hostname, clu_name, clu_rel, mydomain, token=None, rancher1_ip=None):
    """
    Install K3s on a node (mirrors setup_k3s).

    Call with token=None for the first server node. Pass the returned token
    to each subsequent node to join the cluster.

    Args:
        hostname    : Target VM hostname or IP.
        clu_name    : Cluster name (used for TLS SAN).
        clu_rel     : K3s release channel (e.g. "stable", "v1.29").
        mydomain    : Lab domain (e.g. "mydemo.lab").
        token       : Join token from the first node; None for the first node.
        rancher1_ip : First server's address for joining; None for the first node.

    Returns:
        (token, rancher1_ip) — pass these to subsequent node calls.
    """
    prepare_local_as_kubeclient()
    ssh_run(hostname, "mkdir -p /etc/rancher/k3s")

    if token is None:
        log("  Installing K3s server on '{}' (first node of '{}')".format(hostname, clu_name))
        ssh_run(
            hostname,
            "curl -sfL https://get.k3s.io | "
            "INSTALL_K3S_CHANNEL={} "
            "sh -s - server --tls-san {}.{}".format(clu_rel, clu_name, mydomain)
        )
        token = ssh_output(hostname, "cat /var/lib/rancher/k3s/server/node-token")
        return token, hostname
    else:
        log("  Joining K3s cluster '{}' on '{}'".format(clu_name, hostname))
        ssh_run(
            hostname,
            "curl -sfL https://get.k3s.io | "
            "INSTALL_K3S_CHANNEL={} "
            "K3S_URL=https://{}:6443 "
            "K3S_TOKEN={} "
            "sh -".format(clu_rel, rancher1_ip, token)
        )
        return token, rancher1_ip


# ── RKE2 ─────────────────────────────────────────────────────────────────────

# These base64 blobs encode small config snippets (NetworkManager, sysctl, PATH)
# copied directly from the bash library to avoid any modification risk.
_RKE2_NM_CONF = "W2tleWZpbGVdCnVubWFuYWdlZC1kZXZpY2VzPWludGVyZmFjZS1uYW1lOmNhbGkqO2ludGVyZmFjZS1uYW1lOmZsYW5uZWwq"
_RKE2_SYSCTL  = "bmV0LmlwdjQuY29uZi5hbGwuZm9yd2FyZGluZz0xCm5ldC5pcHY2LmNvbmYuYWxsLmZvcndhcmRpbmc9MQ=="
_RKE2_PATH    = "ZXhwb3J0IFBBVEg9JFBBVEg6L29wdC9ya2UyL2JpbjovdmFyL2xpYi9yYW5jaGVyL3JrZTIvYmluLwpleHBvcnQgS1VCRUNPTkZJRz0vZXRjL3JhbmNoZXIvcmtlMi9ya2UyLnlhbWwKCg=="


def setup_rke2(
    hostname, vm_name, clu_name, clu_type, clu_rel, node_type, mydomain,
    token=None, rancher1_ip=None, install_method="",
):
    """
    Install RKE2 on a node (mirrors setup_rke2).

    Args:
        hostname       : Target VM hostname or IP.
        vm_name        : VM name (used for local config directory).
        clu_name       : Cluster name.
        clu_type       : "rke2" or similar.
        clu_rel        : Release channel (e.g. "stable").
        node_type      : "server" or "agent".
        mydomain       : Lab domain.
        token          : Join token; None for the first server node.
        rancher1_ip    : First server address for joining; None for the first node.
        install_method : RKE2 install method (usually empty for default).

    Returns:
        (token, rancher1_ip) — pass to subsequent nodes.
    """
    prepare_local_as_kubeclient()

    log("  Configuring host for RKE2 on '{}'".format(hostname))
    ssh_run(hostname, "echo '{}' | base64 -d > /etc/NetworkManager/conf.d/rke2-canal.conf; chmod 0420 /etc/NetworkManager/conf.d/rke2-canal.conf".format(_RKE2_NM_CONF))
    ssh_run(hostname, "echo '{}' | base64 -d > /etc/sysctl.d/90-rke2.conf; chmod 0420 /etc/sysctl.d/90-rke2.conf".format(_RKE2_SYSCTL))
    ssh_run(hostname, "echo '{}' | base64 -d > /etc/profile.d/rke2.sh; chmod 0420 /etc/profile.d/rke2.sh".format(_RKE2_PATH))
    ssh_run(hostname, "mkdir -p /var/lib/rancher/{0} /etc/rancher/{0}".format(clu_type))

    log("  Installing RKE2 on '{}'".format(hostname))
    ssh_run(
        hostname,
        "curl -sfL https://get.{clu_type}.io | "
        "INSTALL_RKE2_TYPE={node_type} "
        "INSTALL_RKE2_METHOD={install_method} "
        "INSTALL_RKE2_CHANNEL={clu_rel} "
        "sh -".format(
            clu_type=clu_type, node_type=node_type,
            install_method=install_method, clu_rel=clu_rel,
        )
    )

    _write_rke2_config(clu_name, node_type, clu_type, mydomain)
    config_src = str(Path(clu_name) / "config-{}.yaml".format(node_type))
    subprocess.run(
        ["rsync", "-a", config_src,
         "root@{}:/etc/rancher/{}/config.yaml".format(hostname, clu_type)],
        check=True,
    )

    if token is None:
        log("  Starting RKE2 server on '{}' (first node of '{}')".format(hostname, clu_name))
        ssh_run(hostname, "systemctl enable --now {}-server.service".format(clu_type))
        token = ssh_output(hostname, "cat /var/lib/rancher/{}/server/node-token".format(clu_type))
        return token, hostname
    else:
        log("  Joining RKE2 cluster '{}' on '{}'".format(clu_name, hostname))
        ssh_run(hostname, "echo 'server: https://{}:9345' >> /etc/rancher/{}/config.yaml".format(
            rancher1_ip, clu_type))
        ssh_run(hostname, "echo 'token: {}' >> /etc/rancher/{}/config.yaml".format(
            token, clu_type))
        ssh_run(hostname, "systemctl enable --now {}-{}.service".format(clu_type, node_type))
        return token, rancher1_ip


def _write_rke2_config(clu_name, node_type, clu_type, mydomain):
    """Write local RKE2 config files (server + agent) if they do not already exist."""
    clu_dir = Path(clu_name)
    clu_dir.mkdir(exist_ok=True)
    content = textwrap.dedent("""\
        write-kubeconfig-mode: "0600"
        tls-san:
          - "{domain}"
          - "{clu}.{domain}"
    """.format(domain=mydomain, clu=clu_name))
    for kind in ("server", "agent"):
        cfg = clu_dir / "config-{}.yaml".format(kind)
        if not cfg.exists():
            cfg.write_text(content)


# ── Node iterator helpers ─────────────────────────────────────────────────────

def first_server_node(definition):
    """
    Return (vm_name, ssh_cmd) for the first server node found in the definition,
    or None if no server node exists (mirrors on_first_server).

    A node is treated as a server when INSTALL_RKE2_TYPE is "server" or absent.
    """
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if node_cfg.get("INSTALL_RKE2_TYPE", "") in ("server", ""):
            ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new -q root@{}".format(vm_name)
            log("# Using node: {}".format(vm_name))
            return vm_name, ssh_cmd
    warn("No server node found in definition")
    return None


def addon_nodes(definition, addon, vm_name=None):
    """
    Return a list of (vm_name, ssh_cmd) for nodes that have addon in their addons[]
    list (mirrors on_addon_nodes).

    If vm_name is given, return only that node without scanning the definition.

    Returns a list of (vm_name, ssh_cmd) tuples.
    """
    if vm_name:
        ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new root@{}".format(vm_name)
        log("# Using node: {}".format(vm_name))
        return [(vm_name, ssh_cmd)]

    results = []
    for name, node_cfg in definition.get("nodes", {}).items():
        if addon in node_cfg.get("addons", []):
            ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new root@{}".format(name)
            log("# Using node: {}".format(name))
            results.append((name, ssh_cmd))

    if not results:
        warn("No node with addon '{}' found in definition".format(addon))
    return results


def iter_cluster_nodes(definition, clu_name):
    """
    Yield (vm_name, node_cfg, ssh_cmd) for every node that belongs to clu_name,
    in definition order.
    """
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if node_cfg.get("kcluster") == clu_name:
            ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new -q root@{}".format(vm_name)
            yield vm_name, node_cfg, ssh_cmd
