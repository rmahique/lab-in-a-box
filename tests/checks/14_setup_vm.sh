#!/bin/bash
# Mocked unit tests for scripts/setup_vm.py — single-VM
# provisioning orchestration. Every lab_creation call is monkeypatched (no
# live KVM host available); this verifies call ordering and dispatch, not
# real provisioning. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

python3 tests/checks/14_setup_vm_test.py
