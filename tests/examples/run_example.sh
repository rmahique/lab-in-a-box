#!/usr/bin/env bash
# tests/examples/run_example.sh — deploy one of this project's documented
# README examples against REAL hardware, then run its post-deploy check.
#
# These are NOT part of tests/run_tests.sh's automatic sweep (which only
# discovers tests/checks/*.sh) — they need a real KVM hypervisor and a
# working automation VM (lab_creation.cfg/.defaults, a source QCOW2/ISO on
# the hypervisor, DNS, the works), so they're deliberately run by hand, on
# the automation VM, before pushing a change that could affect one of these
# documented flows. Present, but never auto-run.
#
# Usage:
#   tests/examples/run_example.sh <name> [--keep] [--no-destroy]
#
#   <name>        one of: standalone, rancher-cluster, multi-host,
#                 uyuni-lab, legacy — matches tests/examples/<name>.json
#                 and tests/examples/checks/<name>_check.py
#   --keep        passed through to setup_lab.py (skip VMs already up and
#                 reachable, instead of destroying and recreating)
#   --no-destroy  don't destroy_lab.py the example after a successful check
#                 (default: destroy on success, always leave it up on
#                 failure so you can inspect what actually happened)
#
# Exit code reflects the CHECK's result, not just the deploy's — a lab that
# "deployed" but whose cluster never went Ready is a failure here.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

name="${1:-}"
shift || true
keep=""
no_destroy=0
for arg in "$@"; do
    case "$arg" in
        --keep) keep="--keep" ;;
        --no-destroy) no_destroy=1 ;;
        *) echo "unknown option: $arg" >&2; exit 1 ;;
    esac
done

if [[ -z "$name" ]]; then
    echo "Usage: $0 <name> [--keep] [--no-destroy]" >&2
    echo "  <name> is one of:" >&2
    for f in tests/examples/*.json; do
        echo "    $(basename "$f" .json)" >&2
    done
    exit 1
fi

lab_json="tests/examples/${name}.json"
check_py="tests/examples/checks/${name}_check.py"

if [[ ! -f "$lab_json" ]]; then
    echo "No such example: $lab_json not found" >&2
    exit 1
fi
if [[ ! -f "$check_py" ]]; then
    echo "No check script for '$name': $check_py not found" >&2
    exit 1
fi

echo "== Deploying $lab_json =="
if ! setup_lab.py $keep "$lab_json"; then
    echo "FAILED: setup_lab.py itself did not exit 0 — see output above" >&2
    exit 1
fi

echo "== Checking $lab_json actually works =="
if python3.11 "$check_py"; then
    echo "PASSED: $name"
    if [[ "$no_destroy" -eq 0 ]]; then
        echo "== Tearing down (pass --no-destroy to keep it up) =="
        destroy_lab.py "$lab_json" || true
    fi
    exit 0
else
    echo "FAILED: $name — left up for inspection (run destroy_lab.py '$lab_json' yourself when done)" >&2
    exit 1
fi
