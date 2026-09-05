#!/bin/bash
# Mocked unit tests for mcp/mcp_server.py's mutating tools
# (deploy_lab/rebuild_lab/destroy_lab_tool/destroy_vm_tool) — setup_lab.py/
# destroy_lab.py/destroy_vm.py's own functions are monkeypatched (never
# real SSH/libvirt). The most important assertion here: a missing or wrong
# confirm never reaches the underlying function at all. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/25_mcp_deploy_destroy_test.py
