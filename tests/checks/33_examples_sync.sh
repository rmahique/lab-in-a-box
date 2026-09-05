#!/bin/bash
# Confirms tests/examples/*.json stays in sync with README.md's own copy of
# each example. Pure text/JSON comparison, no hardware or network involved
# — see tests/examples/README.md for the real deploy+check tests these
# fixtures back. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/33_examples_sync_test.py
