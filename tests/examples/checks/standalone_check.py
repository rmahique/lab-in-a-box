#!/usr/bin/env python3.11
"""
Post-deploy check for tests/examples/standalone.json (README's "Minimal
single-VM lab" example) — confirms the one VM actually came up and is
reachable, not just that setup_lab.py exited 0.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CheckFailure, require, require_ssh_reachable  # noqa: E402

NODE = "standalone.mydemo.lab"


def main():
    require_ssh_reachable(NODE)
    require(NODE, "hostname", contains=NODE.split(".")[0], label="hostname matches")
    print("OK: {} is up and reachable".format(NODE))


if __name__ == "__main__":
    try:
        main()
    except CheckFailure as e:
        print("FAILED:", e, file=sys.stderr)
        sys.exit(1)
