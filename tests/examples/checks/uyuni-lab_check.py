#!/usr/bin/env python3.11
"""
Post-deploy check for tests/examples/uyuni-lab.json (README's "SUSE Manager
(Uyuni) server + a registered client" example) — confirms the activation
key was actually created (not just that install_uyuni exited 0) and that
client1 actually shows up as a registered system against the server, not
just that client_registration's own script ran without error.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CheckFailure, require, require_ssh_reachable  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "libs"))
import spacecmd_common as sc  # noqa: E402

SERVER = "uyuni.mydemo.lab"
CLIENT = "client1.mydemo.lab"
EXEC_PREFIX = "mgrctl exec --"
ACTIVATION_KEY = "1-lab-clients"


def main():
    require_ssh_reachable(SERVER)
    require_ssh_reachable(CLIENT)

    if not sc.activation_key_exists(SERVER, EXEC_PREFIX, ACTIVATION_KEY):
        raise CheckFailure("activation key '{}' does not exist on {}".format(ACTIVATION_KEY, SERVER))

    # activation_key_exists() only proves the KEY exists — separately confirm
    # the client actually used it to register as a real system, not just
    # that client_registration.py's own SSH calls to the client exited 0.
    require(SERVER, "mgrctl exec -- spacecmd -- system_list", contains=CLIENT,
            label="client1 registered as a system")

    print("OK: activation key '{}' exists, {} is a registered system".format(ACTIVATION_KEY, CLIENT))


if __name__ == "__main__":
    try:
        main()
    except CheckFailure as e:
        print("FAILED:", e, file=sys.stderr)
        sys.exit(1)
