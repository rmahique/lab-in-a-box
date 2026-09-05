#!/usr/bin/env python3
# Part of lab-in-a-box, Python orchestrator for lab setup
# Author/s: Raul Mahiques
# License: GPLv3
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
#
# Supports lab definition files in JSON or YAML format.
# YAML files are converted to a temporary JSON file so all shell scripts
# (setup_vm.sh, destroy_vm.sh, install_*) continue to work unchanged.
#
# Requires: pyyaml (only for YAML input)  →  pip install pyyaml

"""
setup_lab.py  —  equivalent of setup_lab.sh with JSON + YAML support.

Usage:
    setup_lab.py <definition.json>
    setup_lab.py <definition.yaml>
    setup_lab.py --delay 5 mylab.yaml
"""

__version__ = "def08d9"

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ─── Terminal colours ─────────────────────────────────────────────────────────

RED    = "\033[1;91m"
YELLOW = "\033[1;33m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


# ─── Output helpers ───────────────────────────────────────────────────────────

_lvl = 0


def log(msg: str, extra_indent: int = 0) -> None:
    print("  " * (_lvl + extra_indent) + msg)


def section(msg: str) -> None:
    print(f"\n{BOLD}{msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}WARNING:{RESET} {msg}", file=sys.stderr)


def die(msg: str) -> None:
    print(f"{RED}ERROR:{RESET} {msg}", file=sys.stderr)
    sys.exit(1)


# ─── Subprocess helpers ───────────────────────────────────────────────────────

def run(cmd: list, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, env=env)
    if check and result.returncode != 0:
        die(f"Command failed (rc={result.returncode}): {' '.join(str(c) for c in cmd)}")
    return result


def bash_lab(json_file: str, code: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """
    Source lab_creation.defaults + primary_functions (which loads lab_creation.cfg
    and the main library), then execute arbitrary bash code with inputFile set.
    """
    env = {**os.environ, **(extra_env or {})}
    script = (
        "set -e\n"
        "if [[ -f /etc/lab_creation.defaults ]]; then\n"
        "    . /etc/lab_creation.defaults\n"
        "elif [[ -f lab_creation.defaults ]]; then\n"
        "    . lab_creation.defaults\n"
        "else\n"
        "    echo 'ERROR: lab_creation.defaults not found' >&2; exit 1\n"
        "fi\n"
        ". ${_primary_funtions}\n"
        f"inputFile={shlex.quote(json_file)}\n"
        f"{code}\n"
    )
    return subprocess.run(["bash", "-c", script], env=env)


# ─── Definition loading ───────────────────────────────────────────────────────

def load_definition(path: str) -> dict:
    """
    Load a lab definition from a JSON or YAML file.
    Detection order:
      1. .yaml / .yml extension  → parse as YAML (requires pyyaml)
      2. other extension          → try JSON, then YAML as fallback
    """
    p = Path(path)
    if not p.exists():
        die(f"Definition file '{path}' not found")

    suffix = p.suffix.lower()
    text = p.read_text()

    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            die(
                "PyYAML is required for YAML definition files.\n"
                "Install it with:  pip install pyyaml"
            )
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            die(f"'{path}' does not contain a YAML mapping at the top level")
        return data

    # JSON or unknown extension
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
            warn(f"'{path}' is not valid JSON — attempting YAML parse")
            return yaml.safe_load(text)
        except ImportError:
            die(
                f"'{path}' is not valid JSON and PyYAML is not available for fallback.\n"
                "Install PyYAML with:  pip install pyyaml"
            )


def as_json_file(definition: dict, original_path: str) -> tuple[str, bool]:
    """
    Shell scripts require JSON (they use jq internally).
    If the source was already valid JSON return it as-is (is_temp=False).
    Otherwise write a temporary JSON file and return (path, True).
    The caller must delete the temp file when done.
    """
    p = Path(original_path)
    if p.suffix.lower() not in (".yaml", ".yml"):
        try:
            with open(original_path) as f:
                json.load(f)
            return original_path, False
        except (json.JSONDecodeError, OSError):
            pass  # fall through: source was parsed as YAML fallback

    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="lab_setup_")
    with os.fdopen(fd, "w") as f:
        json.dump(definition, f, indent=2)
    return tmp, True


# ─── Definition accessors ─────────────────────────────────────────────────────

def nodes(definition: dict) -> dict:
    return definition.get("nodes", {})


def kclusters(definition: dict) -> dict:
    return definition.get("kclusters", {})


def has_kclusters(definition: dict) -> bool:
    return bool(definition.get("kclusters"))


def node_kcluster(definition: dict, vm_name: str) -> str:
    return nodes(definition).get(vm_name, {}).get("kcluster", "")


