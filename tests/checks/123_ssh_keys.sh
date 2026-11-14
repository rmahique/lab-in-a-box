#!/bin/bash
# Tests-paths: libs/ssh_keys.py libs/schema_validate.py libs/schema_versions.py libs/lab_schema_base.py scripts/setup_lab.py
# Unit tests for libs/ssh_keys.py (ssh_authorized_keys) and the item-list schema support it uses. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/123_ssh_keys_test.py
