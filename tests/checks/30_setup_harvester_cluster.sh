#!/bin/bash
# Unit tests for scripts/setup_harvester_cluster.py — the new PXE-based
# Harvester HCI cluster bootstrap script. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

python3 tests/checks/30_setup_harvester_cluster_test.py
