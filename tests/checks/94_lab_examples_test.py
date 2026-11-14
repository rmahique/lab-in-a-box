#!/usr/bin/env python3.11
# The example labs offered as starting points — see 94_lab_examples.sh.
import json
import subprocess
from unittest import mock
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))
os.environ["LABBUILDER_STATUS_FILE"] = os.devnull

import api  # noqa: E402
import discovery  # noqa: E402
import lab_examples  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── the repository's own examples ─────────────────────────────────────────────
items = discovery.examples()
check("the shipped examples are listed", len(items) >= 2)
check("each one is named by its file name and has a title",
      all(e["name"] and e["title"] and "/" not in e["name"] for e in items))
check("the list is sorted by name", [e["name"] for e in items] == sorted(e["name"] for e in items))
check("a title drops the extension and reads as words",
      lab_examples.title("harvester-aws.json.template") == "harvester aws")
check("the shipped examples report where they come from",
      all(e["source"] == "shipped" for e in items))
check("every listed example counts its VMs", all(e["nodes"] > 0 for e in items))
for e in items:
    definition = discovery.example(e["name"])
    check("{} opens as a lab definition with the nodes it counted".format(e["name"]),
          isinstance(definition, dict) and len(definition.get("nodes") or {}) == e["nodes"])

# ── a name is a name, never a path ────────────────────────────────────────────
for bad in ("../../etc/passwd", "/etc/passwd", "sub/dir.json", "", None):
    try:
        discovery.example(bad)
        check("a path is refused as an example name: {!r}".format(bad), False)
    except ValueError:
        pass
    except FileNotFoundError:
        check("a path is refused as an example name: {!r}".format(bad), False)
try:
    discovery.example("not-an-example.json")
    check("an unknown example is refused", False)
except FileNotFoundError:
    pass

# ── what counts as an example ────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
(tmp / "good.json").write_text(json.dumps({
    "common": {"lab_description": "Two VMs and a cluster"},
    "nodes": {"n1": {"addons": ["mariadb"]}, "n2": {}},
    "kclusters": {"c1": {"clu_type": "rke2", "addons": ["rancher", {"longhorn": {"lh_version": "1.9"}}]}},
}))
(tmp / "old-format.json").write_text(json.dumps({
    "nodes": {"n1": {}}, "cluster": {"clu_name": "c1", "addons": ["rancher"]}}))
(tmp / "broken.json").write_text("{not json at all")
(tmp / "no-nodes.json").write_text(json.dumps({"common": {}}))
(tmp / "notes.txt").write_text("not a lab")
(tmp / "adir.json").mkdir()
os.environ[lab_examples.SHIPPED_DIR_ENV] = str(tmp)
try:
    names = [e["name"] for e in discovery.examples()]
    check("a definition with nodes is offered", "good.json" in names)
    check("the older single-cluster format is offered too", "old-format.json" in names)
    check("a file that does not parse is left out, not an error", "broken.json" not in names)
    check("a file without nodes is not a lab", "no-nodes.json" not in names)
    check("a file that is not a definition is ignored", "notes.txt" not in names)
    check("a directory is not an example", "adir.json" not in names)
    good = [e for e in discovery.examples() if e["name"] == "good.json"][0]
    check("its own description is used when it has one", good["description"] == "Two VMs and a cluster")
    check("VMs and Kubernetes clusters are counted", good["nodes"] == 2 and good["kclusters"] == 1)
    check("every add-on it installs is listed, per-node and per-cluster, overrides included",
          good["addons"] == ["longhorn", "mariadb", "rancher"])
    old = [e for e in discovery.examples() if e["name"] == "old-format.json"][0]
    check("the older format's add-ons are listed too", old["addons"] == ["rancher"])
    check("a lab with no add-ons lists none",
          [e for e in discovery.examples() if e["name"] == "no-nodes.json"] == [])

    # ── through the API ──────────────────────────────────────────────────────
    status, body = api.dispatch("examples", "GET", {}, b"")
    check("GET examples answers 200 with the list",
          status == 200 and [e["name"] for e in body["examples"]] == names)
    status, body = api.dispatch("example", "GET", {"name": ["good.json"]}, b"")
    check("GET example answers 200 with the definition", status == 200 and len(body["nodes"]) == 2)
    status, body = api.dispatch("example", "GET", {"name": ["../../etc/passwd"]}, b"")
    check("GET example refuses a path with 400", status == 400 and "invalid example name" in body["error"])
    status, body = api.dispatch("example", "GET", {"name": ["nope.json"]}, b"")
    check("GET example answers 400 for an unknown name", status == 400 and "no such example" in body["error"])
    status, body = api.dispatch("example", "GET", {}, b"")
    check("GET example with no name is refused", status == 400)
    status, _ = api.dispatch("examples", "POST", {}, b"")
    check("examples is a GET-only action", status == 400)
finally:
    del os.environ[lab_examples.SHIPPED_DIR_ENV]

# ── the UI offers them ────────────────────────────────────────────────────────
app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
index_html = (_REPO / "webui" / "htdocs" / "index.html").read_text()
check("the page has an Examples control and its dialog",
      'id="examplesBtn"' in index_html and 'id="examplesDialog"' in index_html)
check("the control is wired to the dialog", '$("#examplesBtn").addEventListener' in app_js)
check("the dialog reads the examples through the API", 'apiGet("examples")' in app_js)
check("opening one loads it onto the canvas", 'loadLab(await apiGet("example"' in app_js)
static = (_REPO / "scripts" / "build-cube-static.py").read_text()
check("the static build embeds the examples and answers for them",
      '"examples": examples' in static and 'action === "examples"' in static)

