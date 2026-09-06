#!/usr/bin/env python3
# Unit tests for libs/backends.py's HetznerBackend — every HTTP call
# (urllib.request.urlopen) is mocked (no real Hetzner Cloud account/project
# available anywhere in this environment). Asserts request shapes, MAC/image
# handling, config_method enforcement, and server_type sizing — not real API
# behavior; see HetznerBackend's own docstring for exactly what remains
# unverified. Run from 34_hetzner_backend.sh, in its own container — see
# tests/run_tests.sh.
import json
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class _FakeResponse(object):
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8") if body is not None else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── resolve(): HETZNER_TOKEN is mandatory, dies clearly without it ─────────
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.HetznerBackend.resolve({}, "vm1", {}, False)
    except SystemExit:
        pass
check("resolve() dies without HETZNER_TOKEN", any("HETZNER_TOKEN" in m for m in died))

resolved = backends.HetznerBackend.resolve({}, "vm1", {"HETZNER_TOKEN": "tok123"}, False)
check("resolve() picks up the token", resolved.token == "tok123")
check("resolve() defaults location to None", resolved.location is None)

resolved = backends.HetznerBackend.resolve(
    {}, "vm1", {"HETZNER_TOKEN": "tok123", "HETZNER_LOCATION": "nbg1"}, False)
check("resolve() picks up an explicit location", resolved.location == "nbg1")


# ── config_method enforcement (same shape as HarvesterBackend's) ──────────
backend = backends.HetznerBackend("tok123")

for method_name, args in (
    ("copy_vm_image", ("ubuntu-24.04", "vm1", 40)),
    ("push_provisioning_files", ("vm1",)),
):
    died = []
    with mock.patch.object(backends, "die",
                            side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
        try:
            getattr(backend, method_name)(*args, config_method="")
        except SystemExit:
            pass
        except Exception:
            pass
    check("{}() dies on config_method != cloud-init".format(method_name),
          any("cloud-init" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("", "vm1", 40, config_method="cloud-init")
    except SystemExit:
        pass
check("copy_vm_image() dies on an empty ISO_IMAGE", any("ISO_IMAGE" in m for m in died))

ok = [False]
with mock.patch.object(backends, "die", side_effect=AssertionError("should not die")):
    backend.copy_vm_image("ubuntu-24.04", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real image name with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): stashes user-data for create_vm() to read ──
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.HetznerBackend("tok123", lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    check("push_provisioning_files() stashes the real file content",
          b2._user_data_by_vm.get("vm1") == "#cloud-config\nhostname: vm1\n")


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on Hetzner ─
check("list_used_macs() returns empty (Hetzner has no MAC concept)",
      backend.list_used_macs() == ([], {}))

fake_definition = {"nodes": {"vm1": {}}}
mymac, network = backend.check_or_generate_mac("vm1", "", fake_definition)
check("check_or_generate_mac() still generates SOME mac value (never sends it to Hetzner)",
      bool(mymac))


# ── _pick_server_type(): smallest SKU that satisfies both cores and memory ─
check("_pick_server_type() picks the smallest sufficient SKU (2 vCPU / 2048 MiB)",
      backend._pick_server_type(2, 2048, 20, "vm1") == "cx22")
check("_pick_server_type() steps up when memory needs more than cores would suggest",
      backend._pick_server_type(2, 16384, 20, "vm1") == "cx42")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend._pick_server_type(999, 999999, 20, "vm1")
    except SystemExit:
        pass
check("_pick_server_type() dies clearly when nothing in the table is big enough",
      any("SERVER_TYPES" in m for m in died))


# ── vm_exists() / _find_server(): GET /servers?name=... ────────────────────
with mock.patch.object(backends.urllib.request, "urlopen",
                        return_value=_FakeResponse({"servers": [{"id": 42, "status": "running"}]})) as m_open:
    check("vm_exists() returns True when the API lists a matching server", backend.vm_exists("vm1") is True)
    called_url = m_open.call_args[0][0].full_url
    check("vm_exists() queries /servers?name=<vm>", "/servers?name=vm1" in called_url)

with mock.patch.object(backends.urllib.request, "urlopen", return_value=_FakeResponse({"servers": []})):
    check("vm_exists() returns False when the API lists no matching server", backend.vm_exists("vm1") is False)


# ── delete_vm(): idempotent when the server is already gone ────────────────
with mock.patch.object(backends.urllib.request, "urlopen", return_value=_FakeResponse({"servers": []})) as m_open:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls GET (no DELETE) when the server doesn't exist",
          m_open.call_count == 1)


# ── create_vm(): real request body shape, and the disk-size warning ───────
b3 = backends.HetznerBackend("tok123")
b3._user_data_by_vm["vm1"] = "#cloud-config\n"
captured = {}


def _fake_urlopen(req, timeout=30):
    captured["body"] = json.loads(req.data.decode("utf-8"))
    captured["headers"] = dict(req.headers)
    return _FakeResponse({"server": {"id": 1, "server_type": {"disk": 40}}})


with mock.patch.object(backends.urllib.request, "urlopen", side_effect=_fake_urlopen):
    b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="ubuntu-24.04")

check("create_vm() sends the real image name", captured["body"].get("image") == "ubuntu-24.04")
check("create_vm() sends the stashed user_data", captured["body"].get("user_data") == "#cloud-config\n")
check("create_vm() picks a real server_type", captured["body"].get("server_type") == "cx22")
check("create_vm() sends a Bearer token", captured["headers"].get("Authorization") == "Bearer tok123")
check("create_vm() omits location when none was configured", "location" not in captured["body"])

b4 = backends.HetznerBackend("tok123", location="nbg1")
b4._user_data_by_vm["vm1"] = ""
with mock.patch.object(backends.urllib.request, "urlopen", side_effect=_fake_urlopen):
    b4.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="ubuntu-24.04")
check("create_vm() sends location when one was configured", captured["body"].get("location") == "nbg1")


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all hetzner_backend checks passed")
