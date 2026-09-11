#!/bin/bash
# Pure-logic unit tests for libs/backends.py's AWSBackend. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/35_aws_backend_test.py
