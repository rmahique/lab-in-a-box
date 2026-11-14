#!/usr/bin/env python3.11
"""
lab-builder-helper — the actions of the lab-builder web UI that need root.

The web UI runs as the web server's user; it runs this helper through a sudoers
rule that allows only this file (or directly, when the UI itself runs as root).
Every command prints one JSON object on stdout. Secrets are read as one JSON
object on stdin, never from the command line. On a refused request the output
is {"error": "..."} and the exit status is 1.

Usage: lab-builder-helper <command> [argument]

  credentials-list            the stored credential files (names, kinds and
                              field names, never values) and the fields each
                              cloud provider and service kind takes
  credentials-add             write a new credential file; stdin:
                              {"kind": "cloud"|"service", "type": "<provider or
                              service kind>", "account": "<name>", "fields":
                              {...}, "passphrase": "<passphrase>" or null for a
                              plaintext file}
  credentials-encrypt FILE    write FILE's ".encrypted.yaml" copy with its
                              secret-looking fields encrypted; stdin:
                              {"passphrase": "..."}
  credentials-delete FILE     delete credential file FILE
  lab-create FILE [--keep] [--cloud-account NAME]
  lab-create --example NAME [--keep] [--cloud-account NAME]
                              start setup_lab.py on saved lab FILE, or on
                              example lab NAME (libs/lab_examples.py, which
                              setup_lab.py --example materialises for the run
                              and removes afterwards), in the
                              background (--keep: keep VMs that already exist;
                              --cloud-account: the cloud account this run uses,
                              outranking the one the definition names);
                              prints the job id, a public live-status page URL
                              (job id + a random token, needing no login — see
                              pub-status below) and that page's QR code as SVG
                              markup (qrencode not installed: both are left
                              out, nothing else changes). A JSON stdin
                              {"passphrase": "..."} is passed to the job's own
                              environment (LAB_CREDENTIALS_PASSPHRASE) so a lab
                              with encrypted credentials can be created
                              without a terminal; it is never logged or stored
  lab-validate                run setup_lab.py's preflight as root on a lab
                              definition, its cloud accounts read and
                              decrypted; stdin: {"config": {...lab...},
                              "passphrase": "..." or absent}. Answers
                              {"ok", "output"}, or {"need_passphrase": true}
                              when an account is encrypted and no passphrase
                              was given, or {"wrong_passphrase": true} after
                              a WRONG_PASSPHRASE_DELAY-second wait when the
                              passphrase does not decrypt it
  lab-power {status|start|stop} {FILE|--example NAME} [--cloud-account NAME]
                              run vm_power.py on a saved lab or an example lab:
                              the power state of its VMs, or start/stop them in
                              the lab's own order. Answers {"action", "lab",
                              "result": {vm: state}, "note"}; "note" is what
                              each backend says stopping does to the bill, which
                              on some providers is nothing. Synchronous, unlike
                              lab-create: nothing is provisioned, and the answer
                              is the states themselves
  lab-status JOB              a job's state, exit code and the end of its log
  lab-jobs                    every job, newest first
  pub-status JOB              a job's state and log for whoever holds its own
                              token; stdin: {"token": "..."}. Deliberately
                              answers the same generic refusal whether JOB or
                              the token is wrong, so a guess learns nothing.
                              This is the one command the webui exposes with
                              no login (see webui/lib/api.py's "pub-status"),
                              since all it hands back is what the lab's own
                              random-token link is for.

Paths (environment overrides, for tests): LABBUILDER_LABS_DIR (saved labs,
/srv/www/lab-builder/labs), LABBUILDER_JOBS_DIR (job logs,
/var/lib/lab-builder/jobs), LABBUILDER_SETUP_LAB (/usr/local/bin/setup_lab.py),
LABBUILDER_VM_POWER (/usr/local/bin/vm_power.py),
CREDENTIALS_PATH in /etc/lab_creation.cfg (credential files).
"""

__version__ = "__LABVERSION__"

import contextlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
for _candidate in ("/usr/local/bin", str(Path(__file__).resolve().parent)):
    if Path(_candidate, "setup_credentials.py").is_file() and _candidate not in sys.path:
        sys.path.append(_candidate)

import crypto_store  # noqa: E402
import lab_examples  # noqa: E402
import primary  # noqa: E402
import qr_util  # noqa: E402
import setup_credentials  # noqa: E402

