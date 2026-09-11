#!/bin/bash
# Pure-logic unit tests for libs/backends.py's HetznerBackend. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/34_hetzner_backend_test.py
