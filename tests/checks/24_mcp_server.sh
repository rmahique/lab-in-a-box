#!/bin/bash
# Mocked unit tests for mcp/mcp_server.py — tool
# registration gating, the confirm/audit logic, and read-only tools calling
# through to webui/lib/api.py's dispatch(). The real `mcp`/`uvicorn`
# packages aren't installed in this test image (network-dependent pip
# install, deliberately not added to keep the test build hermetic) — a
# minimal fake FastMCP is injected into sys.modules instead; see
# 24_mcp_server_test.py's own comment. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

python3 tests/checks/24_mcp_server_test.py
