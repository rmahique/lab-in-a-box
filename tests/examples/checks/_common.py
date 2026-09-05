"""
tests/examples/checks/_common.py — shared helpers for the README-example
post-deploy checks (tests/examples/run_example.sh).

These run against REAL, just-deployed VMs (no mocking) — that's the whole
point of this test category: setup_lab.py exiting 0 only proves virt-install
and the provisioning scripts didn't error out, not that the thing it built
actually works (a cluster that never goes Ready, a service that never
listens, an addon that silently no-oped). Import lab_creation/k8s the same
way tests/checks/*.py already does, and reuse their real ssh_run() — nothing
new invented here.
"""
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation as lc  # noqa: E402


class CheckFailure(Exception):
    """Raised by a check_*() function to fail the overall example with a clear reason."""


def ssh(hostname, cmd, **kwargs):
    """Thin passthrough to lc.ssh_run — see its own docstring for kwargs."""
    return lc.ssh_run(hostname, cmd, **kwargs)


def require(hostname, cmd, contains=None, label=None):
    """
    Run `cmd` on `hostname` over SSH; raise CheckFailure if it exits nonzero,
    or if `contains` is given and doesn't appear in stdout. Returns stdout.
    """
    label = label or cmd
    result = ssh(hostname, cmd, check=False, capture=True)
    if result.returncode != 0:
        raise CheckFailure("{} on {}: exit {} — {}".format(
            label, hostname, result.returncode, (result.stderr or result.stdout or "").strip()))
    if contains is not None and contains not in (result.stdout or ""):
        raise CheckFailure("{} on {}: expected to find {!r} in output, got: {!r}".format(
            label, hostname, contains, (result.stdout or "").strip()))
    return (result.stdout or "").strip()


def require_ssh_reachable(hostname):
    """Confirm `hostname` answers a trivial SSH command at all."""
    require(hostname, "true", label="SSH reachable")


def require_all_nodes_ready(hostname, expected_count=None):
    """
    Confirm every node `kubectl get nodes` lists is Ready, run from `hostname`
    (any node in the cluster — RKE2/K3s both ship kubectl + a working
    kubeconfig for root once installed). Optionally also check the node
    COUNT matches expected_count, catching a node that silently never
    joined rather than just checking the ones that did are healthy.
    """
    out = require(hostname, "kubectl get nodes --no-headers", label="kubectl get nodes")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if expected_count is not None and len(lines) != expected_count:
        raise CheckFailure("kubectl get nodes on {}: expected {} node(s), got {}:\n{}".format(
            hostname, expected_count, len(lines), out))
    not_ready = [ln for ln in lines if " Ready" not in ln]
    if not_ready:
        raise CheckFailure("kubectl get nodes on {}: node(s) not Ready:\n{}".format(
            hostname, "\n".join(not_ready)))


def require_helm_release(hostname, release, namespace):
    """Confirm a helm release is deployed (status "deployed") in namespace."""
    out = require(hostname, "helm status {} -n {} --no-headers 2>/dev/null || true".format(release, namespace),
                   label="helm status {}".format(release))
    if "STATUS: deployed" not in out:
        raise CheckFailure("helm release '{}' in namespace '{}' on {} is not deployed:\n{}".format(
            release, namespace, hostname, out))
