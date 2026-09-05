#!/usr/bin/env python3
# Mocked unit tests for scripts/setup_lab.py — no live KVM
# host or Kubernetes cluster is available in this project. Covers:
# _merged_env's defaults/config/JSON precedence, the addon-dispatch block
# (shutil.which + subprocess.run, duplicate-addon skip, missing-installer
# die()) for both cluster- and VM-level addons, and phase_create_vms's
# --keep reusability logic. Run from 17_setup_lab.sh, in its own container
# — see tests/run_tests.sh.
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import setup_lab  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeCompleted:
    # subprocess is one shared module object — mocking setup_lab.subprocess.run
    # also mocks lab_creation.subprocess.run (same underlying `subprocess`
    # module), which load_vm_vars' _detect_gateway()/_detect_netmask() call
    # for their own, unrelated "ip route"/"ip addr" probes. stdout must be a
    # real string here so those don't crash on a missing attribute further
    # down the call chain (phase_create_vms -> _merged_env -> load_vm_vars).
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ── _merged_env: defaults -> config -> per-VM JSON precedence (JSON wins) ────
definition = {
    "common": {"VM_MEM": "2048", "shared_only": "common-val"},
    "nodes": {"vm1": {"VM_MEM": "8192", "myip": "10.0.0.1"}},
}
config = {"VM_MEM": "4096", "cfg_only": "cfg-val"}
defaults = {"VM_MEM": "1024", "def_only": "def-val"}
env = setup_lab._merged_env(definition, config, defaults, "vm1")
check("_merged_env: per-VM JSON value wins over config and defaults", env["VM_MEM"] == "8192")
check("_merged_env: config value passes through when JSON doesn't override it", env["cfg_only"] == "cfg-val")
check("_merged_env: defaults value passes through when nothing overrides it", env["def_only"] == "def-val")
check("_merged_env: common section values are present", env["shared_only"] == "common-val")


# ── _install_cluster_addons: dispatch, duplicate skip, missing installer ────
# apps.check_requirements is stubbed out here: it's exercised by its own
# concerns (plugin target/distro compatibility), not by what this test is
# actually after (setup_lab.py's own installer lookup + dispatch + dedup
# logic) — and shutil.which is one shared, mutable module object, so
# pointing it at a fake path for setup_lab.py's own lookup below would also
# feed apps.load_plugin() a "found" installer it can't really import,
# falling back to a container-only default plugin that would spuriously
# reject VM-targeted addons in the phase_vm_addons tests further down.
setup_lab.apps.check_requirements = lambda *a, **kw: None
setup_lab.shutil.which = lambda name: "/fake/bin/{}".format(name) if "missing" not in name else None
run_calls = []
setup_lab.subprocess.run = lambda args, env=None, **kw: run_calls.append((args, env)) or FakeCompleted()

clu_cfg = {"addons": ["rancher", "rancher", "longhorn"], "clu_type": "rke2", "mgm_node": "srv1"}
definition2 = {"nodes": {"srv1": {"kcluster": "c1"}, "agt1": {"kcluster": "c1"}}}
setup_lab._install_cluster_addons(definition2, config, defaults, "lab.json", "c1", clu_cfg)
check("_install_cluster_addons: a repeated addon in the list is only run once",
      len(run_calls) == 2)
check("_install_cluster_addons: runs on the cluster's mgm_node",
      all(env["_vm_name"] == "srv1" for _, env in run_calls))
check("_install_cluster_addons: env carries the cluster name",
      all(env["clu_name"] == "c1" for _, env in run_calls))
check("_install_cluster_addons: invokes the resolved installer with the JSON file",
      all(args[0].startswith("/fake/bin/install_") and args[1] == "lab.json" for args, env in run_calls))

run_calls.clear()
died = False
try:
    setup_lab._install_cluster_addons(
        definition2, config, defaults, "lab.json", "c1",
        {"addons": ["totally-missing"], "clu_type": "rke2"})
except SystemExit:
    died = True
check("_install_cluster_addons: dies when an addon's install script isn't found", died)
check("_install_cluster_addons: never invokes subprocess.run for a missing installer", run_calls == [])

run_calls.clear()
setup_lab._install_cluster_addons(definition2, config, defaults, "lab.json", "c1", {"addons": [], "clu_type": "rke2"})
check("_install_cluster_addons: no-op when the cluster has no addons", run_calls == [])


# ── phase_vm_addons: per-node dispatch ───────────────────────────────────────
run_calls.clear()
setup_lab.shutil.which = lambda name: "/fake/bin/{}".format(name)
definition3 = {"nodes": {"vm1": {"addons": ["mariadb", "openldap"]}, "vm2": {}}}
setup_lab.phase_vm_addons(definition3, "lab.json")
check("phase_vm_addons: runs each of vm1's addons once", len(run_calls) == 2)
check("phase_vm_addons: runs on the owning node", all(env["_vm_name"] == "vm1" for _, env in run_calls))
check("phase_vm_addons: a node with no addons is skipped entirely",
      not any("vm2" == env.get("_vm_name") for _, env in run_calls))


