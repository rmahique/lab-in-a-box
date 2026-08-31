#!/bin/bash
# Unit tests for libs/backends.py's resolve()-classmethod dispatch
# (get_backend() -> BACKENDS[name].resolve(...)) — no real virsh/kubectl
# calls, resolve_kvm_host()/locate_kvm_host() are mocked. Independent
# container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

python3 tests/checks/26_backends_resolve_test.py
