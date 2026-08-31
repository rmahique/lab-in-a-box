#!/bin/bash
# Unit tests for libs/services.py's PXEService "ipxe-uefi" mode — the
# two-stage UEFI netboot support added for Harvester's PXE install path.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

python3 tests/checks/29_pxe_ipxe_uefi_test.py