# ── apps.collect_addon_names: every addon referenced anywhere in the lab ───
addon_def = {
    "kclusters": {"c1": {"addons": ["rancher", "longhorn"]}, "c2": {"addons": ["longhorn"]}},
    "nodes": {"vm1": {"addons": ["mariadb"]}, "vm2": {}},
}
check("collect_addon_names: every kcluster- and node-level addon is included, deduped",
      setup_lab.apps.collect_addon_names(addon_def) == ["longhorn", "mariadb", "rancher"])
check("collect_addon_names: a lab with no addons anywhere returns an empty list",
      setup_lab.apps.collect_addon_names({"kclusters": {"c1": {}}, "nodes": {"vm1": {}}}) == [])


# ── validate_addon_configs: --validate every referenced addon before any VM/
# cluster work starts ───────────────────────────────────────────────────────
# Found in code review 2026-09-05: each addon's own Validator (vns/vport/
# vver/vreq/...) checks were only ever reachable by an operator manually
# running `install_<addon> --validate <file>` — setup_lab.py never called
# it at all, so a bad addon-config value surfaced only once that addon
# actually ran, potentially after VMs/clusters were already created.
validate_def = {"kclusters": {"c1": {"addons": ["rancher", "longhorn"]}}, "nodes": {}}

setup_lab.shutil.which = lambda name: "/fake/bin/{}".format(name)
setup_lab.subprocess.run = lambda args, **kw: FakeCompleted(returncode=0, stdout="")
check("validate_addon_configs: every addon validating clean returns True",
      setup_lab.validate_addon_configs(validate_def, "lab.json") is True)

setup_lab.subprocess.run = lambda args, **kw: (
    FakeCompleted(returncode=1, stdout="[ERROR] longhorn.longhorn_ns='bad ns': invalid namespace\n")
    if "install_longhorn" in args[0] else FakeCompleted(returncode=0, stdout=""))
_out = io.StringIO()
with redirect_stdout(_out):
    result = setup_lab.validate_addon_configs(validate_def, "lab.json")
check("validate_addon_configs: one failing addon's [ERROR] makes the whole check fail",
      result is False)
check("validate_addon_configs: the failing addon's own error text is included in the report",
      "longhorn_ns" in _out.getvalue())

setup_lab.shutil.which = lambda name: None
_out = io.StringIO()
with redirect_stdout(_out):
    result = setup_lab.validate_addon_configs(validate_def, "lab.json")
check("validate_addon_configs: a completely missing installer is reported as an error too",
      result is False and "not found on PATH" in _out.getvalue())

setup_lab.shutil.which = lambda name: "/fake/bin/{}".format(name)
setup_lab.subprocess.run = lambda args, **kw: FakeCompleted(returncode=0, stdout="")
check("validate_addon_configs: a lab with no addons anywhere returns True without running anything",
      setup_lab.validate_addon_configs({"kclusters": {}, "nodes": {}}, "lab.json") is True)


# ── phase_create_vms: --keep reusability + existing-node handling ──────────
calls = {"destroy": [], "provision": [], "check_ssh_conn": []}
setup_lab.destroy_vm = lambda definition, config, defaults, vm_name: calls["destroy"].append(vm_name)
setup_lab.provision_vm = lambda definition, config, defaults, vm_name: calls["provision"].append(vm_name)
setup_lab.lc.check_ssh_conn = lambda vm_name: calls["check_ssh_conn"].append(vm_name)
setup_lab.subprocess.run = lambda *a, **kw: FakeCompleted()

# Existing node: never destroyed/provisioned, just waited on.
setup_lab.targets.is_existing_node = lambda node_cfg: bool(node_cfg.get("existing"))
definition4 = {"nodes": {"existing1": {"existing": True}}}
setup_lab.phase_create_vms(definition4, config, defaults, "lab.json", keep=False)
check("phase_create_vms: an 'existing' node is never destroyed or provisioned",
      calls["destroy"] == [] and calls["provision"] == [])
check("phase_create_vms: an 'existing' node is still waited on for SSH",
      calls["check_ssh_conn"] == ["existing1"])

# keep=True, VM matches definition -> skipped (not destroyed/recreated).
for k in calls:
    calls[k].clear()
setup_lab.lc.locate_kvm_host = lambda definition, vm_name, config: ("hv1", "qemu+ssh://...")
setup_lab.lc.vm_is_reusable = lambda virt_srv, vm_name, mymac, myip, remote_host=None: True
definition5 = {"nodes": {"vm1": {"myip": "10.0.0.1", "mymac": "aa:bb:cc:dd:ee:01"}}}
setup_lab.phase_create_vms(definition5, config, defaults, "lab.json", keep=True)
check("phase_create_vms: --keep + a reusable VM is skipped (no destroy/recreate)",
      calls["destroy"] == [] and calls["provision"] == [])

# keep=True, but the VM doesn't match (or doesn't exist) -> destroyed and recreated.
for k in calls:
    calls[k].clear()
