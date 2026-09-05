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

class LabDefinition(dict):
    """
    A loaded lab definition. Behaves as a plain dict for every existing
    read access (.get()/["..."]/.items()/json.dumps()/isinstance(x, dict) —
    every one of the ~40 addons and every library function that reads a
    definition keeps working completely unchanged), but also carries where
    it came from and in what format, as plain instance attributes:

        .source_path  — the file path it was loaded from
        .fmt          — "json" or "yaml", whichever was actually used

    These are attributes, not dict keys, so they never show up in
    .items()/.keys()/iteration or get serialized by json.dumps()/
    yaml.safe_dump() — the dict's own content is always exactly what was
    on disk, nothing extra riding along in it.

    The point: anything holding a `definition` already has everything it
    needs to save a change back later (see save_definition() below) without
    a separate path/format argument threaded through every function in the
    call chain — logic that mutates a value (e.g. backends.py's MAC-
    conflict resolution) shouldn't need to know or care where the file
    lives or what format it's in; that's this object's job, not theirs.
    """

    def __init__(self, data, source_path, fmt):
        super().__init__(data)
        self.source_path = source_path
        self.fmt = fmt


def load_definition(path):
    """
    Load a lab definition from a JSON or YAML file. Dies (SystemExit) on any
    parse failure — for the graceful, non-dying equivalent used by preflight/
    --validate paths that need to fold a parse failure into their own issue
    list, see try_load_definition() below (this function is a thin wrapper
    around it).

    Returns a LabDefinition (see above). YAML input requires pyyaml
    (pip install pyyaml). Falls back to YAML parsing if the file is not
    valid JSON.
    """
    definition, error = try_load_definition(path)
    if error:
        _die(error)
    return definition


def try_load_definition(path):
    """
    Format-detecting lab-definition parse that never dies: returns
    (definition, error) where exactly one of the two is None/empty.
    `definition`, when present, is a LabDefinition (see above) — it already
    knows its own source path and format, so nothing downstream needs to
    re-derive or re-pass either.

    Detection mirrors load_definition(): a .yaml/.yml extension parses as
    YAML directly; anything else is tried as JSON first, falling back to
    YAML if that fails (so an extensionless or oddly-named file still works
    either way). YAML parsing (including the fallback) requires pyyaml.
    """
    p = Path(path)
    if not p.exists():
        return None, "Lab definition file '{}' not found".format(path)

    try:
        text = p.read_text()
    except OSError as e:
        return None, "could not read '{}': {}".format(path, e)

    is_yaml_ext = p.suffix.lower() in (".yaml", ".yml")

    json_error = None
    if not is_yaml_ext:
        try:
            return LabDefinition(json.loads(text), path, "json"), None
        except json.JSONDecodeError as e:
            json_error = e

    try:
        import yaml
    except ImportError:
        return None, (
            "PyYAML is required to parse '{}' as YAML.\n"
            "Install it with:  pip install pyyaml".format(path)
        )

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        if json_error is not None:
            return None, (
                "'{}' is not valid JSON ({}) or YAML ({})".format(path, json_error, e)
            )
        return None, "YAML syntax error in '{}': {}".format(path, e)

    if not isinstance(data, dict):
        return None, "'{}' does not contain a mapping at the top level".format(path)

    return LabDefinition(data, path, "yaml"), None


def save_definition(definition):
    """
    Persist a change made to an in-memory `definition` (a LabDefinition, or
    any dict-like object carrying .source_path/.fmt attributes in the same
    shape). Returns the path actually written.

    Deliberately does NOT overwrite .source_path itself: this project's
    lab definitions are sometimes hand-edited (comments, specific
    formatting), and a plain json.dumps()/yaml.safe_dump() round-trip would
    silently discard all of that. Instead, writes to
    "<source_path>.system_modified.<fmt>" — a clearly-named sibling file the
    operator can review and merge back manually. The original file is never
    touched.

    This is the ONLY place a lab-definition-mutating change gets written to
    disk — a caller that needs to persist something (e.g. backends.py's
    MAC-conflict resolution, generating a new MAC) mutates the in-memory
    `definition` directly and calls this; there is no re-reading of the
    source file anywhere in that path, ever — the in-memory `definition` IS
    the current, authoritative state.

    Dies (SystemExit) if pyyaml is required and missing, same as
    load_definition().
    """
    path = definition.source_path
    fmt = definition.fmt
    output_path = "{}.system_modified.{}".format(path, fmt)

    if fmt == "yaml":
        try:
            import yaml
        except ImportError:
            _die(
                "PyYAML is required to write '{}' as YAML.\n"
                "Install it with:  pip install pyyaml".format(output_path)
            )
        Path(output_path).write_text(yaml.safe_dump(dict(definition), sort_keys=False))
    else:
        Path(output_path).write_text(json.dumps(dict(definition), indent=2))

    return output_path


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