LABS_DIR = Path(os.environ.get("LABBUILDER_LABS_DIR", "/srv/www/lab-builder/labs"))
JOBS_DIR = Path(os.environ.get("LABBUILDER_JOBS_DIR", "/var/lib/lab-builder/jobs"))
SETUP_LAB = os.environ.get("LABBUILDER_SETUP_LAB", "/usr/local/bin/setup_lab.py")
VM_POWER = os.environ.get("LABBUILDER_VM_POWER", "/usr/local/bin/vm_power.py")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CREDENTIAL_EXTS = (".yaml", ".yml", ".json", ".cfg")
LOG_TAIL_LINES = 400
# Seconds lab-validate waits before answering that a passphrase is wrong, so passphrases cannot be tried quickly.
WRONG_PASSPHRASE_DELAY = 3
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Refused(Exception):
    """A request this helper does not carry out; its message goes back to the user."""


def safe_name(name: str, what: str) -> str:
    """`name` when it is a plain file name (no directory, no leading dot); Refused otherwise."""
    if not isinstance(name, str) or not NAME_RE.match(name) or ".." in name:
        raise Refused("invalid {} name {!r}".format(what, name))
    return name


def read_stdin() -> dict:
    """The JSON object on stdin, {} when stdin is empty."""
    text = sys.stdin.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise Refused("stdin must hold a JSON object")
    return data


def credentials_dir() -> Path:
    """The directory new credential files go to (the first of primary.credentials_dirs())."""
    config = primary.load_config() if Path("/etc/lab_creation.cfg").exists() else {}
    return Path(primary.credentials_dirs(config)[0])


def load_credential(path: Path) -> dict:
    """Credential file `path` as a mapping; {} when it does not parse."""
    import yaml

    try:
        text = path.read_text()
        data = json.loads(text) if path.suffix == ".json" else (
            yaml.safe_load(text) if path.suffix in (".yaml", ".yml") else primary._parse_shell_vars(text))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def credential_entry(path: Path) -> dict:
    """What credentials-list shows for one file: never a value, only names and flags."""
    data = load_credential(path)
    lower = {k.lower(): v for k, v in data.items()}
    cloudtype = str(lower.get("cloudtype") or lower.get("cloud_type") or "")
    kind = str(lower.get("credential_kind") or lower.get("kind") or "")
    markers = {"cloudtype", "cloud_type", "credential_kind", "kind", "unencrypted"}
    whole = data.get("encrypted") is True
    fields = [] if whole else [k for k in data if k.lower() not in markers]
    return {
        "file": path.name,
        "name": path.stem,
        "kind": "cloud" if cloudtype else ("service" if kind else "unknown"),
        "type": cloudtype or kind,
        "fields": fields,
        "encrypted": whole,
        "encrypted_fields": [k for k in fields if isinstance(data[k], dict) and data[k].get("encrypted") is True],
        "plaintext_secrets": [] if whole else setup_credentials.sensitive_plaintext_fields(data),
    }


def credential_file(name: str) -> Path:
    """Existing credential file `name` in the credentials directory; Refused otherwise."""
    path = credentials_dir() / safe_name(name, "credential file")
    if path.suffix not in CREDENTIAL_EXTS or not path.is_file():
        raise Refused("no credential file {}".format(name))
    return path


def registry(kind: str) -> dict:
    """The field table for "cloud" (providers) or "service" (service kinds)."""
    if kind == "cloud":
        return setup_credentials.PROVIDER_FIELDS
    if kind == "service":
        return setup_credentials.SERVICE_CREDENTIAL_FIELDS
    raise Refused("kind must be cloud or service")


def credentials_list() -> dict:
    """Every credential file, plus the fields each provider and service kind takes."""
    d = credentials_dir()
    files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix in CREDENTIAL_EXTS) if d.is_dir() else []
    table = lambda reg: {k: [{"name": n, "required": r, "secret": s} for n, r, s in v] for k, v in reg.items()}
    return {
        "dir": str(d),
        "credentials": [credential_entry(p) for p in files],
        "cloud": table(setup_credentials.PROVIDER_FIELDS),
        "service": table(setup_credentials.SERVICE_CREDENTIAL_FIELDS),
    }


