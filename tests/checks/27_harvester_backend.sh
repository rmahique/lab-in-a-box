#!/bin/bash
# Unit tests for libs/backends.py's HarvesterBackend — mocked kubectl/
# virtctl subprocess calls (no real Harvester cluster available anywhere in
# this environment; see backends.py's own HarvesterBackend docstring).
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

python3 tests/checks/27_harvester_backend_test.py
