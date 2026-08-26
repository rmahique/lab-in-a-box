"""
addon_common.py — shared CLI scaffolding for install_<addon>.py scripts.

Every install_<addon> script (bash and now python) follows the same pattern:
--version/-v, --validate <json>, --help (prints the script's own "JSON
section:" doc comment), and --input-definition/--schema (delegates to
lab_schema, which parses that same comment block — language-agnostic, since
it just reads "#"-prefixed lines, so it works unchanged on these .py ports).
Extracted here once instead of duplicated in all 40 scripts, mirroring how
the bash versions duplicate ~30 lines of identical _vreq/_vns/_vver/_vurl/
_vbool/_vport function bodies in every single script.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import json
import re
import subprocess
import sys
from pathlib import Path


# ── Field validators ──────────────────────────────────────────────────────────
# Mirror bash's _vreq/_vns/_vver/_vurl/_vbool/_vport exactly (same jq `// ""`
# semantics via .get(key, ""), same regexes, same [ERROR] message text).

class Validator:
    """
    Accumulates [ERROR] lines against a loaded definition dict, mirroring the
    bash _vf/_ve/_vreq/_vns/_vver/_vurl/_vbool/_vport closures.

    errors : list of "[ERROR] ..." strings printed by run_validate().
    """

    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def _get(self, section, field):
        return (self.definition.get(section, {}) or {}).get(field, "") or ""

    def vreq(self, section, field):
        """Mirrors _vreq: <section>.<field> is required (no default)."""
        if not self._get(section, field):
            self.errors.append("[ERROR] {}.{} is required (no default)".format(section, field))

    def vns(self, section):
        """Mirrors _vns: <section>.<section>_ns, if set, must be a valid k8s namespace."""
        v = self._get(section, "{}_ns".format(section))
        if v and not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', str(v)):
            self.errors.append(
                "[ERROR] {0}.{0}_ns='{1}': invalid namespace (lowercase, alphanumeric, hyphens only)".format(
                    section, v))

    def vver(self, section):
        """Mirrors _vver: <section>.<section>_version, if set, must look like X.Y…"""
        v = self._get(section, "{}_version".format(section))
        if v and not re.match(r'^[0-9]+\.[0-9]', str(v)):
            self.errors.append(
                "[ERROR] {0}.{0}_version='{1}': not a valid version (expected X.Y…)".format(section, v))

    def vurl(self, section):
        """Mirrors _vurl: <section>.<section>_repo_url, if set, must start with http(s)://"""
        v = self._get(section, "{}_repo_url".format(section))
        if v and not re.match(r'^https?://', str(v)):
            self.errors.append(
                "[ERROR] {0}.{0}_repo_url='{1}': must start with https://".format(section, v))

    def vbool(self, section, field):
        """Mirrors _vbool: <section>.<field>, if set, must be 'true' or 'false'."""
        v = self._get(section, field)
        if v and str(v) not in ("true", "false"):
            self.errors.append("[ERROR] {}.{}='{}': must be 'true' or 'false'".format(section, field, v))

    def vport(self, section, field):
        """Mirrors _vport: <section>.<field>, if set, must be a bare integer."""
        v = self._get(section, field)
        if v and not re.match(r'^[0-9]+$', str(v)):
            self.errors.append("[ERROR] {}.{}='{}': must be a port number".format(section, field, v))


def run_validate(json_path, check_fn):
    """
    Load json_path, run check_fn(Validator) to populate errors, print them,
    and return the error count as an exit code — mirrors bash's `exit ${_ve}`.
    """
    try:
        definition = json.loads(Path(json_path).read_text())
    except (OSError, ValueError) as e:
        print("[ERROR] could not read/parse '{}': {}".format(json_path, e))
        return 1
    v = Validator(definition)
    check_fn(v)
    for line in v.errors:
        print(line)
    return len(v.errors)


# ── --help (self-documenting from the script's own header comment) ──────────

def print_help(script_path, usage=None):
    """
    Mirrors bash's --help: prints a usage line, then re-emits this script's
    own "# JSON section: ..." comment block verbatim (same block lab_schema
    parses for --input-definition/--schema).
    """
    name = Path(script_path).name
    print(usage or "Usage: {} <lab.json> [<vm_name>]".format(name))
    print()
    text = Path(script_path).read_text(errors="replace")
    in_block = False
    for line in text.splitlines():
        if not in_block:
            if line.lstrip().startswith("#") and "JSON section:" in line:
                in_block = True
            else:
                continue
        elif not line.lstrip().startswith("#"):
            break
        # The line that triggers in_block is deliberately printed too (falls
        # through here rather than `continue`), matching bash's awk rule
        # ordering exactly (`/pattern/{p=1}` doesn't skip the rest of that
        # line's rules, including the final `p{print}`).
        print(re.sub(r'^#\s?', '', line))


def print_schema(script_path, fmt):
    """Mirrors bash's --input-definition/--schema: delegates to lab_schema."""
    return subprocess.run(["lab_schema", script_path, fmt]).returncode


# ── Top-level dispatch ────────────────────────────────────────────────────────

def handle_common_args(script_path, version, validate_fn=None, usage=None):
    """
    Handle --version/-v, --validate <json>, --help, --input-definition/
    --schema if present as sys.argv[1] (mirrors the identical dispatch block
    at the top of every install_<addon> script). Exits the process if one of
    these matched; otherwise returns None so the caller proceeds normally.

    validate_fn : callable(Validator) used for --validate; if None, --validate
                  always exits 0 (mirrors a script whose block is just
                  `exit ${_ve}` with no checks — several addons have exactly
                  this, e.g. install_uyuni, install_suma, install_wordpress).
    """
    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-v"):
        print("{} {}".format(Path(script_path).name, version))
        sys.exit(0)

    if argv and argv[0] == "--validate":
        if len(argv) < 2:
            print("[ERROR] --validate requires a JSON file path")
            sys.exit(1)
        if validate_fn is None:
            sys.exit(0)
        sys.exit(run_validate(argv[1], validate_fn))

    if argv and argv[0] == "--help":
        print_help(script_path, usage=usage)
        sys.exit(0)

    if argv and argv[0] in ("--input-definition", "--schema"):
        fmt = argv[1] if len(argv) > 1 else "json"
        sys.exit(print_schema(script_path, fmt))
