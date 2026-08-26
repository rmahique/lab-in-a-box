"""
primary.py — Lab definition loading, config parsing, and validation.

Python equivalent of primary_functions.bash.

Typical usage:
    from primary import load_definition, load_config, load_defaults, validate_definition
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import json
import os
import re
import sys
from pathlib import Path


# ── Definition loading ────────────────────────────────────────────────────────

def load_definition(path):
    """
    Load a lab definition from a JSON or YAML file.

    Returns a dict. YAML input requires pyyaml (pip install pyyaml).
    Falls back to YAML parsing if the file is not valid JSON.
    """
    p = Path(path)
    if not p.exists():
        _die("Lab definition file '{}' not found".format(path))

    text = p.read_text()

    if p.suffix.lower() in (".yaml", ".yml"):
        return _load_yaml(text, path)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import warnings
        warnings.warn("'{}' is not valid JSON — attempting YAML parse".format(path))
        return _load_yaml(text, path)


def _load_yaml(text, path):
    try:
        import yaml
    except ImportError:
        _die(
            "PyYAML is required for YAML definition files.\n"
            "Install it with:  pip install pyyaml"
        )
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        _die("'{}' does not contain a mapping at the top level".format(path))
    return data


# ── Config / defaults loading ─────────────────────────────────────────────────

_DEFAULT_CFG_PATHS      = ["/etc/lab_creation.cfg", "lab_creation.cfg"]
_DEFAULT_DEFAULTS_PATHS = ["/etc/lab_creation.defaults", "lab_creation.defaults"]


def load_config(paths=None):
    """
    Load lab_creation.cfg (node-specific settings: REMOTE_HOST, ROOT_SSH_KEY, VIRT_SRV, …).

    Searches paths in order; uses system + local defaults when paths is None.
    Returns a dict of the parsed key-value pairs.
    Raises SystemExit if no config file is found.
    """
    return _load_shell_vars_file(paths or _DEFAULT_CFG_PATHS, "lab_creation.cfg")


def load_defaults(paths=None):
    """
    Load lab_creation.defaults (system-wide defaults: _lib_path, VM_IMG_LOC, delay_min, …).

    Parses only simple KEY=value assignments; skips bash arrays and expressions.
    Returns a dict.
    """
    return _load_shell_vars_file(paths or _DEFAULT_DEFAULTS_PATHS, "lab_creation.defaults")


def load_shell_vars(path):
    """
    Parse an arbitrary simple-shell-variable config file at an exact path
    (unlike load_config/load_defaults, which search a list of default
    locations for a specific filename). Used for config files outside the
    lab_creation.cfg/.defaults pair — e.g. setup_demo_server/lab.cfg.
    Returns a dict. Raises SystemExit if the file doesn't exist.
    """
    p = Path(path)
    if not p.exists():
        _die("Configuration file '{}' not found".format(path))
    return _parse_shell_vars(p.read_text())


def _load_shell_vars_file(search_paths, name):
    for candidate in search_paths:
        p = Path(candidate)
        if p.exists():
            return _parse_shell_vars(p.read_text())
    _die("Configuration file '{}' not found in: {}".format(name, ", ".join(search_paths)))


_VAR_REF_RE = re.compile(r'\$\{(\w+)\}|\$(\w+)')


def _parse_shell_vars(text):
    """
    Parse simple KEY=value or KEY="value" assignments from a bash config file.
    Skips comments, declare statements, arrays, and command substitutions.

    Expands ${VAR}/$VAR references to previously-parsed keys in the same file
    (sequential, like bash `source` — e.g. the real lab_creation.cfg.example
    ships `VIRT_SRV="qemu+ssh://root@${REMOTE_HOST}/system?..."`, which relies
    on REMOTE_HOST already having been assigned earlier in the same file).
    Falls back to the process environment for anything not defined earlier in
    the file, matching a sourced script's actual variable scope. An
    unresolvable reference is left as-is rather than raising.

    Returns a dict.
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if any(line.startswith(kw) for kw in ("declare", "export declare", "typeset")):
            continue
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw.startswith("(") or "$(" in raw or "`" in raw:
            continue
        if raw and raw[0] in ('"', "'"):
            # Quoted value: the value ends at the matching closing quote —
            # anything after that (e.g. a trailing " # comment") is not part
            # of it. A naive "first char == last char" check breaks on lines
            # like `KEY='value' # comment`, since the line's last character
            # is then the comment's, not the closing quote.
            quote = raw[0]
            end = raw.find(quote, 1)
            raw = raw[1:end] if end != -1 else raw[1:]
        else:
            # Unquoted: bash treats " #" (space then hash) as the start of a
            # trailing comment.
            comment_at = raw.find(" #")
            if comment_at != -1:
                raw = raw[:comment_at].rstrip()

        def _expand(m):
            name = m.group(1) or m.group(2)
            if name in result:
                return result[name]
            return os.environ.get(name, m.group(0))

        raw = _VAR_REF_RE.sub(_expand, raw)
        result[key] = raw
    return result


# ── Validation ────────────────────────────────────────────────────────────────

def validate_definition(definition, path):
    """
    Check a loaded lab definition for obvious structural errors.
    Raises SystemExit on fatal problems; prints warnings for non-fatal issues.
    """
    if not definition.get("nodes"):
        _die("'{}' has no 'nodes' section".format(path))

    if "cluster" in definition and "kclusters" not in definition:
        _warn(
            "'{}' uses the legacy single-cluster 'cluster' format. "
            "K8s setup requires the 'kclusters' + per-node 'kcluster' format.".format(path)
        )

    kclusters = definition.get("kclusters", {})
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        ref = node_cfg.get("kcluster", "")
        if ref and ref not in kclusters:
            _die(
                "Node '{}' references kcluster '{}' "
                "which is not defined in 'kclusters'".format(vm_name, ref)
            )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _die(msg):
    print("\033[1;91mERROR:\033[0m {}".format(msg), file=sys.stderr)
    raise SystemExit(1)


def _warn(msg):
    print("\033[1;33mWARNING:\033[0m {}".format(msg), file=sys.stderr)
