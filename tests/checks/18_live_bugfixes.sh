#!/bin/bash
# Regression tests for bugs found via live-host testing of the
# python_migration cutover on disposable VMs (nuc6.mydemo.lab, 2026-08-28)
# — mocked-SSH/subprocess here so they're covered without needing real
# infrastructure going forward. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/18_live_bugfixes_test.py
