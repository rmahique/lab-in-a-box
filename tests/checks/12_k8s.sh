#!/bin/bash
# Mocked-SSH unit tests for libs/k8s.py — Kubernetes
# cluster setup and addon-execution helpers. No live cluster available in
# this project. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

python3 tests/checks/12_k8s_test.py
