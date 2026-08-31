#!/usr/bin/env python3
"""
tests/schema_consistency_check.py — fails if scripts/lab_schema's `enum`
lists drift out of sync with the actual Python registries they describe.

Why this exists: a schema `enum` list (used by the web UI to render a
dropdown) is hand-maintained prose next to code that defines the real set of
valid values (e.g. libs/backends.py's BACKENDS dict). It's
easy to add a new backend/distro there and forget the schema never mentions
it — this check makes that a hard test failure instead of a silent drift.

Add a new (schema field) <-> (Python registry) pair here whenever a new enum
is introduced that mirrors a real code-level registry.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "libs"))


def load_module(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def find_field(schema, section, field_name):
    fields = schema["sections"][section]["fields"]
    for f in fields:
        if f["name"] == field_name:
            return f
    raise AssertionError("field '{}' not found in section '{}'".format(field_name, section))


def enum_values(field):
    return {(o["value"] if isinstance(o, dict) else o) for o in field.get("enum", [])}


def main():
    lab_schema = load_module("lab_schema", REPO_ROOT / "scripts" / "lab_schema")
    backends = load_module("backends", REPO_ROOT / "libs" / "backends.py")
    k8s = load_module("k8s", REPO_ROOT / "libs" / "k8s.py")

    schema = lab_schema.base_lab_schema()
    failures = []

    checks = [
        ("common.backend", find_field(schema, "common", "backend"), set(backends.BACKENDS)),
        ("nodes.backend", find_field(schema, "nodes", "backend"), set(backends.BACKENDS)),
        ("kclusters.clu_type", find_field(schema, "kclusters", "clu_type"), set(k8s.DISTROS)),
    ]

    for label, field, registry_keys in checks:
        schema_values = enum_values(field)
        if schema_values != registry_keys:
            failures.append(
                "{}: schema enum {} != registry keys {}".format(
                    label, sorted(schema_values), sorted(registry_keys)))

    if failures:
        print("FAIL: schema enum(s) out of sync with their Python registry:")
        for f in failures:
            print("  " + f)
        sys.exit(1)

    print("ok: schema enums match their Python registries ({} checked)".format(len(checks)))


if __name__ == "__main__":
    main()