def credentials_add(req: dict) -> dict:
    """Write <type>-<account>.yaml from request `req` (see the module docstring)."""
    kind = req.get("kind")
    table = registry(kind)
    ctype = req.get("type")
    if ctype not in table:
        raise Refused("unknown {} type {!r}".format(kind, ctype))
    account = safe_name(req.get("account", ""), "account")
    known = {n: r for n, r, _ in table[ctype]}
    given = {k: str(v) for k, v in (req.get("fields") or {}).items() if str(v) != ""}
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise Refused("{} takes no field {}".format(ctype, ", ".join(unknown)))
    missing = [n for n, r in known.items() if r and n not in given]
    if missing:
        raise Refused("missing required field {}".format(", ".join(missing)))
    passphrase = req.get("passphrase")
    if passphrase is not None and len(passphrase) < 8:
        raise Refused("the passphrase must have at least 8 characters")
    d = credentials_dir()
    stem = "{}-{}".format(ctype, account)
    if any((d / (stem + ext)).exists() for ext in CREDENTIAL_EXTS):
        raise Refused("a credential file {} already exists".format(stem))
    write = setup_credentials.write_account_file if kind == "cloud" else setup_credentials.write_credential_file
    path = write(d / (stem + ".yaml"), ctype, given, passphrase is not None, passphrase=passphrase)
    return {"file": path.name}


def credentials_encrypt(name: str, req: dict) -> dict:
    """Write the ".encrypted.yaml" copy of credential file `name`."""
    path = credential_file(name)
    passphrase = req.get("passphrase") or ""
    if len(passphrase) < 8:
        raise Refused("the passphrase must have at least 8 characters")
    data = load_credential(path)
    keys = setup_credentials.sensitive_plaintext_fields(data)
    if not keys:
        raise Refused("{} has no plaintext secret to encrypt".format(name))
    out = path.parent / "{}.encrypted.yaml".format(path.stem)
    if out.exists():
        raise Refused("{} already exists".format(out.name))
    return {"file": setup_credentials.write_encrypted_copy(path, data, keys, passphrase).name, "encrypted": keys}


def credentials_delete(name: str) -> dict:
    """Delete credential file `name`."""
    path = credential_file(name)
    path.unlink()
    return {"deleted": path.name}


def job_dir(job: str) -> Path:
    """The directory of job `job`; Refused when there is none."""
    d = JOBS_DIR / safe_name(job, "job")
    if not (d / "job.json").is_file():
        raise Refused("no job {}".format(job))
    return d


def job_state(d: Path) -> dict:
    """
    Job directory `d` as {"job", "lab", "started", "state", "rc", ...}; state is running, done or
    failed. Includes "status_url" (rebuilt from job.json's own token) whenever that token exists, so an
    admin reopening the Create dialog for an already-running job (admin.py's login-gated "job"/"jobs"
    actions call this) can show the same link/QR lab_create() showed the first time, not just on the one
    original call. This is safe specifically because only an already-authenticated admin reaches this —
    pub_status() (no login) strips both "status_url" and "token" from its own answer before returning,
    since an unauthenticated caller supplied that exact token to get here and gains nothing from having
    it echoed back, and should not implicitly gain the ability to read it off some OTHER job's state.
    """
    meta = json.loads((d / "job.json").read_text())
    rc_file = d / "rc"
    rc = int(rc_file.read_text().strip()) if rc_file.is_file() and rc_file.read_text().strip() else None
    if rc is None:
        alive = Path("/proc", str(meta.get("pid", 0))).exists()
        state = "running" if alive else "failed"
    else:
        state = "done" if rc == 0 else "failed"
    out = dict(meta, job=d.name, state=state, rc=rc)
    if out.get("token"):
        out["status_url"] = status_url(d.name, out["token"])
        out["qr_svg"] = qr_util.render_svg(out["status_url"])
    return out


def status_url(job: str, token: str) -> str:
    """The public live-status page URL for `job`/`token` (see pub_status()). Built from this node's own
    FQDN, same as setup_lab.py's print_summary() builds it independently for a bare CLI run."""
    return "https://{}/lab-builder/status.html?job={}&token={}".format(socket.getfqdn(), job, token)


