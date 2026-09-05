#!/usr/bin/env python3.11
"""
Post-deploy check for tests/examples/multi-host.json (README's "Spreading a
cluster across two hosts" example) — confirms all three nodes joined and
are Ready, AND that srv1 actually landed on the pinned hypervisor (not just
"the cluster works", which wouldn't catch a kvm_host pin silently being
ignored).

Requires KVM_HOSTS in /etc/lab_creation.cfg to already list at least
hv1.mydemo.lab (and a second host for the agents to auto-place onto) — see
README's "Multi-host labs" section. This check only validates the RESULT;
it doesn't set up the multi-host config itself.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CheckFailure, require, require_all_nodes_ready, require_ssh_reachable  # noqa: E402

SERVER = "srv1.mydemo.lab"
AGENTS = ("agent1.mydemo.lab", "agent2.mydemo.lab")
PINNED_HOST = "hv1.mydemo.lab"


def main():
    require_ssh_reachable(SERVER)
    for agent in AGENTS:
        require_ssh_reachable(agent)
    require_all_nodes_ready(SERVER, expected_count=1 + len(AGENTS))

    # Confirm srv1 really landed on the pinned host: resolve_kvm_host()/
    # locate_kvm_host() decide this at deploy time, but the only
    # after-the-fact, backend-agnostic way to confirm it from outside is to
    # ask the pinned hypervisor itself whether it has a domain by this name.
    require(PINNED_HOST, "virsh dominfo srv1.mydemo.lab", contains="running",
            label="srv1 actually running on its pinned kvm_host")
    print("OK: {} Ready node(s), srv1 confirmed on {}".format(1 + len(AGENTS), PINNED_HOST))


if __name__ == "__main__":
    try:
        main()
    except CheckFailure as e:
        print("FAILED:", e, file=sys.stderr)
        sys.exit(1)
