#!/bin/bash
# Tests-paths: libs/static_webgame_common.py scripts/install_skynet_simulator scripts/install_supertux_classic scripts/install_open_saber
# libs/static_webgame_common.py and the games built on it: the Kubernetes manifest, the podman fetch-and-serve on a
# node, and install_skynet_simulator's mode dispatch. SSH and podman are faked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/157_static_webgame_test.py
