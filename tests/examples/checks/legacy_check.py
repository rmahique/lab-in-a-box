#!/usr/bin/env python3.11
"""
Post-deploy check for tests/examples/legacy.json (README's "Deploying a
legacy image (CentOS 7)" example) — this is the one example where the
interesting failure mode is the VM not booting AT ALL (the whole point of
the VM_MACHINE="pc" override — see the README section this mirrors), so
"is it SSH-reachable and does it identify as the right distro" already
covers the case that actually matters here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CheckFailure, require, require_ssh_reachable  # noqa: E402

NODE = "legacy1.mydemo.lab"


def main():
    require_ssh_reachable(NODE)
    require(NODE, "cat /etc/os-release", contains='ID="centos"', label="/etc/os-release identifies as CentOS")
    print("OK: {} booted (VM_MACHINE=pc worked) and is reachable".format(NODE))


if __name__ == "__main__":
    try:
        main()
    except CheckFailure as e:
        print("FAILED:", e, file=sys.stderr)
        sys.exit(1)
