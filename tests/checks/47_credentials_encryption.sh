#!/bin/bash
# Integration tests for encrypted credential files: primary.py's decrypt/
# cache/opt-out handling and setup_credentials.py's two modes, against the
# real crypto_store (cryptography package). Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3.11 tests/checks/47_credentials_encryption_test.py
