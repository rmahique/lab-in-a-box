#!/bin/bash
# Tests-paths: scripts/install_qdrant
# install_qdrant: the Helm install on a kcluster, the podman container on a node, the mode dispatch and validation.
# SSH and podman are faked.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/156_qdrant_test.py
