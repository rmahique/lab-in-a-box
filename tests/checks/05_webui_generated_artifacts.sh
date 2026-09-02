#!/bin/bash
# The lab-builder-ctl control script and lab-builder.service systemd unit
# that install_automation_node_scripts.sh generates for _webui_mode=service
# — extracted verbatim from the real installer (not reimplemented), so this
# can't silently drift from what actually ships. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

_pass=0
_fail=0
_err=$(mktemp)
_tmpdir=$(mktemp -d)
trap 'rm -f "$_err"; rm -rf "$_tmpdir"' EXIT

_installer="install_automation_node_scripts.sh"

awk '/cat > \/usr\/local\/bin\/lab-builder-ctl << CTLEOF/{f=1; next} /^CTLEOF$/{f=0} f' "$_installer" > "$_tmpdir/ctl.tmpl"
if [[ -s "$_tmpdir/ctl.tmpl" ]]; then
    sed 's/\${_lb_root}/\/tmp\/fake-root/g; s/\${_lb_port}/9999/g; s/\${_lb_scheme}/https/g; s/\${_lb_tls_cert}/\/tmp\/cert.pem/g; s/\${_lb_tls_key}/\/tmp\/key.pem/g' "$_tmpdir/ctl.tmpl" > "$_tmpdir/lab-builder-ctl"
    if bash -n "$_tmpdir/lab-builder-ctl" 2>"$_err"; then
        _pass=$((_pass + 1))
    else
        _fail=$((_fail + 1))
        echo "FAIL: generated lab-builder-ctl"
        sed 's/^/    /' "$_err"
    fi
else
    _fail=$((_fail + 1))
    echo "FAIL: could not extract lab-builder-ctl heredoc from $_installer (marker text changed?)"
fi

awk '/cat > \/etc\/systemd\/system\/lab-builder.service << UNITEOF/{f=1; next} /^UNITEOF$/{f=0} f' "$_installer" > "$_tmpdir/unit.tmpl"
if [[ -s "$_tmpdir/unit.tmpl" ]]; then
    sed 's/\${_lb_root}/\/tmp\/fake-root/g; s/\${_lb_port}/9999/g; s/\${_lb_tls_cert}/\/tmp\/cert.pem/g; s/\${_lb_tls_key}/\/tmp\/key.pem/g' "$_tmpdir/unit.tmpl" > "$_tmpdir/lab-builder.service"
    if systemd-analyze verify "$_tmpdir/lab-builder.service" 2>"$_err"; then
        _pass=$((_pass + 1))
    else
        _fail=$((_fail + 1))
        echo "FAIL: generated lab-builder.service"
        sed 's/^/    /' "$_err"
    fi
else
    _fail=$((_fail + 1))
    echo "FAIL: could not extract lab-builder.service heredoc from $_installer (marker text changed?)"
fi

echo "Passed: $_pass  Failed: $_fail"
[[ $_fail -eq 0 ]]
