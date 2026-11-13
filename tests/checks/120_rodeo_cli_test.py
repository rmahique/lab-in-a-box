#!/usr/bin/env python3
# Unit tests for scripts/install_rodeo_cli — mocked ssh_run, no real SSH.
# Run from 120_rodeo_cli.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402

rc = apps.load_addon_module("install_rodeo_cli", str(_REPO / "scripts"))  # no .py: loaded by path

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


calls = []
results = {}


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    for marker, result in results.items():
        if marker in cmd:
            return result
    return FakeResult(returncode=0)


rc.ssh_run = _fake_ssh_run

died = []
rc.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)


# ── setup_rodeo_cli(): the happy path ────────────────────────────────────────
calls.clear()
died.clear()
results.clear()
results["test -e /dev/kvm"] = FakeResult(returncode=0)
results["rodeo --version"] = FakeResult(returncode=0, stdout="rodeo-cli 0.7.0\n")
rc.setup_rodeo_cli("sol.mydemo.lab", "https://example.com/install.sh")
check("setup_rodeo_cli: fetches and runs the install script on the target host",
      any(h == "sol.mydemo.lab" and "curl" in c and "https://example.com/install.sh" in c and c.endswith(" bash")
          for h, c, _ in calls))
check("setup_rodeo_cli: checks for /dev/kvm", any("test -e /dev/kvm" in c for _, c, _ in calls))
check("setup_rodeo_cli: confirms the install with rodeo --version",
      any("rodeo --version" in c for _, c, _ in calls))
check("setup_rodeo_cli: no death on a clean install", died == [])

calls.clear()
rc.setup_rodeo_cli("sol.mydemo.lab", "https://example.com/install.sh", "sol.mydemo.lab 3.1.2.3")
check("setup_rodeo_cli: passes the builder's allowed names to the installer as RODEO_BUILDER_ALLOW",
      any("| RODEO_BUILDER_ALLOW='sol.mydemo.lab 3.1.2.3' bash" in c for _, c, _ in calls))

# ── builder_allow(): the node's FQDN, its resolved address, and the extra field ──
rc.socket.gethostbyname = lambda name: "3.1.2.3"
check("builder_allow: the FQDN and the address it resolves to, with no extra field",
      rc.builder_allow("sol.mydemo.lab", None) == "sol.mydemo.lab 3.1.2.3")
check("builder_allow: extra names, space- or comma-separated, duplicates dropped",
      rc.builder_allow("sol.mydemo.lab", "alias.lab, 3.1.2.3 other.lab") ==
      "sol.mydemo.lab 3.1.2.3 alias.lab other.lab")
check("builder_allow: a list of extra names works too",
      rc.builder_allow("sol.mydemo.lab", ["alias.lab"]) == "sol.mydemo.lab 3.1.2.3 alias.lab")


def _no_dns(name):
    raise OSError("not found")


rc.socket.gethostbyname = _no_dns
check("builder_allow: a name that does not resolve still allows the FQDN",
      rc.builder_allow("sol.mydemo.lab", "") == "sol.mydemo.lab")

# Missing /dev/kvm: a warning, not a failure — install still completes.
calls.clear()
died.clear()
results["test -e /dev/kvm"] = FakeResult(returncode=1)
rc.setup_rodeo_cli("sol.mydemo.lab", "https://example.com/install.sh")
check("setup_rodeo_cli: missing /dev/kvm does not die (install still proceeds)", died == [])

# The install script ran, but rodeo isn't actually usable afterwards: dies.
calls.clear()
died.clear()
results["test -e /dev/kvm"] = FakeResult(returncode=0)
results["rodeo --version"] = FakeResult(returncode=1, stderr="rodeo: command not found")
try:
    rc.setup_rodeo_cli("sol.mydemo.lab", "https://example.com/install.sh")
except SystemExit:
    pass
check("setup_rodeo_cli: dies if 'rodeo --version' fails after the install",
      len(died) == 1 and "sol.mydemo.lab" in died[0])


# ── check_rodeo_cli() ─────────────────────────────────────────────────────────
class FakeChecker:
    def __init__(self, vm_name):
        self.vm_name = vm_name
        self.problems = []
        self.notes = []

    def ok(self, what):
        self.notes.append(what)

    def fail(self, what, **kw):
        self.problems.append(what)


calls.clear()
results.clear()
results["rodeo --version"] = FakeResult(returncode=0, stdout="rodeo-cli 0.7.0\n")
c = FakeChecker("sol.mydemo.lab")
rc.check_rodeo_cli(c)
check("check_rodeo_cli: ok when rodeo --version succeeds", c.problems == [] and len(c.notes) == 1)

results["rodeo --version"] = FakeResult(returncode=1)
c = FakeChecker("sol.mydemo.lab")
rc.check_rodeo_cli(c)
check("check_rodeo_cli: fails when rodeo --version fails", len(c.problems) == 1)


