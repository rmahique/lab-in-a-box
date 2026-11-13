#!/usr/bin/env python3.11
# Part of lab-in-a-box — extra SSH keys on a lab's VMs (common/node "ssh_authorized_keys")
# Author/s: Raul Mahiques
# License: GPLv3

"""
libs/ssh_keys.py: the "ssh_authorized_keys" field of a lab definition, installed on a VM over SSH once it is up.

Each entry is a string, which goes to root, or {"key": ..., "user": ...}. A key is a public key line, an https URL
whose body holds key lines (such as https://github.com/<user>.keys), or an absolute path to a file of key lines on the
automation node. common's entries and the node's own are merged.

    entries(definition, vm_name)       -> list of {"key", "user"}, common's then the node's, duplicates dropped
    source_problem(key, check_files)   -> "" or what is wrong with an entry's key as written, fetching nothing
    key_lines(source)                  -> the key lines `source` holds; ValueError when it holds none
    install(ssh_target, user, keys)    -> "" or why `keys` could not be added to `user`'s authorized_keys
    install_node_keys(definition, vm_name, ssh_target) -> list of warnings, one per entry that could not be installed

An entry's shape and user name are checked against the schema (libs/lab_schema_base.py); source_problem() checks its
key. Nothing here fails a run: a key that cannot be installed is a warning for the caller to report.
"""

__version__ = "__LABVERSION__"

import re
import shlex
import urllib.request
from pathlib import Path

from lab_creation import log, ssh_run, warn

FIELD = "ssh_authorized_keys"
DEFAULT_USER = "root"
URL_TIMEOUT = 20

# A public key line: an optional options prefix, a key type, and base64 key data, then an optional comment.
_KEY_RE = re.compile(r"(^|\s)(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+)\s+AAAA[A-Za-z0-9+/=]+(\s|$)")

# Exit code of the remote script when the user does not exist on the VM.
_NO_SUCH_USER = 3

# Runs on the VM as root, with the user as $1 and the key lines on stdin. Creates ~/.ssh and authorized_keys owned by
# the user, and appends each key that is not already a line of it.
_REMOTE_SCRIPT = (
    'u="$1"; home=$(getent passwd "$u" | cut -d: -f6); [ -n "$home" ] || exit ' + str(_NO_SUCH_USER) + '; '
    'grp=$(id -gn "$u"); f="$home/.ssh/authorized_keys"; '
    'install -d -m 700 -o "$u" -g "$grp" "$home/.ssh" && touch "$f" && chown "$u:$grp" "$f" && chmod 600 "$f" '
    '|| exit 1; '
    'while IFS= read -r k; do [ -n "$k" ] || continue; grep -qxF "$k" "$f" || printf "%s\\n" "$k" >> "$f"; done; '
    'if command -v restorecon >/dev/null 2>&1; then restorecon -R "$home/.ssh" 2>/dev/null; fi; exit 0'
)


def is_key_line(text: str) -> bool:
    """Whether `text` is one public key line."""
    return "\n" not in text and bool(_KEY_RE.search(text.strip()))


def _normalize(entry):
    """`entry` as {"key", "user"}, or None when it is neither a string nor a mapping with a "key"."""
    if isinstance(entry, str):
        return {"key": entry.strip(), "user": ""}
    if isinstance(entry, dict) and isinstance(entry.get("key"), str):
        return {"key": entry["key"].strip(), "user": str(entry.get("user") or "").strip()}
    return None


def raw_entries(definition, vm_name: str) -> list:
    """`vm_name`'s entries as written: common's, then the node's own."""
    common = (definition.get("common") or {}).get(FIELD) or []
    node = (((definition.get("nodes") or {}).get(vm_name) or {}).get(FIELD)) or []
    return list(common if isinstance(common, list) else [common]) + list(node if isinstance(node, list) else [node])


def entries(definition, vm_name: str) -> list:
    """`vm_name`'s entries as {"key", "user"}, common's then the node's, duplicates and unusable entries dropped."""
    out = []
    for entry in raw_entries(definition, vm_name):
        norm = _normalize(entry)
        if norm and norm["key"] and norm not in out:
            out.append(norm)
    return out


def source_problem(key: str, check_files: bool = True) -> str:
    """
    What is wrong with an entry's "key" as written, or "". Fetches nothing: an https URL is only checked for its form.
    With `check_files`, a path must name a file on this host.
    """
    if is_key_line(key) or key.startswith("https://"):
        return ""
    if key.startswith("http://"):
        return "'{}' must be fetched over https".format(key)
    if key.startswith("/"):
        if check_files and not Path(key).is_file():
            return "file '{}' does not exist on this host".format(key)
        return ""
    return "'{}' is neither a public key line, an https URL nor an absolute file path".format(key)



def _keys_in(text: str) -> list:
    """The key lines of `text`; blank lines and # comments are left out."""
    return [line.strip() for line in text.splitlines() if line.strip() and is_key_line(line)]


def key_lines(source: str) -> list:
    """The key lines `source` holds: itself when it is one, else a fetched https URL's or a file's. ValueError when
    it cannot be read or holds no key."""
    if is_key_line(source):
        return [source.strip()]
    try:
        if source.startswith("https://"):
            with urllib.request.urlopen(source, timeout=URL_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        elif source.startswith("/"):
            text = Path(source).read_text(errors="replace")
        else:
            raise ValueError("'{}' is neither a public key line, an https URL nor an absolute file path".format(source))
    except OSError as e:
        raise ValueError("could not read '{}': {}".format(source, e))
    keys = _keys_in(text)
    if not keys:
        raise ValueError("'{}' holds no public key".format(source))
    return keys


def install(ssh_target: str, user: str, keys: list) -> str:
    """Add `keys` to `user`'s authorized_keys on `ssh_target`, over SSH as root. Returns "" or why it could not."""
    r = ssh_run(ssh_target, "sh -c {} _ {}".format(shlex.quote(_REMOTE_SCRIPT), shlex.quote(user)),
                check=False, capture=True, input_text="\n".join(keys) + "\n")
    if r.returncode == 0:
        return ""
    if r.returncode == _NO_SUCH_USER:
        return "user '{}' does not exist".format(user)
    return ((r.stderr or "") + (r.stdout or "")).strip() or "exit {}".format(r.returncode)


def install_node_keys(definition, vm_name: str, ssh_target: str = "") -> list:
    """
    Install `vm_name`'s ssh_authorized_keys on it, reached at `ssh_target` (default: its myip, else its name).
    Returns one warning per entry that could not be installed, each also printed; an empty list when all were.
    """
    todo = entries(definition, vm_name)
    if not todo:
        return []
    ssh_target = ssh_target or ((definition.get("nodes") or {}).get(vm_name) or {}).get("myip") or vm_name
    log("Adding {} ssh_authorized_keys entr{} on \"{}\"".format(len(todo), "y" if len(todo) == 1 else "ies", vm_name))
    warnings = []
    for entry in todo:
        user = entry["user"] or DEFAULT_USER
        label = entry["key"] if not is_key_line(entry["key"]) else entry["key"][:40] + "..."
        try:
            problem = install(ssh_target, user, key_lines(entry["key"]))
        except ValueError as e:
            problem = str(e)
        if problem:
            message = "ssh_authorized_keys on '{}': {} for {} not added: {}".format(vm_name, label, user, problem)
            warn(message)
            warnings.append(message)
    return warnings