def cluster_addons(definition: dict, clu_name: str) -> list:
    return kclusters(definition).get(clu_name, {}).get("addons", [])


def vm_addons(definition: dict, vm_name: str) -> list:
    return nodes(definition).get(vm_name, {}).get("addons", [])


def first_node_in_cluster(definition: dict, clu_name: str) -> str:
    return next(
        (v for v in nodes(definition) if node_kcluster(definition, v) == clu_name),
        "",
    )


# ─── SSH polling ──────────────────────────────────────────────────────────────

def wait_for_ssh(hostname: str, timeout: int = 300, interval: int = 5) -> None:
    log(f"Waiting for {RED}{hostname}{RESET} to come online", extra_indent=1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((hostname, 22), timeout=3):
                log(f"{RED}{hostname}{RESET} is online", extra_indent=1)
                return
        except OSError:
            time.sleep(interval)
    die(f"Timeout waiting for {hostname} to come online")


# ─── Phases ───────────────────────────────────────────────────────────────────

def phase_dns(definition: dict, json_file: str) -> None:
    section("Registering Kubernetes cluster DNS entries")
    for clu_name in kclusters(definition):
        log(f"Cluster: {RED}{clu_name}{RESET}", extra_indent=1)
        r = bash_lab(
            json_file,
            f"clu_name={shlex.quote(clu_name)}\n"
            f"load_kclu_vars\n"
            f"add_kclu_dns\n",
        )
        if r.returncode != 0:
            die(f"Failed to register DNS for cluster '{clu_name}'")


def phase_create_vms(definition: dict, json_file: str) -> None:
    section("Creating VMs")
    for vm_name in nodes(definition):
        log(f"Node: {RED}{vm_name}{RESET}", extra_indent=1)
        # Remove stale known_hosts entry silently
        subprocess.run(
            ["ssh-keygen", "-f", os.path.expanduser("~/.ssh/known_hosts"), "-R", vm_name],
            capture_output=True,
        )
        run(["destroy_vm.sh", json_file, vm_name], check=False)
        run(["setup_vm.sh", json_file, vm_name])


def phase_reboot_and_wait(definition: dict) -> None:
    vm_list = list(nodes(definition))

    section("Rebooting all nodes")
    for vm_name in vm_list:
        log(f"Restart {RED}{vm_name}{RESET}", extra_indent=1)
        subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-q",
             f"root@{vm_name}", "reboot"],
            check=False,
        )

    time.sleep(5)

    section("Waiting for nodes to come back online")
    for vm_name in vm_list:
        wait_for_ssh(vm_name)


def phase_install_k8s(definition: dict, json_file: str) -> None:
    """
    Install Kubernetes on all nodes.

    All nodes are processed inside a single bash process so that the token[]
    associative array (which tracks the first server per cluster) and
    RANCHER1_IP persist across node iterations — exactly as setup_lab.sh does.
    """
    section("Installing Kubernetes on each node")

    lines = [
        "declare -A token",
        "declare RANCHER1_IP",
    ]

    for vm_name, node_cfg in nodes(definition).items():
        clu_name = node_cfg.get("kcluster", "")
        if not clu_name:
            lines.append(
                f"echo 'WARNING: no kcluster defined for {vm_name}, skipping'"
            )
            continue

        clu_type = kclusters(definition).get(clu_name, {}).get("clu_type", "")
        if not clu_type:
            lines.append(
                f"echo 'WARNING: clu_type not defined for cluster {clu_name} ({vm_name}), skipping'"
            )
            continue

        lines += [
            f"_vm_name={shlex.quote(vm_name)}",
            f"clu_name={shlex.quote(clu_name)}",
            "load_vm_vars",
            "load_kclu_vars",
            "load_def",   # sets ssh_command="ssh ... root@${_vm_name}"
            f"echo 'Installing {clu_type} on {vm_name} for cluster {clu_name}'",
            f"setup_{clu_type} || {{ echo 'FAILED: setup_{clu_type} on {vm_name}' >&2; exit 1; }}",
        ]

    r = bash_lab(json_file, "\n".join(lines))
    if r.returncode != 0:
        die("Kubernetes installation phase failed")


