#!/bin/bash
# node --check syntax on the webui frontend, plus a DOM-free functional smoke
# test of app.js's pure default-resolution/validation logic
# (07_webui_js_smoke.js, loaded via node's vm module — no jsdom/npm
# dependency). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

_pass=0
_fail=0
_err=$(mktemp)
trap 'rm -f "$_err"' EXIT

_js="webui/htdocs/app.js"

if node --check "$_js" 2>"$_err"; then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: node --check $_js"
    sed 's/^/    /' "$_err"
fi

if node tests/checks/07_webui_js_smoke.js 2>"$_err"; then
    _pass=$((_pass + 1))
else
    _fail=$((_fail + 1))
    echo "FAIL: app.js functional smoke test"
    sed 's/^/    /' "$_err"
fi

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
