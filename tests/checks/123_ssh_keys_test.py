#!/usr/bin/env python3
# Unit tests for libs/ssh_keys.py (ssh_authorized_keys), the item-list support in libs/schema_validate.py and
# libs/schema_versions.py, and setup_lab.py's phase_ssh_keys() — mocked SSH and URL fetches, no real host.
# Run from 123_ssh_keys.sh, in its own container — see tests/run_tests.sh.
import io
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
# libs/ ahead of scripts/: both hold a schema_versions module, and the library is the one under test.
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "libs"))

import lab_schema_base  # noqa: E402
import schema_validate  # noqa: E402
import schema_versions  # noqa: E402
import ssh_keys  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAaaaa alice@laptop"
KEY_B = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbbbb bob@desk"


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── is_key_line ───────────────────────────────────────────────────────────────
check("is_key_line: an ed25519 line", ssh_keys.is_key_line(KEY_A))
check("is_key_line: an ecdsa line", ssh_keys.is_key_line(KEY_B))
check("is_key_line: a line with an options prefix", ssh_keys.is_key_line('from="10.0.0.0/8" ' + KEY_A))
check("is_key_line: a URL is not a key", not ssh_keys.is_key_line("https://github.com/alice.keys"))
check("is_key_line: two lines are not one key", not ssh_keys.is_key_line(KEY_A + "\n" + KEY_B))

# ── entries(): common then node, merged, duplicates dropped ──────────────────
definition = {
    "common": {"ssh_authorized_keys": [KEY_A, {"key": "https://github.com/bob.keys", "user": "bob"}]},
    "nodes": {"n1.lab": {"myip": "10.0.0.5", "ssh_authorized_keys": [KEY_A, {"key": "/k/team.pub"}]},
              "n2.lab": {}},
}
got = ssh_keys.entries(definition, "n1.lab")
check("entries: common's entries then the node's, duplicates dropped",
      got == [{"key": KEY_A, "user": ""}, {"key": "https://github.com/bob.keys", "user": "bob"},
              {"key": "/k/team.pub", "user": ""}])
check("entries: a node without its own gets common's", len(ssh_keys.entries(definition, "n2.lab")) == 2)
check("entries: no field anywhere gives nothing", ssh_keys.entries({"nodes": {"x": {}}}, "x") == [])

# ── source_problem() ─────────────────────────────────────────────────────────
_tmp = tempfile.mkdtemp()
_keyfile = os.path.join(_tmp, "team.pub")
Path(_keyfile).write_text("# team keys\n\n{}\n{}\n".format(KEY_A, KEY_B))
check("source_problem: a key line is fine", ssh_keys.source_problem(KEY_A) == "")
check("source_problem: an https URL is fine without fetching it",
      ssh_keys.source_problem("https://github.com/alice.keys") == "")
check("source_problem: an existing file is fine", ssh_keys.source_problem(_keyfile) == "")
check("source_problem: http is refused", "https" in ssh_keys.source_problem("http://example.com/keys"))
check("source_problem: a relative path is refused", ssh_keys.source_problem("keys/team.pub") != "")
check("source_problem: a missing file is reported", "does not exist" in ssh_keys.source_problem("/nope/x.pub"))
check("source_problem: a missing file is not looked for without check_files",
      ssh_keys.source_problem("/nope/x.pub", check_files=False) == "")

# ── key_lines(): a key, a file, a URL ─────────────────────────────────────────
check("key_lines: a key line is itself", ssh_keys.key_lines(KEY_A) == [KEY_A])
check("key_lines: a file's key lines, comments and blanks left out", ssh_keys.key_lines(_keyfile) == [KEY_A, KEY_B])
_empty = os.path.join(_tmp, "empty.pub")
Path(_empty).write_text("# nothing here\n")
for _label, _src in (("a file with no key", _empty), ("a missing file", "/nope/x.pub"),
                     ("a relative path", "x.pub")):
    try:
        ssh_keys.key_lines(_src)
        _raised = False
    except ValueError:
        _raised = True
    check("key_lines: {} raises ValueError".format(_label), _raised)


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_fetched = []


def _fake_urlopen(url, timeout=None):
    _fetched.append((url, timeout))
    if "down" in url:
        raise OSError("connection refused")
    return _FakeResp((KEY_B + "\n").encode())


ssh_keys.urllib.request.urlopen = _fake_urlopen
check("key_lines: an https URL's key lines", ssh_keys.key_lines("https://github.com/bob.keys") == [KEY_B])
check("key_lines: the fetch has a timeout", _fetched and _fetched[0][1] == ssh_keys.URL_TIMEOUT)
try:
    ssh_keys.key_lines("https://down.example/keys")
    _raised = False
except ValueError:
    _raised = True
check("key_lines: an unreachable URL raises ValueError", _raised)

# ── install(): the remote command, its stdin, and what each exit means ───────
_calls = []
_rc = {"value": 0}


def _fake_ssh_run(host, cmd, check=True, input_text=None, capture=False, user="root"):
    _calls.append((host, cmd, input_text, check))
    return FakeResult(returncode=_rc["value"], stderr="boom" if _rc["value"] not in (0, 3) else "")


ssh_keys.ssh_run = _fake_ssh_run
check("install: success is no problem", ssh_keys.install("10.0.0.5", "alice", [KEY_A, KEY_B]) == "")
_host, _cmd, _stdin, _check = _calls[-1]
check("install: runs on the given target, never raising", _host == "10.0.0.5" and _check is False)
check("install: the user is passed as an argument, quoted", _cmd.endswith(" _ alice"))
check("install: the keys go over stdin, one per line", _stdin == KEY_A + "\n" + KEY_B + "\n")
check("install: keys never appear in the command itself", KEY_A not in _cmd)
check("install: ~/.ssh and authorized_keys get the user's ownership and modes",
      "install -d -m 700" in _cmd and "chmod 600" in _cmd and "chown" in _cmd)
