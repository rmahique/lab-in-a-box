#!/bin/bash
# lab-in-a-box test runner — every check runs in its OWN independent,
# disposable podman container (tests/Containerfile): one `podman run` per
# script under tests/checks/, never bundled together. A crash, hang, or
# leftover state in one check can't touch any other. Wired to run
# automatically before each commit — see .githooks/pre-commit (enable once
# per clone: git config core.hooksPath .githooks).
#
# Run manually:
#   tests/run_tests.sh
#
# Add a new check: drop a new executable script into tests/checks/ — it's
# picked up automatically, no edits needed here.
# -uo pipefail (no -e): a failing check must not abort the loop — it's
# recorded in _failed and the run continues, so one broken check doesn't
# hide the results of every check after it.
set -uo pipefail
# Always run from the repo root regardless of the caller's cwd, since every
# podman invocation below bind-mounts "$(pwd)" as the repo.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

_image="lab-in-a-box-tests"

# podman is a hard requirement, not an optional speedup — there is no
# no-container fallback path (see feedback: tests only run in containers,
# never against the host).
if ! command -v podman >/dev/null 2>&1; then
    echo "podman is required to run tests (every check runs in its own container) — see tests/Containerfile" >&2
    exit 1
fi

# One shared image for every check (built once, reused per `podman run`
# below). podman's own layer cache makes this a no-op rebuild when
# tests/Containerfile and its deps haven't changed.
echo "== building test image (cached — fast if Containerfile is unchanged) =="
if ! podman build -q -t "$_image" -f tests/Containerfile . ; then
    echo "podman build failed" >&2
    exit 1
fi

_status=0
_failed=()

# Each tests/checks/*.sh gets its OWN fresh container (--rm), so state from
# one check (files it wrote, env it changed) never leaks into the next.
# The repo is bind-mounted read-only (:ro) — a check that needs to write
# does so inside the container's own filesystem, never back onto the host
# checkout. Discovery is filesystem globbing, not a hardcoded list: drop a
# new script into tests/checks/ and it runs on the next invocation.
for script in tests/checks/*.sh; do
    name=$(basename "$script")
    echo
    echo "############################################"
    echo "# $name (independent container)"
    echo "############################################"
    if ! podman run --rm -v "$(pwd)":/repo:ro -w /repo "$_image" bash "$script"; then
        _status=1
        _failed+=("$name")
    fi
done

# Summarise once at the end rather than failing fast, so a single run always
# reports the full picture (which checks failed, which passed) instead of
# stopping at the first failure.
echo
if [[ $_status -eq 0 ]]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED: ${_failed[*]}"
fi
exit $_status