def phase_cluster_addons(definition: dict, json_file: str) -> None:
    section("Installing Kubernetes cluster addons")
    installed: set[tuple] = set()

    for clu_name, clu_cfg in kclusters(definition).items():
        addons = clu_cfg.get("addons", [])
        if not addons:
            log(f"No addons for cluster {RED}{clu_name}{RESET}", extra_indent=1)
            continue

        mgm_node = clu_cfg.get("mgm_node", "")
        vm_name = mgm_node or first_node_in_cluster(definition, clu_name)
        if not vm_name:
            warn(f"No node found for cluster '{clu_name}', skipping addons")
            continue

        log(
            f"Installing cluster {RED}{clu_name}{RESET} "
            f"addons from {RED}{vm_name}{RESET}",
            extra_indent=1,
        )
        for addon in addons:
            if (clu_name, addon) in installed:
                continue
            installer = shutil.which(f"install_{addon}")
            if not installer:
                die(f"Addon script 'install_{addon}' not found in PATH")
            log(
                f"Running addon {RED}{addon}{RESET} on {RED}{vm_name}{RESET} "
                f"for cluster {RED}{clu_name}{RESET}",
                extra_indent=2,
            )
            env = {**os.environ, "_vm_name": vm_name, "clu_name": clu_name}
            run([installer, json_file], env=env)
            installed.add((clu_name, addon))


def phase_vm_addons(definition: dict, json_file: str) -> None:
    section("Installing VM addons")
    any_found = False

    for vm_name in nodes(definition):
        addons = vm_addons(definition, vm_name)
        if not addons:
            continue
        any_found = True
        log(f"Installing {RED}{vm_name}{RESET} addons", extra_indent=1)
        for addon in addons:
            installer = shutil.which(f"install_{addon}")
            if not installer:
                die(f"Addon script 'install_{addon}' not found in PATH")
            log(
                f"Running addon {RED}{addon}{RESET} on {RED}{vm_name}{RESET}",
                extra_indent=2,
            )
            env = {**os.environ, "_vm_name": vm_name}
            run([installer, json_file], env=env)

    if not any_found:
        log("No VM addons defined", extra_indent=1)


# ─── Validation ───────────────────────────────────────────────────────────────

def validate(definition: dict, path: str) -> None:
    """Catch obvious definition mistakes before doing any work."""
    if not definition.get("nodes"):
        die(f"'{path}' has no 'nodes' section")

    if "cluster" in definition and "kclusters" not in definition:
        warn(
            "Definition uses the old 'cluster' format (single cluster, no per-node kcluster).\n"
            "         setup_lab.py only supports the 'kclusters' format for Kubernetes setup.\n"
            "         VMs will be created but Kubernetes will NOT be installed."
        )

    for vm_name, cfg in definition.get("nodes", {}).items():
        clu_name = cfg.get("kcluster", "")
        if clu_name and clu_name not in definition.get("kclusters", {}):
            die(
                f"Node '{vm_name}' references kcluster '{clu_name}' "
                f"which is not defined in 'kclusters'"
            )


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="lab-in-a-box: set up a lab from a JSON or YAML definition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Supported input formats:\n"
            "  JSON  —  passed directly to shell scripts (jq-compatible)\n"
            "  YAML  —  converted to a temporary JSON file transparently\n"
            "           Requires: pip install pyyaml\n\n"
            "Examples:\n"
            "  setup_lab.py mylab.json\n"
            "  setup_lab.py mylab.yaml\n"
            "  setup_lab.py --delay 5 mylab.yaml\n"
        ),
    )
    parser.add_argument(
        "definition",
        help="Lab definition file (.json, .yaml, or .yml)",
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"setup_lab.py {__version__}",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=None,
        metavar="MINUTES",
        help=(
            "Extra stabilisation delay in minutes after Kubernetes install "
            "(overrides the 'delay_min' value in the definition file)"
        ),
    )
    args = parser.parse_args()

    definition = load_definition(args.definition)
    validate(definition, args.definition)

    json_file, is_temp = as_json_file(definition, args.definition)

    try:
        lab_name  = definition.get("common", {}).get("lab_name", args.definition)
        delay_min = (
            args.delay
            if args.delay is not None
            else int(definition.get("common", {}).get("delay_min", 2))
        )
        k8s = has_kclusters(definition)

        if k8s:
            log(
                f"\n{BOLD}Setup lab {RED}{lab_name}{RESET}"
                f"{BOLD} (VMs + Kubernetes clusters){RESET}"
            )
        else:
            log(f"\n{BOLD}Setup lab {RED}{lab_name}{RESET}{BOLD} (VMs only){RESET}")

        if k8s:
            phase_dns(definition, json_file)

        phase_create_vms(definition, json_file)

        if k8s:
            phase_reboot_and_wait(definition)
            phase_install_k8s(definition, json_file)

            total_wait = 2 + delay_min
            section(f"Waiting {total_wait} min for cluster(s) to stabilise")
            time.sleep(60 * total_wait)

            phase_cluster_addons(definition, json_file)

        phase_vm_addons(definition, json_file)

    finally:
        if is_temp:
            try:
                os.unlink(json_file)
            except OSError:
                pass


if __name__ == "__main__":
    main()