def lab_create(name: str, keep: bool, passphrase: str = "", example: str = "",
               cloud_account: str = "") -> dict:
    """
    Start setup_lab.py as a background job, on saved lab `name` or on example lab `example`; refuse while one runs
    for that same lab. An example is not copied anywhere: setup_lab.py --example writes it to a temporary file for
    the run and removes it afterwards, so launching one leaves nothing on the server.

    `cloud_account` becomes --cloud-account, which outranks the account the definition names.

    `passphrase` decrypts the lab's credential files (libs/crypto_store.py). It is handed to the job in its own
    environment and nowhere else: not in job.json, not in the log, not on a command line other processes can read.
    """
    if example:
        # Checked against the catalogue, not just the name's shape: the value reaches a command line.
        if safe_name(example, "example lab") not in [e["name"] for e in lab_examples.catalogue()]:
            raise Refused("no example lab {}".format(example))
        label, lab_args = example, ["--example", example]
    else:
        lab = LABS_DIR / safe_name(name, "lab file")
        if lab.suffix != ".json" or not lab.is_file():
            raise Refused("no saved lab {}".format(name))
        label, lab_args = lab.name, [str(lab)]
    if cloud_account:
        lab_args = ["--cloud-account", safe_name(cloud_account, "cloud account")] + lab_args
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for d in JOBS_DIR.iterdir():
        if (d / "job.json").is_file():
            st = job_state(d)
            if st["lab"] == label and st["state"] == "running":
                raise Refused("lab {} is already being created (job {})".format(label, d.name))
    # The id carries the lab and the time; two runs within the same second get -2, -3, … rather than colliding.
    stamp = "{}-{}".format(Path(label).stem, time.strftime("%Y%m%d-%H%M%S"))
    job, attempt = stamp, 1
    while True:
        d = JOBS_DIR / job
        try:
            d.mkdir(mode=0o700)
            break
        except FileExistsError:
            attempt += 1
            job = "{}-{}".format(stamp, attempt)
    # The token makes the public status page unguessable: the job id alone is a predictable
    # <lab>-<timestamp>, but holding the id AND this token is what pub_status() actually checks.
    token = secrets.token_urlsafe(32)
    cmd = [SETUP_LAB] + (["--keep"] if keep else []) + lab_args
    env = dict(os.environ)
    if passphrase:
        env[crypto_store.PASSPHRASE_ENV] = passphrase
    else:
        env.pop(crypto_store.PASSPHRASE_ENV, None)
    env["LAB_STATUS_JOB"] = job
    env["LAB_STATUS_TOKEN"] = token
    with open(d / "log", "wb") as log:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", '"$@"; echo $? > "$0"', str(d / "rc")] + cmd,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(JOBS_DIR), env=env, start_new_session=True, close_fds=True)
    (d / "job.json").write_text(json.dumps({"lab": label, "started": int(time.time()), "pid": proc.pid,
                                            "keep": keep, "example": bool(example), "token": token}))
    url = status_url(job, token)
    return {"job": job, "status_url": url, "qr_svg": qr_util.render_svg(url)}


class _NeedPassphrase(Exception):
    """An encrypted cloud account and no passphrase to decrypt it with."""


class _WrongPassphrase(Exception):
    """The passphrase given does not decrypt a cloud account."""


def cloud_accounts(lab: dict) -> list:
    """Every cloud_account lab definition `lab` names, in common or on a node, each once."""
    names = [(lab.get("common") or {}).get("cloud_account")]
    names += [(cfg or {}).get("cloud_account") for cfg in (lab.get("nodes") or {}).values()]
    return list(dict.fromkeys(str(n) for n in names if n))


def unlock_account(name: str, config: dict, passphrase: str) -> None:
    """
    Load cloud account `name`, decrypting it with `passphrase` when it is encrypted, so the preflight that follows
    finds it decrypted (primary caches a passphrase that worked). Raises _NeedPassphrase or _WrongPassphrase; any
    other problem with the account is left to the preflight to report.
    """
    tried = []

    def given(_label):
        if tried:
            raise _WrongPassphrase()
        if not passphrase:
            raise _NeedPassphrase()
        tried.append(True)
        return passphrase

    primary.try_load_cloud_account(name, config=config, passphrase_prompt=given)


