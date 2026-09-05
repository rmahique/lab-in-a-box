#!/bin/bash
# Functional test of `_webui_mode=service` — meant to run INSIDE
# tests/Containerfile's image (a disposable, --rm container: whatever this
# writes to /etc, /usr/local, /srv/www never touches a real host). This
# container has no running systemd (no /run/systemd/system), so running the
# real installer here exercises its non-systemd fallback path — the one
# code path this project's own dev machine (which does have systemd) can
# never reach on its own. Independent container — see tests/run_tests.sh.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit

echo "== confirming no systemd in this container =="
if [[ -d /run/systemd/system ]]; then
    echo "FAIL: this container has systemd — the non-systemd fallback path would not be exercised"
    exit 1
fi
echo "ok: /run/systemd/system absent, as expected"

echo
echo "== running the real installer with _webui_mode=service =="
# _webui_tls=0: this check exercises start/stop/restart/status lifecycle over
# plain HTTP, not TLS itself — TLS defaults on otherwise, which would break
# every plain "curl http://..." call below against an HTTPS-only listener.
_webui_mode=service _webui_port=9677 _webui_tls=0 bash install_automation_node_scripts.sh

echo
echo "== checking install artifacts =="
test -x /usr/local/bin/lab-builder-ctl || { echo "FAIL: lab-builder-ctl not installed"; exit 1; }
test ! -f /etc/systemd/system/lab-builder.service || { echo "FAIL: systemd unit was written despite no systemd in this container"; exit 1; }
echo "ok: lab-builder-ctl installed, no systemd unit written (correct — no systemd here)"

echo
echo "== installer's non-systemd fallback already auto-started it =="
# install_automation_node_scripts.sh's non-systemd branch calls
# 'lab-builder-ctl start' itself right after generating it, so the service
# is already running at this point — confirm that, then exercise the full
# stop/start/restart/stop cycle explicitly below.
lab-builder-ctl status || { echo "FAIL: expected the installer to have already started it"; exit 1; }

echo
echo "== stopping, then starting explicitly =="
lab-builder-ctl stop
sleep 1
if lab-builder-ctl status; then
    echo "FAIL: expected 'not running' after stop"
    exit 1
fi
lab-builder-ctl start
sleep 1
lab-builder-ctl status

echo
echo "== hitting the real HTTP API =="
_code=$(curl -s -o /tmp/resp.json -w '%{http_code}' "http://localhost:9677/api?action=components")
if [[ "$_code" != "200" ]]; then
    echo "FAIL: expected HTTP 200 from /api?action=components, got $_code"
    cat /tmp/resp.json
    exit 1
fi
_count=$(python3 -c "import json; print(json.load(open('/tmp/resp.json'))['count'])")
echo "ok: HTTP 200, $_count components discovered"
if [[ "$_count" -lt 1 ]]; then
    echo "FAIL: expected at least one discovered addon component"
    exit 1
fi

_code_base=$(curl -s -o /tmp/base.json -w '%{http_code}' "http://localhost:9677/api?action=base")
if [[ "$_code_base" != "200" ]]; then
    echo "FAIL: expected HTTP 200 from /api?action=base, got $_code_base"
    exit 1
fi
python3 -c "
import json
d = json.load(open('/tmp/base.json'))
sections = set(d['sections'].keys())
missing = {'common', 'nodes', 'kclusters', 'pxe'} - sections
assert not missing, 'missing schema sections: {}'.format(missing)
print('ok: base schema has all expected sections:', sorted(sections))
"

echo
echo "== restarting =="
lab-builder-ctl restart
sleep 1
lab-builder-ctl status || { echo "FAIL: not running after restart"; exit 1; }
curl -sf -o /dev/null "http://localhost:9677/" || { echo "FAIL: no HTTP response after restart"; exit 1; }
echo "ok: still serving after restart"

echo
echo "== stopping =="
lab-builder-ctl stop
sleep 1
if lab-builder-ctl status; then
    echo "FAIL: expected 'not running' after stop"
    exit 1
fi
if curl -sf --max-time 2 -o /dev/null "http://localhost:9677/" 2>/dev/null; then
    echo "FAIL: server still responding after stop"
    exit 1
fi
echo "ok: correctly stopped, port no longer responding"

echo
echo "ALL WEBUI SERVICE-MODE TESTS PASSED"