setup_lab.lc.vm_is_reusable = lambda virt_srv, vm_name, mymac, myip, remote_host=None: False
setup_lab.phase_create_vms(definition5, config, defaults, "lab.json", keep=True)
check("phase_create_vms: --keep + a non-reusable VM is destroyed and recreated",
      calls["destroy"] == ["vm1"] and calls["provision"] == ["vm1"])

# keep=True, VM never existed (locate_kvm_host raises) -> destroyed (no-op) and recreated.
for k in calls:
    calls[k].clear()


def _no_such_vm(definition, vm_name, config):
    raise SystemExit(1)


setup_lab.lc.locate_kvm_host = _no_such_vm
setup_lab.phase_create_vms(definition5, config, defaults, "lab.json", keep=True)
check("phase_create_vms: --keep + a VM that doesn't exist yet is still (re)created",
      calls["destroy"] == ["vm1"] and calls["provision"] == ["vm1"])

# keep=False: always destroyed and recreated, reusability never even considered.
for k in calls:
    calls[k].clear()
setup_lab.lc.locate_kvm_host = lambda definition, vm_name, config: ("hv1", "qemu+ssh://...")
# vm_is_reusable() returns True here, but `keep and keep_virt_srv and
# vm_is_reusable(...)` short-circuits on `keep` being False, so this is
# never even consulted — proving the destroy/recreate still happens either way.
setup_lab.lc.vm_is_reusable = lambda *a, **kw: True
setup_lab.phase_create_vms(definition5, config, defaults, "lab.json", keep=False)
check("phase_create_vms: without --keep, the VM is always destroyed and recreated",
      calls["destroy"] == ["vm1"] and calls["provision"] == ["vm1"])

# One node's provision_vm() dying (check_ssh_conn's own die() on a boot-wait
# timeout, mirrored here as SystemExit) must not abort the whole multi-node
# deploy — reported live 2026-09-01: it used to take the entire run down.
for k in calls:
    calls[k].clear()


def _provision_second_node_dies(definition, config, defaults, vm_name):
    calls["provision"].append(vm_name)
    if vm_name == "vm_slow":
        raise SystemExit(1)


setup_lab.provision_vm = _provision_second_node_dies
setup_lab.lc.vm_is_reusable = lambda *a, **kw: False
definition6 = {"nodes": {
    "vm_first": {"myip": "10.0.0.10", "mymac": "aa:bb:cc:dd:ee:10"},
    "vm_slow": {"myip": "10.0.0.11", "mymac": "aa:bb:cc:dd:ee:11"},
    "vm_last": {"myip": "10.0.0.12", "mymac": "aa:bb:cc:dd:ee:12"},
}}
setup_lab.phase_create_vms(definition6, config, defaults, "lab.json", keep=False)
check("phase_create_vms: a node whose provision_vm() dies doesn't stop the remaining nodes",
      calls["provision"] == ["vm_first", "vm_slow", "vm_last"])

for k in calls:
    calls[k].clear()


def _provision_second_node_raises(definition, config, defaults, vm_name):
    calls["provision"].append(vm_name)
    if vm_name == "vm_slow":
        raise RuntimeError("SSH command failed")


setup_lab.provision_vm = _provision_second_node_raises
setup_lab.phase_create_vms(definition6, config, defaults, "lab.json", keep=False)
check("phase_create_vms: a node whose provision_vm() raises RuntimeError doesn't stop the rest either",
      calls["provision"] == ["vm_first", "vm_slow", "vm_last"])


# ── main(): --version / --help / --keep parsing ──────────────────────────────
old_argv = sys.argv
sys.argv = ["setup_lab.py", "--version"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        setup_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main --version: exits 0 and prints the version", code == 0 and "setup_lab.py" in buf.getvalue())

sys.argv = ["setup_lab.py", "--help"]
buf = io.StringIO()
code = None
try:
    with redirect_stdout(buf):
        setup_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main --help: exits 0 and prints usage mentioning --keep", code == 0 and "--keep" in buf.getvalue())

# main(): a failing validate_addon_configs() must abort before setup_lab() —
# a bad addon config must not leave a half-deployed lab behind it.
setup_lab.primary.load_defaults = lambda: {}
setup_lab.primary.load_config = lambda: {}
setup_lab.primary.load_definition = lambda path: {"nodes": {}, "common": {}}
setup_lab.lc.validate_lab_definition = lambda *a, **kw: True
setup_lab.validate_addon_configs = lambda *a, **kw: False
setup_lab_calls = []
setup_lab.setup_lab = lambda *a, **kw: setup_lab_calls.append(a)
sys.argv = ["setup_lab.py", "lab.json"]
code = None
try:
    setup_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main: exits 1 when validate_addon_configs() fails", code == 1)
check("main: never reaches setup_lab() when addon-config validation fails", setup_lab_calls == [])

setup_lab.validate_addon_configs = lambda *a, **kw: True
sys.argv = ["setup_lab.py", "lab.json"]
code = None
try:
    setup_lab.main()
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("main: reaches setup_lab() when addon-config validation passes",
      len(setup_lab_calls) == 1)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all setup_lab checks passed")