check("install: a key already present is not added twice", "grep -qxF" in _cmd)
_rc["value"] = 3
check("install: exit 3 is a missing user", "does not exist" in ssh_keys.install("10.0.0.5", "ghost", [KEY_A]))
_rc["value"] = 1
check("install: any other failure carries its output", ssh_keys.install("10.0.0.5", "alice", [KEY_A]) == "boom")
_rc["value"] = 0

# ── install_node_keys(): warnings, never a failure ───────────────────────────
_calls.clear()


def _per_user_ssh_run(host, cmd, check=True, input_text=None, capture=False, user="root"):
    _calls.append((host, cmd, input_text, check))
    return FakeResult(returncode=3 if cmd.endswith(" _ ghost") else 0)


ssh_keys.ssh_run = _per_user_ssh_run
definition = {
    "common": {"ssh_authorized_keys": [KEY_A]},
    "nodes": {"n1.lab": {"myip": "10.0.0.5", "ssh_authorized_keys": [
        {"key": KEY_B, "user": "ghost"}, "https://down.example/keys", {"key": _keyfile, "user": "alice"}]}},
}
_out = io.StringIO()
with redirect_stdout(_out), redirect_stderr(_out):
    warnings = ssh_keys.install_node_keys(definition, "n1.lab")
check("install_node_keys: reached at the node's myip", all(h == "10.0.0.5" for h, _, _, _ in _calls))
check("install_node_keys: a plain string goes to root", any(c.endswith(" _ root") for _, c, _, _ in _calls))
check("install_node_keys: a file's keys go to the named user",
      any(c.endswith(" _ alice") and s == KEY_A + "\n" + KEY_B + "\n" for _, c, s, _ in _calls))
check("install_node_keys: one warning per entry that could not be added", len(warnings) == 2)
check("install_node_keys: a missing user is a warning naming it",
      any("ghost" in w and "does not exist" in w for w in warnings))
check("install_node_keys: an unreadable URL is a warning naming it",
      any("https://down.example/keys" in w for w in warnings))
check("install_node_keys: warnings are printed too", "does not exist" in _out.getvalue())
_calls.clear()
check("install_node_keys: nothing to add runs nothing",
      ssh_keys.install_node_keys({"nodes": {"x": {}}}, "x") == [] and _calls == [])

# ── schema: the field, and item validation in schema_validate ────────────────
_fields = {s: [f for f in lab_schema_base.base_lab_schema()["sections"][s]["fields"]
               if isinstance(f, dict) and f.get("name") == "ssh_authorized_keys"]
           for s in ("common", "nodes")}
check("schema: ssh_authorized_keys is in common and in nodes", all(len(v) == 1 for v in _fields.values()))
_field = _fields["common"][0]
check("schema: a list of strings and mappings is fine",
      schema_validate.value_finding(_field, [KEY_A, {"key": KEY_B, "user": "bob"}]) is None)
_f = schema_validate.value_finding(_field, [KEY_A, {"user": "bob"}], "common.ssh_authorized_keys")
check("schema: a mapping without its key is an error at its index",
      _f is not None and _f.where == "common.ssh_authorized_keys[1]" and "key" in _f.message)
_f = schema_validate.value_finding(_field, [{"key": KEY_A, "user": "bad user"}], "common.ssh_authorized_keys")
check("schema: a user name that does not match is an error",
      _f is not None and _f.where == "common.ssh_authorized_keys[0].user")
check("schema: a number is not an item", schema_validate.value_finding(_field, [42]) is not None)
check("schema: a string where the list goes is an error", schema_validate.value_finding(_field, KEY_A) is not None)

# ── schema_versions: item fields are snapshot fields of their own ────────────
_snap = schema_versions.schema_snapshot("lab", lab_schema_base.base_lab_schema())["fields"]
check("snapshot: item fields are named <field>[].<item field>",
      "common.ssh_authorized_keys[].key" in _snap and "nodes.ssh_authorized_keys[].user" in _snap)
_old = {"fields": {"a": {"type": "string"}}}
_new = {"fields": {"a": {"type": "string"}, "l": {"type": "array"}, "l[].k": {"type": "string", "required": True}}}
check("breaking_changes: a required item field of a new list breaks nothing",
      schema_versions.breaking_changes(_old, _new) == [])
check("other_changes: it is listed as added", "l[].k added" in schema_versions.other_changes(_old, _new))
_old2 = {"fields": {"l": {"type": "array"}}}
check("breaking_changes: a required item field added to an existing list is breaking",
      schema_versions.breaking_changes(_old2, _new) == ["l[].k added as required"])

# ── setup_lab.phase_ssh_keys(): created and reused nodes only ────────────────
import setup_lab  # noqa: E402

_installed = []
setup_lab.ssh_keys.install_node_keys = lambda d, vm: _installed.append(vm) or (["warn " + vm] if vm == "b" else [])
setup_lab.harvester.cluster_of = lambda d, vm: "harv" if vm == "h" else ""
setup_lab._report = setup_lab._RunReport()
for _name, _status in (("a", "created"), ("b", "reused"), ("c", "existing"), ("d", "FAILED"), ("h", "created")):
    setup_lab._report.add_node(_name, _status)
setup_lab.phase_ssh_keys({"nodes": {}})
check("phase_ssh_keys: created and reused nodes only, never a Harvester node", _installed == ["a", "b"])
check("phase_ssh_keys: warnings go to the run summary", setup_lab._report.warnings == ["warn b"])

if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All ssh_keys checks passed")
