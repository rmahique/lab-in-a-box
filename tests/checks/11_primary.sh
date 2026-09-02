#!/bin/bash
# Pure-logic unit tests for libs/primary.py — lab
# definition loading and simple-shell-variable config parsing. No mocking
# needed (pure file/string parsing, no subprocess/SSH). Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/11_primary_test.py