# ── _validate ─────────────────────────────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def vurl_field(self, section, field):
        v = (self.definition.get(section, {}) or {}).get(field)
        if v and not str(v).startswith(("http://", "https://")):
            self.errors.append("[ERROR] bad url")

    def vbool(self, section, field):
        v = (self.definition.get(section, {}) or {}).get(field)
        if v and str(v).lower() not in ("true", "false"):
            self.errors.append("[ERROR] bad bool")


v = FakeValidator({"rodeo_cli": {}})
rc._validate(v)
check("_validate: no install URL set is fine (default applies)", v.errors == [])

v = FakeValidator({"rodeo_cli": {"rodeo_cli_install_url": "ftp://example.com/install.sh"}})
rc._validate(v)
check("_validate: a non-http(s) install URL is an error", v.errors != [])

v = FakeValidator({"rodeo_cli": {"rodeo_cli_install_url": "https://example.com/install.sh"}})
rc._validate(v)
check("_validate: a valid https install URL has no error", v.errors == [])


# ── rodeo_cli_install_lab_in_a_box: the opt-in lab-in-a-box tie-in ──────────
lic_calls = []
reach_calls = []
rc.require_ssh = lambda hostname: reach_calls.append(hostname)
rc.install_lab_in_a_box = lambda hostname: lic_calls.append(hostname)
rc.setup_rodeo_cli = lambda hostname, install_url, allow="": None

rc.lab_in_a_box_installed = lambda hostname: False
definition_tiein = primary.LabDefinition({
    "nodes": {"sol.mydemo.lab": {"addons": [{"rodeo_cli": {"rodeo_cli_install_lab_in_a_box": "true"}}]}},
    "rodeo_cli": {},
}, source_path="/tmp/fake.json", fmt="json")
primary.load_definition = lambda path: definition_tiein
sys.argv = ["install_rodeo_cli", "/tmp/fake.json"]
rc.main()
check("main(): rodeo_cli_install_lab_in_a_box=true installs lab-in-a-box first when it's missing",
      lic_calls == ["sol.mydemo.lab"])
check("main(): checks the node answers SSH before anything else", reach_calls == ["sol.mydemo.lab"])

# An unreachable node stops the addon before the lab-in-a-box probe or install runs.
lic_calls.clear()
probed = []
rc.lab_in_a_box_installed = lambda hostname: probed.append(hostname) or False


def _unreachable(hostname):
    raise SystemExit(1)


rc.require_ssh = _unreachable
try:
    rc.main()
except SystemExit:
    pass
check("main(): an unreachable node never reaches the lab-in-a-box probe or install",
      probed == [] and lic_calls == [])
rc.require_ssh = lambda hostname: reach_calls.append(hostname)

lic_calls.clear()
rc.lab_in_a_box_installed = lambda hostname: True
rc.main()
check("main(): already-installed lab-in-a-box is not reinstalled", lic_calls == [])

lic_calls.clear()
rc.lab_in_a_box_installed = lambda hostname: False
definition_no_tiein = primary.LabDefinition({
    "nodes": {"sol.mydemo.lab": {"addons": ["rodeo_cli"]}},
    "rodeo_cli": {},
}, source_path="/tmp/fake.json", fmt="json")
primary.load_definition = lambda path: definition_no_tiein
rc.main()
check("main(): the tie-in is opt-in — unset means no lab-in-a-box install at all", lic_calls == [])


# ── main(): per-node dispatch, and the default install URL ──────────────────
definition = primary.LabDefinition({
    "nodes": {
        "sol.mydemo.lab": {"addons": ["rodeo_cli"]},
        "luna.mydemo.lab": {"addons": [{"rodeo_cli": {"rodeo_cli_install_url": "https://mirror.lab/install.sh"}}]},
        "earth.mydemo.lab": {"addons": ["rancher"]},
    },
    "rodeo_cli": {},
}, source_path="/tmp/fake.json", fmt="json")

setup_calls = []
rc.setup_rodeo_cli = lambda hostname, install_url, allow="": setup_calls.append((hostname, install_url))
sys.argv = ["install_rodeo_cli", "/tmp/fake.json"]
primary.load_definition = lambda path: definition
rc.main()
check("main(): runs on every node listing rodeo_cli, not earth (which doesn't)",
      {h for h, _ in setup_calls} == {"sol.mydemo.lab", "luna.mydemo.lab"})
check("main(): the shared section with no override uses the default install URL",
      dict(setup_calls)["sol.mydemo.lab"] == rc.DEFAULT_INSTALL_URL)
check("main(): a per-node override wins over the default install URL",
      dict(setup_calls)["luna.mydemo.lab"] == "https://mirror.lab/install.sh")


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All install_rodeo_cli checks passed")
