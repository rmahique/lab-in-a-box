"""
lab-builder — runtime introspection of lab-in-a-box, driven by the project's
own Python libraries (no subprocess fan-out to shell scripts).

It imports, in-process:
  * scripts/lab_schema  -> parse_script()      (schema of each addon definition)
  * libs/primary        -> validate_definition (structural lab validation)

It holds NO knowledge of any specific addon or field: components and their
fields are discovered at run time from the definitions themselves, so adding a
new install_* definition (or a field to one) appears automatically.

Configuration (all optional, env-overridable):
  LABBUILDER_SCRIPTS_DIR   dir holding install_* definitions + lab_schema
                           (auto: /usr/local/bin, else <repo>/scripts)
  LABBUILDER_LIBS_DIR      dir holding the python libs package
                           (auto: /usr/local/lib/lab_creation, else <repo>/libs)
  LABBUILDER_OUTPUT_DIR    where generated labs are written (~/.lab-builder/labs)
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from importlib.machinery import SourceFileLoader

# Only definitions with these name shapes are ever introspected.
SCRIPT_RE = re.compile(r'^install_[A-Za-z0-9._-]+$')
_ANSI = re.compile(r'\x1b\[[0-9;]*m')

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))


# ── configuration / path resolution ───────────────────────────────────────────

def scripts_dir():
    d = os.environ.get("LABBUILDER_SCRIPTS_DIR")
    if d:
        return os.path.abspath(d)
    for cand in ("/usr/local/bin", os.path.join(_REPO, "scripts")):
        if os.path.isfile(os.path.join(cand, "lab_schema")):
            return os.path.abspath(cand)
    return "/usr/local/bin"


def libs_dir():
    d = os.environ.get("LABBUILDER_LIBS_DIR")
    if d:
        return os.path.abspath(d)
    for cand in ("/usr/local/lib/lab_creation", os.path.join(_REPO, "libs")):
        if os.path.isfile(os.path.join(cand, "primary.py")):
            return os.path.abspath(cand)
    return os.path.join(_REPO, "libs")


def output_dir():
    d = os.environ.get("LABBUILDER_OUTPUT_DIR") or os.path.expanduser("~/.lab-builder/labs")
    os.makedirs(d, exist_ok=True)
    return d


# ── lazy, cached in-process imports of the project libraries ───────────────────

_lab_schema = None
_primary = None


def _schema_lib():
    global _lab_schema
    if _lab_schema is None:
        path = os.path.join(scripts_dir(), "lab_schema")
        loader = SourceFileLoader("lab_schema", path)
        spec = importlib.util.spec_from_loader("lab_schema", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _lab_schema = mod
    return _lab_schema


def _primary_lib():
    global _primary
    if _primary is None:
        d = libs_dir()
        if d not in sys.path:
            sys.path.insert(0, d)
        import primary  # noqa: E402  (loaded from libs_dir)
        _primary = primary
    return _primary


# ── introspection API (all in-process) ────────────────────────────────────────

def _def_path(name):
    if not SCRIPT_RE.match(name or ""):
        raise ValueError("invalid component name: %r" % name)
    p = os.path.join(scripts_dir(), name)
    if not os.path.isfile(p):
        raise FileNotFoundError(name)
    return p


def schema(name):
    """Schema of a single addon definition, via the lab_schema library."""
    return _schema_lib().parse_script(_def_path(name))


def base_schema():
    """The base lab-definition schema (common/nodes/kclusters), via the library."""
    return _schema_lib().base_lab_schema()


def discover():
    """Every install_* definition that yields a non-empty schema section."""
    parse = _schema_lib().parse_script
    d = scripts_dir()
    items = []
    for name in sorted(os.listdir(d)):
        if not SCRIPT_RE.match(name):
            continue
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            sc = parse(p)
        except Exception:
            continue
        if not sc.get("section") and not sc.get("fields"):
            continue
        items.append({
            "name": name,
            "kind": "addon",
            "title": sc.get("section") or name,
            "description": sc.get("description", ""),
            "field_count": len(sc.get("fields", [])),
        })
    return items


def validate_lab(definition):
    """Validate a full lab definition via libs/primary (captures its stderr)."""
    buf = io.StringIO()
    ok = True
    try:
        with contextlib.redirect_stderr(buf):
            _primary_lib().validate_definition(definition, "lab.json")
    except SystemExit:
        ok = False
    return {"ok": ok, "output": _ANSI.sub("", buf.getvalue()).strip()}


def _safe_name(filename):
    base = os.path.basename(filename or "")
    if not re.match(r'^[A-Za-z0-9._-]+$', base):
        raise ValueError("invalid filename")
    return base if base.endswith(".json") else base + ".json"


def save_lab(filename, config):
    path = os.path.join(output_dir(), _safe_name(filename))
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    return path
