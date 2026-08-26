# lab-builder — dynamic web UI for lab-in-a-box

A web interface that builds lab definitions by **introspecting the project's own
libraries at run time**. It lists every `install_*` component, and when you pick
one it renders a form from that component's `--schema` output. Add a new
component, or a new field to an existing one, and the UI picks it up
automatically — there is no per-component code in the UI.

## Design

Thin web layer over the existing Python libraries — **no subprocess fan-out**:

```
htdocs/            single-page app (vanilla HTML/CSS/JS)
 └─ app.js         generic schema walker: fields recognised by shape (name+type),
                   `fields`/`sections` treated as structural wrappers
lib/discovery.py   imports scripts/lab_schema (parse_script) and libs/primary
                   (validate_definition) in-process
lib/api.py         transport-agnostic request dispatch (one place)
cgi-bin/labbuilder.py   Apache CGI shim
run-local.py       zero-dependency dev server (no Apache needed)
apache/lab-builder.conf Apache drop-in
```

The only fixed convention is the schema vocabulary: a **field** is any object
with `name` + `type`; `fields`/`sections` are structural; a section may carry
`repeatable`. Everything else is discovered.

## Run locally (any machine with Python 3)

```bash
python3 webui/run-local.py            # http://localhost:8677/
```

It auto-detects the scripts and libs directories:
`/usr/local/bin` + `/usr/local/lib/lab_creation` if installed, otherwise the
repo's `scripts/` and `libs/`.

## Deploy on the automation VM (Apache)

```bash
cp -r webui /srv/www/lab-builder
mkdir -p /srv/www/lab-builder/labs && chown wwwrun /srv/www/lab-builder/labs
cp webui/apache/lab-builder.conf /etc/apache2/vhosts.d/
# ensure 'cgi' is enabled, then:
systemctl reload apache2
```

Browse to `http://<automation-vm>/lab-builder/`.

## Configuration (env vars, all optional)

| var | meaning | default |
|-----|---------|---------|
| `LABBUILDER_SCRIPTS_DIR` | dir with `install_*` + `lab_schema` | auto |
| `LABBUILDER_LIBS_DIR`    | dir with the python `libs` package | auto |
| `LABBUILDER_OUTPUT_DIR`  | where generated labs are written | `~/.lab-builder/labs` |

## Endpoints

| method | path | purpose |
|--------|------|---------|
| GET  | `api?action=components`     | list components + live count |
| GET  | `api?action=schema&name=install_longhorn` | one component's schema |
| POST | `api?action=validate`       | validate a lab via `libs/primary` |
| POST | `api?action=save`           | write `lab.json` to the output dir |

## Scope

Builds a **complete lab.json**:

- **Base topology** — the pinned *▚ Lab topology* entry renders `common`
  (singleton) plus `nodes` and `kclusters` as **repeatable** keyed maps
  (add/remove instances). Its schema is the single source of truth in
  `lab_schema.base_lab_schema()`, which `setup_lab.sh --schema` also emits — so
  there is one definition, consumed in-process here (no subprocess).
- **Addon sections** — every `install_*` component (e.g. `longhorn: {…}`,
  `smlm: {…}`), rendered from its own `--schema`.

Possible next increment (deliberately not built): **execution** — running a
saved lab via `setup_lab.py`'s phase functions as a library.

## Deploy note

The base-topology feature needs the updated `lab_schema` / `setup_lab.sh` on the
automation VM. Redeploy the installed scripts the normal way
(`install_automation_node_scripts.sh`) so `/usr/local/bin/lab_schema` gains
`--base`; the web app auto-detects that installed copy.
