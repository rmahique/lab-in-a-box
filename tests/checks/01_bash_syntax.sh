#!/bin/bash
# bash -n over every .sh file in the repo. Each script under tests/checks/
# runs in its own independent, disposable container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

_pass=0
_fail=0
_err=$(mktemp)
trap 'rm -f "$_err"' EXIT

while IFS= read -r -d '' f; do
    if bash -n "$f" 2>"$_err"; then
        _pass=$((_pass + 1))
    else
        _fail=$((_fail + 1))
        echo "FAIL: $f"
        sed 's/^/    /' "$_err"
    fi
done < <(find . -name '*.sh' -not -path './.git/*' -not -path '*/__pycache__/*' -print0)

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
