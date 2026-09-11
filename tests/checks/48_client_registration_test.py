#!/usr/bin/env python3
# Unit tests for the general per-node addon-config-override mechanism (added
# 2026-09-11): an addons[] entry is either a plain "<addon>" string, or a
# single-key {"<addon>": {...}} mapping overriding that addon's shared
# top-level config for that one node — e.g. a lab registering many OSes
# against one shared Uyuni/SMLM server, each node needing its own
# client_registration_activation_key. Covers the shared parsing helpers
# (apps.addon_entry_name/addon_entry_overrides), k8s.addon_nodes()/
# addon_node_config(), and install_client_registration.py's own use of
# them. register_client() itself is mocked (no real spacecmd/mgrctl). Run
# from 48_client_registration.sh, in its own container — see
# tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import k8s  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── apps.addon_entry_name / addon_entry_overrides: the shared parsing ──────
check("addon_entry_name: a plain string entry is returned unchanged",
      apps.addon_entry_name("mariadb") == "mariadb")
check("addon_entry_name: a {addon: {...}} entry returns the addon name",
      apps.addon_entry_name({"client_registration": {"client_registration_activation_key": "1-x"}})
      == "client_registration")
check("addon_entry_overrides: a plain string entry has no overrides",
      apps.addon_entry_overrides("mariadb") == {})
check("addon_entry_overrides: a {addon: {...}} entry returns its own value",
      apps.addon_entry_overrides({"client_registration": {"client_registration_activation_key": "1-x"}})
      == {"client_registration_activation_key": "1-x"})

died = []
apps.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)
try:
    apps.addon_entry_name({"a": {}, "b": {}})
except SystemExit:
    pass
check("addon_entry_name: an entry with more than one key dies clearly", any("exactly one key" in m for m in died))


# ── apps.collect_addon_names: mixed plain-string and nested entries ────────
mixed_def = {
    "kclusters": {"c1": {"addons": ["rancher"]}},
    "nodes": {
        "vm1": {"addons": ["mariadb", {"client_registration": {"client_registration_activation_key": "1-x"}}]},
        "vm2": {"addons": ["mariadb"]},  # same addon, no override -> still just "mariadb"
    },
}
check("collect_addon_names: dedupes correctly across plain and nested entries",
      apps.collect_addon_names(mixed_def) == ["client_registration", "mariadb", "rancher"])


# ── k8s.addon_nodes: a nested entry counts as "has this addon" ─────────────
nodes = k8s.addon_nodes(mixed_def, "client_registration")
check("addon_nodes: finds the node whose addons[] entry is nested for this addon",
      [n for n, _ in nodes] == ["vm1"])
check("addon_nodes: a node using a DIFFERENT addon isn't matched",
      [n for n, _ in k8s.addon_nodes(mixed_def, "rancher")] == [])


# ── k8s.addon_node_config: the actual per-node merge ────────────────────────
cfg_def = {
    "client_registration": {
        "client_registration_server": "smlm52beta.mydemo.lab",
        "client_registration_admin_user": "admin",
        "client_registration_activation_key": "1-shared-default",
    },
    "nodes": {
        "mercury": {"addons": [{"client_registration": {"client_registration_activation_key": "1-sles15sp7"}}]},
        "callisto": {"addons": [{"client_registration": {"client_registration_activation_key": "1-debian13"}}]},
        "ganymede": {"addons": ["client_registration"]},  # no override -> shared default
    },
}
merc = k8s.addon_node_config(cfg_def, "client_registration", "mercury")
check("addon_node_config: the node's own override wins", merc["client_registration_activation_key"] == "1-sles15sp7")
check("addon_node_config: fields the node doesn't override still come from the shared section",
      merc["client_registration_server"] == "smlm52beta.mydemo.lab"
      and merc["client_registration_admin_user"] == "admin")

call = k8s.addon_node_config(cfg_def, "client_registration", "callisto")
check("addon_node_config: a different node gets its OWN override, not another node's",
      call["client_registration_activation_key"] == "1-debian13")

gany = k8s.addon_node_config(cfg_def, "client_registration", "ganymede")
check("addon_node_config: a plain-string entry (no override) falls back to the shared default",
      gany["client_registration_activation_key"] == "1-shared-default")

check("addon_node_config: overriding one node never mutates the shared top-level section",
      cfg_def["client_registration"]["client_registration_activation_key"] == "1-shared-default")


# ── install_client_registration.py: end-to-end through main() ──────────────
import install_client_registration as icr  # noqa: E402

calls = []
icr.register_client = lambda vm_name, cfg: calls.append((vm_name, cfg))
icr.primary.load_definition = lambda path: cfg_def

old_argv = sys.argv
sys.argv = ["install_client_registration.py", "lab.json"]
try:
    icr.main()
finally:
    sys.argv = old_argv

by_node = {vm: cfg for vm, cfg in calls}
check("install_client_registration main(): registers every node with the addon, and only those",
      set(by_node) == {"mercury", "callisto", "ganymede"})
check("install_client_registration main(): each node's nested override reaches register_client()",
      by_node["mercury"]["client_registration_activation_key"] == "1-sles15sp7"
      and by_node["callisto"]["client_registration_activation_key"] == "1-debian13"
      and by_node["ganymede"]["client_registration_activation_key"] == "1-shared-default")

# _vm_name env scoping still dispatches to exactly one node, with its override
calls.clear()
import os  # noqa: E402
os.environ["_vm_name"] = "callisto"
sys.argv = ["install_client_registration.py", "lab.json"]
try:
    icr.main()
finally:
    del os.environ["_vm_name"]
    sys.argv = old_argv
check("install_client_registration main(): _vm_name env scopes to one node, override still applied",
      len(calls) == 1 and calls[0][0] == "callisto"
      and calls[0][1]["client_registration_activation_key"] == "1-debian13")


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all client_registration checks passed")
