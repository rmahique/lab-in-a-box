#!/bin/bash
# Pure-logic unit tests for libs/backends.py's AlibabaBackend. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/37_alibaba_backend_test.py
