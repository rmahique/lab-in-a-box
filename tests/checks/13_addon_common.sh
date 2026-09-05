#!/bin/bash
# Pure-logic unit tests for libs/addon_common.py — the
# shared --version/--validate/--help/--schema/--capabilities CLI scaffolding
# every install_<addon>.py script builds on. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/13_addon_common_test.py
