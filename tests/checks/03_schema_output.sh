#!/bin/bash
# lab_schema --base json/yaml both produce valid, parseable output.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_pass=0
_fail=0
_err=$(mktemp)
trap 'rm -f "$_err"' EXIT

if python3 scripts/lab_schema --base json > /tmp/schema.json 2>"$_err"; then
    if python3 -m json.tool < /tmp/schema.json > /dev/null 2>"$_err"; then
        _pass=$((_pass + 1))
    else
        _fail=$((_fail + 1))
        echo "FAIL: lab_schema --base json produced invalid JSON"
        sed 's/^/    /' "$_err"
    fi
else
    _fail=$((_fail + 1))
    echo "FAIL: lab_schema --base json failed to run"
    sed 's/^/    /' "$_err"
fi

if python3 -c 'import yaml' >/dev/null 2>&1; then
    if python3 scripts/lab_schema --base yaml > /tmp/schema.yaml 2>"$_err" \
        && python3 -c 'import yaml; yaml.safe_load(open("/tmp/schema.yaml"))' 2>"$_err"; then
        _pass=$((_pass + 1))
    else
        _fail=$((_fail + 1))
        echo "FAIL: lab_schema --base yaml produced invalid YAML"
        sed 's/^/    /' "$_err"
    fi
fi

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
