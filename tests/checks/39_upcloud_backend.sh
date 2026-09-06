#!/bin/bash
# Pure-logic unit tests for libs/backends.py's UpCloudBackend. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/39_upcloud_backend_test.py