def lab_validate(req: dict) -> dict:
    """
    setup_lab.py's preflight on the lab definition in req["config"], as root: host checks on and its cloud accounts
    read, decrypted with req["passphrase"] when encrypted. The passphrase is used for this request only.
    """
    lab = req.get("config")
    if not isinstance(lab, dict):
        raise Refused("config must be a lab definition object")
    passphrase = str(req.get("passphrase") or "")
    os.environ.pop(crypto_store.PASSPHRASE_ENV, None)
    config = primary.load_config() if Path("/etc/lab_creation.cfg").exists() else {}
    try:
        # The decrypt loop prints its own retry messages; stdout carries only this helper's JSON answer.
        with contextlib.redirect_stdout(io.StringIO()):
            for name in cloud_accounts(lab):
                unlock_account(name, config, passphrase)
    except _NeedPassphrase:
        return {"ok": False, "need_passphrase": True,
                "output": "This lab's cloud credentials are encrypted: their passphrase is needed to validate them."}
    except _WrongPassphrase:
        time.sleep(WRONG_PASSPHRASE_DELAY)
        return {"ok": False, "wrong_passphrase": True,
                "output": "The passphrase does not decrypt this lab's cloud credentials."}

    import lab_creation as lc

    defaults = primary.load_defaults() if Path("/etc/lab_creation.defaults").exists() else {}
    buf = io.StringIO()
    fd, path = tempfile.mkstemp(prefix="lab-validate-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(lab, fh)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                ok = lc.validate_lab_definition(
                    primary.load_definition(path), config,
                    defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources"),
                    defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation"),
                    vm_img_loc=defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/"))
            except SystemExit:
                ok = False
    finally:
        os.unlink(path)
    return {"ok": bool(ok), "output": _ANSI.sub("", buf.getvalue()).strip()}


def lab_status(job: str) -> dict:
    """Job `job`'s state and the last LOG_TAIL_LINES lines of its log."""
    d = job_dir(job)
    lines = (d / "log").read_text(errors="replace").splitlines() if (d / "log").is_file() else []
    return dict(job_state(d), log="\n".join(lines[-LOG_TAIL_LINES:]))


def pub_status(job: str, token: str) -> dict:
    """lab_status(job)'s answer, but only for whoever holds `job`'s own token — no login needed, since
    the random token is itself the credential. A missing job and a wrong token get the exact same
    message, so a guess against this command cannot tell which one it got wrong."""
    d = JOBS_DIR / safe_name(job, "job")
    meta_file = d / "job.json"
    if not meta_file.is_file():
        raise Refused("no such lab or wrong link")
    meta = json.loads(meta_file.read_text())
    if not token or not hmac.compare_digest(str(meta.get("token", "")), token):
        raise Refused("no such lab or wrong link")
    out = lab_status(job)
    out.pop("token", None)
    out.pop("status_url", None)
    out.pop("qr_svg", None)
    return out


def lab_jobs() -> dict:
    """Every job, newest first."""
    if not JOBS_DIR.is_dir():
        return {"jobs": []}
    jobs = [job_state(d) for d in JOBS_DIR.iterdir() if (d / "job.json").is_file()]
    return {"jobs": sorted(jobs, key=lambda j: j.get("started", 0), reverse=True)}


POWER_ACTIONS = ("status", "start", "stop")

# A lab-wide start or stop waits for each VM, so it can take minutes on a large lab; past this it gives up and
# the answer says so, rather than the web request hanging for ever.
POWER_TIMEOUT = 900


def lab_power(action: str, name: str = "", example: str = "", cloud_account: str = "") -> dict:
    """
    Run vm_power.py on saved lab `name` or example lab `example` and answer {"action", "lab", "result", "note"}.

    Unlike lab_create() this waits for the command and answers with its output: nothing is provisioned, the states
    are the answer, and vm_power.py already orders and waits for the lab's own VMs. `note` carries what the
    backends said stopping does to the bill (their stderr), which is the part an operator stopping a lab to save
    money has to read.
    """
    if action not in POWER_ACTIONS:
        raise Refused("lab-power takes one of {}".format(", ".join(POWER_ACTIONS)))
    if example:
        if safe_name(example, "example lab") not in [e["name"] for e in lab_examples.catalogue()]:
            raise Refused("no example lab {}".format(example))
        label, lab_args = example, ["--example", example]
    else:
        lab = LABS_DIR / safe_name(name, "lab file")
        if lab.suffix != ".json" or not lab.is_file():
            raise Refused("no saved lab {}".format(name))
        label, lab_args = lab.name, [str(lab)]
    if cloud_account:
        lab_args = ["--cloud-account", safe_name(cloud_account, "cloud account")] + lab_args
    try:
        proc = subprocess.run([VM_POWER] + lab_args + [action], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=POWER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused("{} on {} did not finish within {}s — run vm_power.py on the automation node to "
                      "see where it is".format(action, label, POWER_TIMEOUT))
    try:
        result = json.loads(proc.stdout.decode("utf-8") or "{}")
    except ValueError:
        raise Refused("vm_power.py answered something other than JSON: {}".format(
            proc.stdout.decode("utf-8", "replace").strip()[:200]
            or proc.stderr.decode("utf-8", "replace").strip()[:200]))
    return {"action": action, "lab": label, "result": result,
            "note": proc.stderr.decode("utf-8", "replace").strip()}


def lab_power_from_args(args: list) -> dict:
    """
    lab-power's own arguments: the action, then a saved lab's FILE (or --example NAME in its place), in any order
    with --cloud-account NAME. lab_power() is what rejects an action that is not one of POWER_ACTIONS, so the
    answer names the three it takes.
    """
    example = cloud_account = ""
    positional, rest = [], list(args)
    while rest:
        arg = rest.pop(0)
        if arg in ("--example", "--cloud-account"):
            if not rest:
                raise Refused("{} needs a name".format(arg))
            if arg == "--example":
                example = rest.pop(0)
            else:
                cloud_account = rest.pop(0)
        elif arg.startswith("-"):
            raise Refused("lab-power: unexpected argument {!r}".format(arg))
        else:
            positional.append(arg)
    if len(positional) > 2:
        raise Refused("lab-power: unexpected argument {!r}".format(positional[2]))
    action = positional[0] if positional else ""
    name = positional[1] if len(positional) > 1 else ""
    if name and example:
        raise Refused("lab-power takes a saved lab or --example, not both")
    return lab_power(action, name, example=example, cloud_account=cloud_account)


def lab_create_from_args(args: list) -> dict:
    """
    lab-create's own arguments: a saved lab's FILE or --example NAME, in any order with --keep and
    --cloud-account NAME. The passphrase comes from stdin, never from here.
    """
    name = example = cloud_account = ""
    keep = False
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--keep":
            keep = True
        elif arg in ("--example", "--cloud-account"):
            if not rest:
                raise Refused("{} needs a name".format(arg))
            if arg == "--example":
                example = rest.pop(0)
            else:
                cloud_account = rest.pop(0)
        elif arg.startswith("-") or name or example:
            raise Refused("lab-create: unexpected argument {!r}".format(arg))
        else:
            name = arg
    if not name and not example:
        raise Refused("lab-create needs a saved lab's file name or --example NAME")
    if name and example:
        raise Refused("lab-create takes a saved lab or --example, not both")
    return lab_create(name, keep, read_stdin().get("passphrase", ""), example=example,
                      cloud_account=cloud_account)


def run(args: list) -> dict:
    """Carry out command line `args`; returns the JSON answer."""
    cmd, rest = (args[0], args[1:]) if args else ("", [])
    if cmd == "credentials-list" and not rest:
        return credentials_list()
    if cmd == "credentials-add" and not rest:
        return credentials_add(read_stdin())
    if cmd == "credentials-encrypt" and len(rest) == 1:
        return credentials_encrypt(rest[0], read_stdin())
    if cmd == "credentials-delete" and len(rest) == 1:
        return credentials_delete(rest[0])
    if cmd == "lab-create":
        return lab_create_from_args(rest)
    if cmd == "lab-validate" and not rest:
        return lab_validate(read_stdin())
    if cmd == "lab-power":
        return lab_power_from_args(rest)
    if cmd == "lab-status" and len(rest) == 1:
        return lab_status(rest[0])
    if cmd == "lab-jobs" and not rest:
        return lab_jobs()
    if cmd == "pub-status" and len(rest) == 1:
        return pub_status(rest[0], read_stdin().get("token", ""))
    raise Refused("usage: lab-builder-helper {credentials-list|credentials-add|credentials-encrypt FILE|"
                  "credentials-delete FILE|lab-create {FILE|--example NAME} [--keep] [--cloud-account NAME]|"
                  "lab-power {status|start|stop} {FILE|--example NAME} [--cloud-account NAME]|"
                  "lab-validate|lab-status JOB|lab-jobs|pub-status JOB}")


def main() -> None:
    if primary.wants_help(sys.argv[1:]):
        print(__doc__)
        return
    if sys.argv[1:] in (["--version"], ["-v"]):
        print("lab-builder-helper {}".format(__version__))
        return
    try:
        print(json.dumps(run(sys.argv[1:])))
    except (Refused, ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
