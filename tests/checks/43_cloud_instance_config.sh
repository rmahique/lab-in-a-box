#!/bin/bash
# Pure-logic unit tests for libs/backends.py's _parse_sku_table() (the shared
# *_INSTANCE_TYPES/*_SERVER_TYPES/*_PLANS config-string parser) and a
# LibvirtBackend.create_vm() regression check. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/43_cloud_instance_config_test.py
