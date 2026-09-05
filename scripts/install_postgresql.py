#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install PostgreSQL in Kubernetes (Helm) or on the OS
# Author/s: Raul Mahiques
# License: GPLv3
#
# ─── MODE DETECTION ────────────────────────────────────────────────────────────
#   Kubernetes mode : script is called as a kclusters addon → clu_name env var is set
#   OS mode         : script is called as a nodes addon     → clu_name env var is empty
#   Override either mode by setting postgresql_mode = "kubernetes" or "os" in the JSON.
#
# ─── JSON section: "postgresql" ────────────────────────────────────────────────
#
# SHARED (both modes)
#   postgresql_mode       : [OPTIONAL] Where to run Postgres (default: auto) (options: auto, kubernetes, os)
#   postgresql_password   : [OPTIONAL] superuser password       (default: postgres123)
#   postgresql_db         : [OPTIONAL] default database name    (default: postgres)
#   postgresql_user       : [OPTIONAL] default username         (default: postgres)
#
# KUBERNETES MODE
#   postgresql_version    : [OPTIONAL] Helm chart version       (empty = latest, e.g. "15.5.17")
#   postgresql_ns         : [OPTIONAL] namespace                (default: postgresql)
#   postgresql_rel        : [OPTIONAL] Helm repo alias          (default: bitnami)
#   postgresql_repo_url   : [OPTIONAL] Helm repo URL            (default: https://charts.bitnami.com/bitnami)
#
# OS MODE  — the target node must list "postgresql" in its nodes[x].addons[] array
#   postgresql_pg_version : [OPTIONAL] PostgreSQL major version (default: 16, e.g. "14", "15", "16")
#                           Setting a lower version than the distro default achieves a downgrade.
#   postgresql_port       : [OPTIONAL] listening port           (default: 5432)
#   postgresql_listen     : [OPTIONAL] listen_addresses value   (default: *)
#
# SLES / SLE Micro note:
#   On SLES 15, the postgresql packages live in the "Server Applications Module".
#   Activate it first:  SUSEConnect -p sle-module-server-applications/15.6/x86_64
#   SLE Micro uses transactional-update and requires a reboot after package install.
#
# RHEL / CentOS note:
#   The official PGDG repository (yum.postgresql.org) is added automatically so that
#   any supported major version (including older ones) can be installed.
#
# Ubuntu / Debian note:
#   The official PGDG APT repository (apt.postgresql.org) is added automatically.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "postgresql",
    "targets": ["container", "vm", "baremetal"],
    "layers": ["kubernetes", "os-native"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import os
import re
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import setup_helm, helm_repo_add, ssh_run, ssh_output, reboot_vm, check_ssh_conn, die  # noqa: E402


def _digits_only(cfg, key, default, label):
    """
    Read an addon-config value that must be a bare integer
    (postgresql_pg_version / postgresql_port) and defensively validate it —
    found in code review 2026-09-05: both are interpolated unquoted,
    directly into remote shell commands and package/service/unit names all
    over this file (e.g. "postgresql{pg_ver}-setup", "port = {port}"), and
    _validate()'s own vport()/isdigit() checks are never actually invoked
    by the real deploy pipeline (setup_lab.py only calls the VM-level
    validate_lab_definition(), never each addon's own --validate) — so a
    value containing a shell metacharacter would otherwise reach a real
    remote shell unescaped. Dies with a clear error instead of silently
    interpolating something dangerous.
    """
    v = str(cfg.get(key) or default)
    if not re.match(r'^[0-9]+$', v):
        die("postgresql.{} = '{}' is invalid — must be a bare number".format(label, v))
    return v


def _validate(v):
    definition = v.definition
    mode = (definition.get("postgresql", {}) or {}).get("postgresql_mode", "")
    if mode and mode not in ("auto", "kubernetes", "os"):
        v.errors.append("[ERROR] postgresql.postgresql_mode='{}': must be auto, kubernetes, or os".format(mode))
    pgver = (definition.get("postgresql", {}) or {}).get("postgresql_pg_version", "")
    if pgver and not str(pgver).isdigit():
        v.errors.append(
            "[ERROR] postgresql.postgresql_pg_version='{}': must be a major version number (e.g. 16)".format(pgver))
    v.vport("postgresql", "postgresql_port")
    v.vns("postgresql")
    v.vver("postgresql")


# ─── Kubernetes (Helm) mode ─────────────────────────────────────────────────────

def setup_postgresql_repo(hostname, postgresql_rel=None, postgresql_repo_url=None):
    """Add the PostgreSQL (bitnami) Helm repo. Mirrors setup_postgresql_repo (bash)."""
    helm_repo_add(hostname, postgresql_rel or "bitnami", postgresql_repo_url or "https://charts.bitnami.com/bitnami")


def setup_postgresql_k8s(hostname, cfg):
    """Install PostgreSQL via Helm. Mirrors setup_postgresql_k8s (bash)."""
    rel = cfg.get("postgresql_rel") or "bitnami"
    ns = cfg.get("postgresql_ns") or "postgresql"
    ver_arg = "--version {}".format(cfg["postgresql_version"]) if cfg.get("postgresql_version") else ""
    pwd = cfg.get("postgresql_password") or "postgres123"
    db = cfg.get("postgresql_db") or "postgres"
    user = cfg.get("postgresql_user") or "postgres"

    ssh_run(hostname,
            "helm upgrade -i postgresql {}/postgresql --namespace {} --create-namespace "
            "--set auth.postgresPassword={} --set auth.database={} --set auth.username={} "
            "{}".format(rel, ns, pwd, db, user, ver_arg))
    print("PostgreSQL installed in Kubernetes. Namespace: {}".format(ns))
    print("Connect: kubectl -n {} exec -it postgresql-0 -- psql -U {}".format(ns, user))


# ─── OS mode — helpers ──────────────────────────────────────────────────────────

def pg_detect_os(hostname):
    """Detect the remote OS family and version. Mirrors _pg_detect_os (bash)."""
    raw = ssh_output(hostname,
                      "source /etc/os-release 2>/dev/null && printf '%s|%s|%s' \"${ID}\" \"${VERSION_ID}\" \"${ID_LIKE:-}\"")
    parts = raw.split("|")
    os_id = parts[0] if len(parts) > 0 else ""
    os_ver_id = parts[1] if len(parts) > 1 else ""
    os_like = parts[2] if len(parts) > 2 else ""
    os_ver_major = os_ver_id.split(".")[0] if os_ver_id else ""
    print("# Detected remote OS: id={} version={} like={}".format(os_id, os_ver_id, os_like))
    return {"id": os_id, "ver_id": os_ver_id, "like": os_like, "ver_major": os_ver_major}


def pg_configure_os(hostname, cfg, pg_ver):
    """Common post-install: set password, configure pg_hba and listen_addresses. Mirrors _pg_configure_os (bash)."""
    pw = cfg.get("postgresql_password") or "postgres123"
    listen = cfg.get("postgresql_listen") or "*"
    port = _digits_only(cfg, "postgresql_port", "5432", "postgresql_port")
    db = cfg.get("postgresql_db") or "postgres"
    user = cfg.get("postgresql_user") or "postgres"

    print("- Configuring PostgreSQL (password, listen_addresses, pg_hba)")

    pgdata = ssh_output(hostname,
                        "su - postgres -s /bin/bash -c 'psql -t -c \"SHOW data_directory;\"' 2>/dev/null "
                        "| tr -d ' \\n'") or "/var/lib/pgsql/data"

    result = ssh_run(hostname, "su - postgres -s /bin/bash -c 'psql'",
                      input_text="ALTER USER postgres PASSWORD '{}';".format(pw), check=False)
    if result.returncode != 0:
        ssh_run(hostname, "sudo -u postgres psql -c \"ALTER USER postgres PASSWORD '{}';\"".format(pw), check=False)

    if db != "postgres":
        ssh_run(hostname, "sudo -u postgres psql -c \"CREATE DATABASE {};\"".format(db), check=False)
    if user != "postgres":
        ssh_run(hostname, "sudo -u postgres psql -c \"CREATE USER {} WITH PASSWORD '{}';\"".format(user, pw),
                check=False)
        ssh_run(hostname, "sudo -u postgres psql -c \"GRANT ALL PRIVILEGES ON DATABASE {} TO {};\"".format(
            db, user), check=False)

    ssh_run(hostname, (
        "if [[ -f '{pgdata}/postgresql.conf' ]]; then\n"
        "    sed -i \"s|^#*listen_addresses.*|listen_addresses = '{listen}'|\" '{pgdata}/postgresql.conf'\n"
        "    sed -i \"s|^#*port.*|port = {port}|\" '{pgdata}/postgresql.conf'\n"
        "fi\n"
        "if [[ -f '{pgdata}/pg_hba.conf' ]]; then\n"
        "    grep -q 'host all all 0.0.0.0/0 md5' '{pgdata}/pg_hba.conf' || \\\n"
        "        echo 'host all all 0.0.0.0/0 md5' >> '{pgdata}/pg_hba.conf'\n"
        "fi"
    ).format(pgdata=pgdata, listen=listen, port=port))

    ssh_run(hostname,
            "systemctl restart postgresql.service 2>/dev/null || "
            "systemctl restart postgresql-{}.service 2>/dev/null || true".format(pg_ver), check=False)

    print("PostgreSQL installed on {}:{}".format(hostname, port))
    print("Connect: psql -h {} -U {} -d {}".format(hostname, user, db))


# ─── OS mode — SUSE family (zypper) ─────────────────────────────────────────────

def pg_install_suse(hostname, cfg):
    """Mirrors _pg_install_suse (bash)."""
    pg_ver = _digits_only(cfg, "postgresql_pg_version", "16", "postgresql_pg_version")
    print("- Installing PostgreSQL {} on {} (SUSE/openSUSE)".format(pg_ver, hostname))

    r = ssh_run(hostname, "zypper install -y --no-confirm postgresql{}-server postgresql{} 2>&1".format(
        pg_ver, pg_ver), check=False)
    if r.returncode != 0:
        r = ssh_run(hostname, "zypper install -y --no-confirm postgresql-server postgresql 2>&1", check=False)
        if r.returncode != 0:
            print("ERROR: Could not install PostgreSQL via zypper. On SLES, ensure the", file=sys.stderr)
            print("       'Server Applications Module' is activated:", file=sys.stderr)
            print("       SUSEConnect -p sle-module-server-applications/{}/x86_64".format(
                pg_detect_os(hostname)["ver_id"]), file=sys.stderr)
            sys.exit(1)

    ssh_run(hostname, (
        "if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then\n"
        "    su - postgres -s /bin/bash -c 'initdb -D /var/lib/pgsql/data' 2>/dev/null || \\\n"
        "    postgresql{pg_ver}-setup initdb 2>/dev/null || \\\n"
        "    postgresql-setup --initdb 2>/dev/null || true\n"
        "fi"
    ).format(pg_ver=pg_ver))

    ssh_run(hostname,
            "systemctl enable --now postgresql.service 2>/dev/null || "
            "systemctl enable --now postgresql-{}.service 2>/dev/null || true".format(pg_ver), check=False)
    pg_configure_os(hostname, cfg, pg_ver)


# ─── OS mode — SLE Micro (transactional-update, requires reboot) ───────────────

def pg_install_slemicro(hostname, cfg, virt_srv):
    """Mirrors _pg_install_slemicro (bash)."""
    pg_ver = _digits_only(cfg, "postgresql_pg_version", "16", "postgresql_pg_version")
    print("- Installing PostgreSQL {} on {} (SLE Micro — transactional)".format(pg_ver, hostname))

    r = ssh_run(hostname, "transactional-update --quiet pkg install -y postgresql{}-server postgresql{} 2>&1".format(
        pg_ver, pg_ver), check=False)
    if r.returncode != 0:
        r = ssh_run(hostname, "transactional-update --quiet pkg install -y postgresql-server postgresql 2>&1",
                    check=False)
        if r.returncode != 0:
            print("ERROR: Could not install PostgreSQL via transactional-update.", file=sys.stderr)
            sys.exit(1)

    print("- Rebooting {} to activate transactional changes".format(hostname))
    reboot_vm(virt_srv, hostname)
    time.sleep(5)
    check_ssh_conn(hostname)

    ssh_run(hostname, (
        "if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then\n"
        "    su - postgres -s /bin/bash -c 'initdb -D /var/lib/pgsql/data' 2>/dev/null || \\\n"
        "    postgresql{pg_ver}-setup initdb 2>/dev/null || true\n"
        "fi"
    ).format(pg_ver=pg_ver))

    ssh_run(hostname,
            "systemctl enable --now postgresql.service 2>/dev/null || "
            "systemctl enable --now postgresql-{}.service 2>/dev/null || true".format(pg_ver), check=False)
    pg_configure_os(hostname, cfg, pg_ver)


# ─── OS mode — RHEL/CentOS/Rocky/Alma family (yum/dnf + PGDG repo) ─────────────

def pg_install_rhel(hostname, cfg, os_info):
    """Mirrors _pg_install_rhel (bash)."""
    pg_ver = _digits_only(cfg, "postgresql_pg_version", "16", "postgresql_pg_version")
    print("- Installing PostgreSQL {} on {} (RHEL/CentOS family)".format(pg_ver, hostname))

    el_ver = os_info["ver_major"]
    # NOTE: bash's package-manager detection here (`command -v dnf 2>/dev/null && echo dnf
    # || echo yum`) had a real bug — 2>/dev/null only silences stderr, but `command -v`
    # prints the found path to STDOUT, so on a system with dnf this captured
    # "/usr/bin/dnf\ndnf" instead of just "dnf", corrupting every later package-manager
    # command. Fixed in bash (redirect stdout too) and avoided entirely here by checking
    # the exit code directly instead of parsing output.
    pkg_mgr = "dnf" if ssh_run(hostname, "command -v dnf", check=False, capture=True).returncode == 0 else "yum"

    pgdg_rpm = "https://download.postgresql.org/pub/repos/yum/reporpms/EL-{}-x86_64/pgdg-redhat-repo-latest.noarch.rpm".format(el_ver)
    ssh_run(hostname, "{} install -y {} 2>&1 || true".format(pkg_mgr, pgdg_rpm), check=False)

    if el_ver and int(el_ver) >= 8:
        ssh_run(hostname, "{} -qy module disable postgresql 2>/dev/null || true".format(pkg_mgr), check=False)

    r = ssh_run(hostname, "{} install -y postgresql{}-server 2>&1".format(pkg_mgr, pg_ver), check=False)
    if r.returncode != 0:
        print("ERROR: Could not install postgresql{}-server. Check PGDG repo availability.".format(pg_ver),
              file=sys.stderr)
        sys.exit(1)

    ssh_run(hostname,
            "/usr/pgsql-{pg_ver}/bin/postgresql-{pg_ver}-setup initdb 2>/dev/null || "
            "postgresql{pg_ver}-setup initdb 2>/dev/null || true".format(pg_ver=pg_ver), check=False)
    ssh_run(hostname, "systemctl enable --now postgresql-{}.service".format(pg_ver))

    port = _digits_only(cfg, "postgresql_port", "5432", "postgresql_port")
    ssh_run(hostname, (
        "if systemctl is-active firewalld &>/dev/null; then\n"
        "    firewall-cmd --permanent --add-port={}/tcp\n"
        "    firewall-cmd --reload\n"
        "fi"
    ).format(port), check=False)

    pg_configure_os(hostname, cfg, pg_ver)


# ─── OS mode — Ubuntu/Debian family (apt + PGDG repo) ──────────────────────────

def pg_install_debian(hostname, cfg):
    """Mirrors _pg_install_debian (bash)."""
    pg_ver = _digits_only(cfg, "postgresql_pg_version", "16", "postgresql_pg_version")
    print("- Installing PostgreSQL {} on {} (Ubuntu/Debian family)".format(pg_ver, hostname))

    ssh_run(hostname, (
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get install -y curl ca-certificates lsb-release gnupg 2>&1\n"
        "install -d /usr/share/postgresql-common/pgdg\n"
        "curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc "
        "-o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc\n"
        "echo \"deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] "
        "https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main\" "
        "> /etc/apt/sources.list.d/pgdg.list\n"
        "apt-get update -qq"
    ))

    r = ssh_run(hostname, "DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-{} 2>&1".format(pg_ver),
                check=False)
    if r.returncode != 0:
        print("ERROR: Could not install postgresql-{}. Check PGDG repo availability.".format(pg_ver),
              file=sys.stderr)
        sys.exit(1)

    port = _digits_only(cfg, "postgresql_port", "5432", "postgresql_port")
    ssh_run(hostname, (
        "if command -v ufw &>/dev/null && ufw status | grep -q 'Status: active'; then\n"
        "    ufw allow {}/tcp\n"
        "fi"
    ).format(port), check=False)

    pg_configure_os(hostname, cfg, pg_ver)


# ─── OS mode — dispatcher ───────────────────────────────────────────────────────

_SUSE_IDS = ("sle-micro", "slemicro")
_SUSE_ZYPPER_IDS = ("sles", "suse", "opensuse-leap", "opensuse-tumbleweed")
_RHEL_IDS = ("rhel", "centos", "rocky", "almalinux", "ol", "scientific", "fedora")
_DEBIAN_IDS = ("ubuntu", "debian", "linuxmint", "pop", "raspbian")


def setup_postgresql_os(hostname, cfg, virt_srv):
    """Detect the remote OS and dispatch to the matching installer. Mirrors setup_postgresql_os (bash)."""
    os_info = pg_detect_os(hostname)
    os_id = os_info["id"]

    if os_id in _SUSE_IDS:
        pg_install_slemicro(hostname, cfg, virt_srv)
    elif os_id in _SUSE_ZYPPER_IDS or os_id.startswith("opensuse"):
        pg_install_suse(hostname, cfg)
    elif os_id in _RHEL_IDS:
        pg_install_rhel(hostname, cfg, os_info)
    elif os_id in _DEBIAN_IDS:
        pg_install_debian(hostname, cfg)
    else:
        os_like = os_info["like"].lower()
        if "suse" in os_like:
            pg_install_suse(hostname, cfg)
        elif "rhel" in os_like or "fedora" in os_like or "centos" in os_like:
            pg_install_rhel(hostname, cfg, os_info)
        elif "debian" in os_like:
            pg_install_debian(hostname, cfg)
        else:
            print("ERROR: Unsupported OS '{}' (ID_LIKE='{}').".format(os_id, os_info["like"]), file=sys.stderr)
            print("       Supported families: SUSE/openSUSE, RHEL/CentOS, Ubuntu/Debian.", file=sys.stderr)
            sys.exit(1)


# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)
    config = primary.load_config()

    cfg = definition.get("postgresql", {}) or {}

    # Mode detection: explicit postgresql_mode override, else auto-detect from
    # whether clu_name was inherited from the environment (set by setup_lab.py
    # only for cluster-level addon invocations).
    clu_name_env = os.environ.get("clu_name", "")
    mode = cfg.get("postgresql_mode") or "auto"
    if mode == "auto":
        mode = "kubernetes" if clu_name_env else "os"

    if mode == "kubernetes":
        target = k8s.first_server_node(definition)
        if not target:
            sys.exit(1)
        vm_name, _ssh_cmd = target
        clu_name = k8s.get_vm_kcluster(definition, vm_name)
        online = definition.get("common", {}).get("online") == "1"

        setup_helm(vm_name, clu_name, online=online)
        setup_postgresql_repo(vm_name, postgresql_rel=cfg.get("postgresql_rel"),
                               postgresql_repo_url=cfg.get("postgresql_repo_url"))
        setup_postgresql_k8s(vm_name, cfg)
    else:
        virt_srv = config.get("VIRT_SRV", "")
        env_vm_name = os.environ.get("_vm_name") or None
        for vm_name, _ssh_cmd in k8s.addon_nodes(definition, "postgresql", vm_name=env_vm_name):
            setup_postgresql_os(vm_name, cfg, virt_srv)


if __name__ == "__main__":
    main()