# ── what the dialog shows, and that it can show it ────────────────────────
# An example's description is a sentence. On the single-line row the examples used to borrow, the name was
# ellipsised away and the Open button squeezed off the row, so the rows are their own two-line layout now.
app = (_REPO / "webui" / "htdocs" / "app.js").read_text()
css = (_REPO / "webui" / "htdocs" / "style.css").read_text()
check("an example row is its own layout, not the one-line saved-lab row",
      'el("li", "example-row")' in app and "example-row" in css)
check("the description wraps under the name rather than beside it",
      'el("p", "example-desc", ex.description)' in app and ".example-desc" in css)
check("the name is allowed to wrap instead of being cut off",
      ".example-row .saved-name" in css and "white-space: normal" in css)
check("the Open button keeps its place whatever the text does", ".example-row .btn" in css)
check("a long description is clamped rather than filling the dialog", "-webkit-line-clamp" in css)

# The older format keeps its cluster under "cluster" and one shared add-on list at the top level, so an example in
# that format used to be listed as having no cluster and no add-ons at all.
old_format = {"nodes": {"n1": {}, "n2": {}}, "cluster": {"clu_name": "c1", "clu_type": "rke2"},
              "addons": ["rancher", "longhorn"], "common": {}}
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, "old-format.json").write_text(json.dumps(old_format))
    with mock.patch.dict(os.environ, {"LABBUILDER_EXAMPLES_DIR": tmp}):
        listed = discovery.examples()
check("an older-format example is listed with its cluster and its add-ons",
      len(listed) == 1 and listed[0]["kclusters"] == 1
      and listed[0]["addons"] == ["longhorn", "rancher"] and listed[0]["nodes"] == 2)

# ── the operator's own directory, beside the shipped one ──────────────────────
# EXAMPLE_LABS_DIR is a lab_creation.cfg key, not an environment variable, so it is passed as the config a caller
# already holds — the same way discovery.py and setup_lab.py get it.
shipped = Path(tempfile.mkdtemp())
own = Path(tempfile.mkdtemp())
(shipped / "shipped-one.json").write_text(json.dumps({"nodes": {"n1": {}}, "common": {"lab_description": "ships"}}))
(shipped / "both.json").write_text(json.dumps({"nodes": {"n1": {}}, "common": {"lab_description": "ships"}}))
(own / "both.json").write_text(json.dumps({"nodes": {"n1": {}, "n2": {}}, "common": {"lab_description": "mine"}}))
(own / "mine.json").write_text(json.dumps({"nodes": {"n1": {}}, "common": {"lab_description": "mine"}}))
(own / "broken.json").write_text("{not json at all")
cfg = {"EXAMPLE_LABS_DIR": str(own)}
with mock.patch.dict(os.environ, {lab_examples.SHIPPED_DIR_ENV: str(shipped)}):
    listed = lab_examples.catalogue(cfg)
    by_name = {e["name"]: e for e in listed}
    check("both directories are listed together",
          sorted(by_name) == ["both.json", "mine.json", "shipped-one.json"])
    check("an operator lab says it is the operator's own", by_name["mine.json"]["source"] == "operator")
    check("a shipped lab still says it is shipped", by_name["shipped-one.json"]["source"] == "shipped")
    check("an operator file of the same name replaces the shipped one",
          by_name["both.json"]["source"] == "operator" and by_name["both.json"]["nodes"] == 2
          and by_name["both.json"]["description"] == "mine")
    check("a broken operator file is left out, not an error", "broken.json" not in by_name)
    check("an entry carries the file it was read from",
          by_name["mine.json"]["path"] == str(own / "mine.json"))
    check("the operator's lab opens from the operator's directory",
          len(lab_examples.load("mine.json", cfg).get("nodes") or {}) == 1)
    check("the shadowed name opens the operator's file",
          len(lab_examples.load("both.json", cfg).get("nodes") or {}) == 2)
    check("both directories are reported, shipped first",
          [d for _s, d in lab_examples.directories(cfg)] == [str(shipped), str(own)])
    check("without EXAMPLE_LABS_DIR the operator directory is /etc/lab_creation/examples",
          lab_examples.operator_dir({}) == "/etc/lab_creation/examples")
    # The file each example came from is the server's business: the browser addresses one by name.
    check("the API leaves the file path out", all("path" not in e for e in discovery.examples()))

# ── setup_lab.py --list-examples ──────────────────────────────────────────────
setup_lab = _REPO / "scripts" / "setup_lab.py"
env = dict(os.environ, LABBUILDER_EXAMPLES_DIR=str(shipped), PYTHONPATH=str(_REPO / "libs"))
out = subprocess.run([sys.executable, str(setup_lab), "--list-examples", "--json"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
check("--list-examples --json exits 0", out.returncode == 0)
try:
    listed_json = json.loads(out.stdout.decode())
except ValueError:
    listed_json = []
    check("--list-examples --json prints JSON", False)
check("--list-examples --json lists the same examples the library does",
      [e["name"] for e in listed_json] == ["both.json", "shipped-one.json"])
out = subprocess.run([sys.executable, str(setup_lab), "--list-examples"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
text = out.stdout.decode()
check("--list-examples prints each example with its source and its description",
      out.returncode == 0 and "shipped-one.json" in text and "[shipped]" in text and "ships" in text)
check("--list-examples is documented in the help",
      "--list-examples" in subprocess.run([sys.executable, str(setup_lab), "--help"],
                                          stdout=subprocess.PIPE, env=env).stdout.decode())

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab-example checks passed")
