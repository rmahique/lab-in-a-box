"""
lab_examples.py — the lab definitions offered as starting points ("example labs").

Two sources, in increasing precedence:
  * shipped:  /usr/share/lab_creation/examples (a checkout: <repo>/examples); LABBUILDER_EXAMPLES_DIR overrides it
  * operator: lab_creation.cfg's EXAMPLE_LABS_DIR (default /etc/lab_creation/examples)

An example lab is a file named *.json, *.yaml or *.yml (optionally followed by .template) in one of those
directories that parses as a lab definition with a "nodes" section. An operator file replaces a shipped one of the
same name. A file that does not parse is left out rather than failing the list, so one broken file cannot take the
others with it.

Read by scripts/setup_lab.py (--list-examples) and by the lab-builder's examples/example actions
(webui/lib/discovery.py). Kept Python 3.6 compatible: the lab-builder CGI runs under the host's own python3.
"""
import os
import re

import apps
import primary

SHIPPED_DIR_ENV = "LABBUILDER_EXAMPLES_DIR"
OPERATOR_DIR_KEY = "EXAMPLE_LABS_DIR"
DEFAULT_OPERATOR_DIR = "/etc/lab_creation/examples"

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SUFFIX_RE = re.compile(r"\.(json|yaml|yml)(\.template)?$")


def shipped_dir() -> str:
    """The directory holding the example labs this installation ships."""
    d = os.environ.get(SHIPPED_DIR_ENV)
    if d:
        return os.path.abspath(d)
    for cand in ("/usr/share/lab_creation/examples", os.path.join(_REPO, "examples")):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.path.join(_REPO, "examples")


def config_values(config: dict = None) -> dict:
    """
    `config` when a caller already has lab_creation.cfg, else that file's values, else {}: a machine where the file
    does not exist (a repository checkout, the static lab-builder build) still lists the shipped examples rather
    than failing, which is what primary.load_config() does on its own.
    """
    if config is not None:
        return config
    return primary.load_config() if primary.config_file_exists() else {}


def operator_dir(config: dict = None) -> str:
    """The directory holding this operator's own example labs: EXAMPLE_LABS_DIR, else /etc/lab_creation/examples."""
    return os.path.abspath(str(config_values(config).get(OPERATOR_DIR_KEY) or DEFAULT_OPERATOR_DIR))


def directories(config: dict = None) -> list:
    """[(source, directory)] in increasing precedence, where source is "shipped" or "operator"."""
    return [("shipped", shipped_dir()), ("operator", operator_dir(config))]


def title(name: str) -> str:
    """A file name as a title: "harvester-aws.json.template" -> "harvester aws"."""
    return _SUFFIX_RE.sub("", name).replace("-", " ").replace("_", " ")


def addon_names(definition: dict) -> list:
    """Every add-on named anywhere in `definition`, sorted, in both the kclusters and the single-cluster format."""
    owners = list((definition.get("kclusters") or {}).values()) + list((definition.get("nodes") or {}).values())
    if isinstance(definition.get("cluster"), dict):        # the older single-Kubernetes-cluster format
        owners.append(definition["cluster"])
    if isinstance(definition.get("addons"), list):         # that format's one shared top-level add-on list
        owners.append({"addons": definition["addons"]})
    names = set()
    for owner in owners:
        for entry in (owner or {}).get("addons") or []:
            names.add(apps.addon_entry_name(entry))
    return sorted(names)


def _entry(name: str, path: str, source: str) -> dict:
    """One catalogue entry for the file `path`, or None when it does not parse as a lab definition."""
    definition, parse_error = primary.try_load_definition(path)
    if parse_error or not isinstance(definition, dict) or "nodes" not in definition:
        return None
    return {
        "name": name,
        "title": title(name),
        "description": (definition.get("common") or {}).get("lab_description", ""),
        "source": source,
        "path": path,
        "nodes": len(definition.get("nodes") or {}),
        # The older format's single "cluster" counts as one, so an entry never reads as having none.
        "kclusters": len(definition.get("kclusters") or {}) or (1 if isinstance(definition.get("cluster"), dict) else 0),
        "addons": addon_names(definition),
    }


def catalogue(config: dict = None) -> list:
    """
    Every example lab, as [{"name", "title", "description", "source", "path", "nodes", "kclusters", "addons"}],
    sorted by name. The counts let a caller say what a file holds without opening it.
    """
    found = {}
    for source, directory in directories(config):
        for name in sorted(os.listdir(directory) if os.path.isdir(directory) else []):
            path = os.path.join(directory, name)
            if not os.path.isfile(path) or not _SUFFIX_RE.search(name):
                continue
            entry = _entry(name, path, source)
            if entry:
                found[name] = entry
    return [found[name] for name in sorted(found)]


def path_of(name: str, config: dict = None) -> str:
    """
    The file of example lab `name`. `name` is a name from catalogue(), never a path: anything with a separator in it,
    or that is not in that list, is refused.
    """
    if not name or os.path.basename(name) != name:
        raise ValueError("invalid example name: %r" % name)
    for entry in catalogue(config):
        if entry["name"] == name:
            return entry["path"]
    raise FileNotFoundError("no such example: %s" % name)


def load(name: str, config: dict = None) -> dict:
    """Example lab `name` as a plain lab definition. Raises ValueError when the file does not parse."""
    path = path_of(name, config)
    definition, parse_error = primary.try_load_definition(path)
    if parse_error:
        raise ValueError("%s does not parse: %s" % (name, parse_error))
    return dict(definition)
