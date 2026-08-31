#!/bin/bash
# Unit tests for libs/lab_creation.py's run_libvirt_tool() — the SSH
# fallback used when no local virsh/virt-install binary exists (e.g. the
# MCP endpoint's thin container). No real virsh/ssh calls — subprocess.run
# and _has_local_binary are mocked. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

python3 tests/checks/28_run_libvirt_tool_ssh_fallback_test.py
