#!/usr/bin/env python3.11
"""
Post-deploy check for tests/examples/rancher-cluster.json (README's
"RKE2 + Rancher + Longhorn" hello-world example) — confirms both nodes
joined the RKE2 cluster and are Ready, and that both addons actually
deployed, not just that setup_lab.py exited 0.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CheckFailure, require_all_nodes_ready, require_helm_release, require_ssh_reachable  # noqa: E402

SERVER = "node101.mydemo.lab"
AGENT = "node102.mydemo.lab"


def main():
    require_ssh_reachable(SERVER)
    require_ssh_reachable(AGENT)
    require_all_nodes_ready(SERVER, expected_count=2)
    require_helm_release(SERVER, "rancher", "cattle-system")
    require_helm_release(SERVER, "longhorn", "longhorn-system")
    print("OK: cluster1 has 2 Ready nodes, rancher + longhorn both deployed")


if __name__ == "__main__":
    try:
        main()
    except CheckFailure as e:
        print("FAILED:", e, file=sys.stderr)
        sys.exit(1)
