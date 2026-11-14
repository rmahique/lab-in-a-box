#!/bin/bash
# The example labs offered as starting points (libs/lab_examples.py): what counts as one, the shipped and the
# operator's own directory, setup_lab.py --list-examples, the two API endpoints (a name is never a path), and the UI
# and the static build offering them. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/94_lab_examples_test.py
