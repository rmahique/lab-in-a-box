#!/usr/bin/env python3
# Part of lab-in-a-box, prepares the hypervisor to work as a lab_automation node
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of setup_demo_server/setup_kvm_node.sh — calls the modular
# per-OS profiles in libs/kvm_host_profiles.py directly, in-process, instead of
# a single hardcoded if/elif. setup_lab_automation.sh (building the automation
# VM's own image) is unchanged/out of scope here — it stays OS-agnostic since
# it always builds a SLE Micro image via chroot, regardless of the host OS.

"""
setup_kvm_node.py — prepare a hypervisor host to run lab-in-a-box.

Usage:
    setup_kvm_node.py [-y] [--share-storage-from[=HOST]] [--copy-storage-from[=HOST]] [<IP/hostname>]

    -y              Automatically accept (run locally without confirmation)
    <IP/hostname>   Set up that remote host over SSH instead of locally

    --share-storage-from[=HOST]  sshfs-mount /var/lib/libvirt/images from HOST
                                  instead of managing local storage. HOST
                                  defaults to lab.cfg's _virt_srv if omitted.
    --copy-storage-from[=HOST]   One-time rsync copy from HOST instead.
                                  Mutually exclusive with --share-storage-from.

    Both flags are optional and additive: the default (neither given) is the
    original single-host bootstrap behavior, unchanged. If an automation host
    (lab.cfg's _myip) is already up and reachable, this host's DNS is also
    pointed at it automatically — never on the very first bootstrap, when the
    automation VM does not exist yet.
"""

__version__ = "__LABVERSION__"

import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
for _candidate in ("/usr/local/lib/lab_creation", str(_SCRIPT_DIR.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import kvm_host_profiles  # noqa: E402

_BOLD = "\033[1m"
_RESET = "\033[0m"
_RED = "\033[1;31m"


def log(msg):
    print("\n{}###._ {} _.###{}\n".format(_BOLD, msg, _RESET))


def die(msg):
    print("{}ERROR{}: {}".format(_RED, _RESET, msg), file=sys.stderr)
    sys.exit(1)


def install_yq():
    """Download yq for the local architecture. Mirrors the inline python3 -c block in bash."""
    arch_map = {"x86_64": "amd64", "aarch64": "arm64"}
    machine = os.uname().machine
    arch = arch_map.get(machine, machine)
    try:
        urllib.request.urlretrieve(
            "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_{}".format(arch),
            "/usr/local/bin/yq")
        os.chmod("/usr/local/bin/yq", 0o755)
        print("yq installed to /usr/local/bin/yq")
    except OSError as e:
        print("{}WARNING{}: yq installation failed ({}), some features may not work".format(_RED, _RESET, e),
              file=sys.stderr)


def _primary_storage_host(cfg):
    """
    Default host for --share-storage-from/--copy-storage-from when the flag
    is given without an explicit value. Reuses lab.cfg's existing _virt_srv
    (e.g. "root@hypervisor" — already the primary-hypervisor pointer used by
    the automation VM's own sshfs mount of source images) rather than adding
    a new config field: strips any "user@" prefix to get a bare hostname.
    """
    virt_srv = cfg.get("_virt_srv", "") or ""
    return virt_srv.split("@", 1)[-1] if virt_srv else ""


def _automation_host_reachable(myip, timeout=3):
    """
    True if the automation VM (lab.cfg's _myip) is already up and reachable
    over SSH. Gates the DNS-configuration step below: pointing a new KVM
    host's DNS at the automation host only makes sense once that host
    actually exists and is running. On the very first bootstrap of the
    first KVM node the automation VM has not been created yet, so this is
    always False there — the DNS step is skipped and do_it_all() behaves
    exactly as it did before this feature existed.
    """
    if not myip:
        return False
    return subprocess.run(
        ["nc", "-z", "-w", str(timeout), myip, "22"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def setup_shared_storage(share_from, copy_from):
    """
    Make /var/lib/libvirt/images (covers both ISO_LOC's sources/ subdir and
    VM_IMG_LOC) available on this host by sshfs-mounting it from another
    host, or by a one-time rsync copy. At most one of share_from/copy_from
    is set (mutually exclusive CLI flags) — a no-op if neither is given,
    which is the default and today's unchanged behavior (this host manages
    its own local storage).
    """
    path = "/var/lib/libvirt/images"
    Path(path).mkdir(parents=True, exist_ok=True)

    if share_from:
        log("Mount {} storage from {} via sshfs".format(path, share_from))
        fstab_line = (
            "{}:{} {} fuse.sshfs  noauto,x-systemd.automount,_netdev,reconnect,"
            "identityfile=/root/.ssh/id_rsa,allow_other,default_permissions 0 0\n"
        ).format(share_from, path, path)
        fstab = Path("/etc/fstab")
        text = fstab.read_text()
        if fstab_line not in text:
            fstab.write_text(text + fstab_line)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["mount", path], check=False)
    elif copy_from:
        log("Copy {} storage from {} via rsync (one-time)".format(path, copy_from))
        subprocess.run(["rsync", "-a", "{}:{}/".format(copy_from, path), "{}/".format(path)], check=False)


def configure_automation_dns(cfg):
    """
    Point this (additional) KVM host's DNS resolution at the already-running
    automation host, using the modular per-OS profile from kvm_host_profiles.
    Only called by do_it_all() when _automation_host_reachable() is True —
    see that function's docstring for why this never fires on the very
    first bootstrap.
    """
    profile = kvm_host_profiles.detect_profile()
    if profile is None:
        return
    log("Point DNS at automation host {} ({})".format(cfg.get("_myip", ""), cfg.get("_mydomain", "")))
    profile.configure_dns(cfg.get("_myip", ""), cfg.get("_mydomain", ""))


def download_automation_image(qcow_image):
    """Mirrors the wget step in do_it_all (bash)."""
    Path("/var/lib/libvirt/images/sources/").mkdir(parents=True, exist_ok=True)

    log("Download image to be used for the automation VM")
    qcow_basename = Path(qcow_image).name
    m = re.search(r"\d+\.\d+", qcow_basename)
    vm_ver = m.group(0) if m else ""
    dest = Path("/var/lib/libvirt/images/sources") / qcow_basename
    if dest.exists():
        return
    url = "https://download.opensuse.org/distribution/leap/{}/appliances/{}".format(vm_ver, qcow_basename)
    urllib.request.urlretrieve(url, str(dest))


_POOL_XML = """\
<!--
WARNING: THIS IS AN AUTO-GENERATED FILE. CHANGES TO IT ARE LIKELY TO BE
OVERWRITTEN AND LOST. Changes to this xml configuration should be made using:
  virsh pool-edit pool
or other application using the libvirt API.
-->

<pool type='dir'>
  <name>pool</name>
  <uuid>8bd63226-f3e4-4a14-965f-a75673a1a291</uuid>
  <capacity unit='bytes'>0</capacity>
  <allocation unit='bytes'>0</allocation>
  <available unit='bytes'>0</available>
  <source>
  </source>
  <target>
    <path>/var/lib/libvirt/images/sources</path>
  </target>
</pool>
"""


def do_it_all(cfg, script_dir, share_storage_from=None, copy_storage_from=None):
    """
    Mirrors do_it_all (bash). share_storage_from/copy_storage_from are new,
    additive, and both default to None (today's unchanged single-host
    behavior: this host manages its own local storage, no DNS reconfigured
    — identical to the original first-KVM-node bootstrap flow)."""
    lab_automation_script = script_dir / "setup_lab_automation.sh"
    if not lab_automation_script.is_file():
        die("Missing script, please download setup_lab_automation.sh script from the GIT repository")

    profile = kvm_host_profiles.detect_profile()
    if profile is None:
        die("OS type not detected or unsupported. Supported: openSUSE Leap, SLES, "
            "Ubuntu/Debian, RHEL/CentOS/Rocky/AlmaLinux/Fedora.")

    os_id = profile.os_info.get("ID", "")
    pretty_name = profile.os_info.get("PRETTY_NAME", os_id)
    print("- Installing in {}".format(pretty_name))

    if profile.unmapped_packages:
        print("{}WARNING{}: not installed automatically on {} (no verified package/repo mapping): {}".format(
            _RED, _RESET, profile.name, ", ".join(profile.unmapped_packages)), file=sys.stderr)
        print("          install these manually if you need them.", file=sys.stderr)

    log("Configure package repositories")
    profile.register_repos()

    log("Update all packages and install necessary ones")
    profile.refresh()
    profile.update()
    profile.install()

    log("Install yq")
    install_yq()

    if share_storage_from or copy_storage_from:
        setup_shared_storage(share_storage_from, copy_storage_from)

    if _automation_host_reachable(cfg.get("_myip", "")):
        configure_automation_dns(cfg)

    download_automation_image(cfg.get("_QCOW_IMAGE", ""))

    Path("/etc/libvirt/storage/pool.xml").write_text(_POOL_XML)
    try:
        Path("/etc/libvirt/storage/autostart/pool.xml").symlink_to("/etc/libvirt/storage/pool.xml")
    except OSError:
        pass  # mirrors bash's `&>/dev/null` — already exists or autostart dir missing

    subprocess.run(["systemctl", "enable", "--now", "libvirtd"], check=False)
    subprocess.run(["systemctl", "disable", "--now", "firewalld"], check=False)

    log("Start setup_lab_automation.sh script to create the automation VM")
    subprocess.run(["bash", str(lab_automation_script)], cwd=str(script_dir), check=False)


def _parse_storage_flags(args):
    """
    Extract --share-storage-from[=HOST]/--copy-storage-from[=HOST] from argv.
    Returns (remaining_args, share_from, copy_from): each of share_from/
    copy_from is None if its flag was not given at all (today's unchanged
    default), "" if given without a value (caller auto-detects via lab.cfg's
    _virt_srv once cfg is loaded), or the explicit HOST string.
    """
    share_from = None
    copy_from = None
    remaining = []
    for a in args:
        if a == "--share-storage-from":
            share_from = ""
        elif a.startswith("--share-storage-from="):
            share_from = a.split("=", 1)[1]
        elif a == "--copy-storage-from":
            copy_from = ""
        elif a.startswith("--copy-storage-from="):
            copy_from = a.split("=", 1)[1]
        else:
            remaining.append(a)
    return remaining, share_from, copy_from


def main():
    script_dir = _SCRIPT_DIR
    current_time = os.environ.get("_currenttime") or str(int(time.time()))

    args, share_from, copy_from = _parse_storage_flags(sys.argv[1:])
    target = args[0] if args else ""

    cfg_path = script_dir / "lab.cfg"
    if not cfg_path.is_file():
        die("Missing configuration file lab.cfg")
    log("Loading configuration file lab.cfg")
    cfg = primary.load_shell_vars(cfg_path)

    if share_from == "":
        share_from = _primary_storage_host(cfg)
        if not share_from:
            die("--share-storage-from requires a HOST (lab.cfg's _virt_srv is not set) "
                "— use --share-storage-from=<host>")
    if copy_from == "":
        copy_from = _primary_storage_host(cfg)
        if not copy_from:
            die("--copy-storage-from requires a HOST (lab.cfg's _virt_srv is not set) "
                "— use --copy-storage-from=<host>")

    if target:
        reachable = subprocess.run(
            ["nc", "-z", "-w", "5", target, "22"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0

        if reachable:
            log("\n## Setting up {} remotely ##\n".format(target))
            r = subprocess.run(["ssh-copy-id", "root@{}".format(target)])
            if r.returncode != 0:
                die("we need an SSH key to continue, to generate one please run "
                    "ssh-keygen -t ed25519 -f ~/id_ed25519_lab -N ''")

            remote_dir = "/var/tmp/{}_{}".format(Path(sys.argv[0]).name, current_time)
            subprocess.run(["ssh", "root@{}".format(target), "mkdir -p {}".format(remote_dir)], check=True)
            subprocess.run(
                ["scp", sys.argv[0], str(cfg_path), str(script_dir / "setup_lab_automation.sh"),
                 "root@{}:{}/".format(target, remote_dir)],
                check=True,
            )
            # Forward the already-resolved storage-sharing host(s) explicitly,
            # rather than the bare flag, so remote-dispatch behaves identically
            # to running do_it_all() locally instead of re-deriving anything.
            extra_flags = ""
            if share_from:
                extra_flags += " --share-storage-from={}".format(share_from)
            if copy_from:
                extra_flags += " --copy-storage-from={}".format(copy_from)
            subprocess.run(
                ["ssh", "root@{}".format(target),
                 "cd {} ; _currenttime={} python3 {} -y{}".format(
                     remote_dir, current_time, Path(sys.argv[0]).name, extra_flags)],
            )
        elif target == "-y":
            do_it_all(cfg, script_dir, share_storage_from=share_from, copy_storage_from=copy_from)
        else:
            print("{}ERROR{}: incorrect parameter \"{}\"".format(_RED, _RESET, target), file=sys.stderr)
    else:
        response = input("Are you sure? (yes/n): ")
        if response == "yes":
            do_it_all(cfg, script_dir, share_storage_from=share_from, copy_storage_from=copy_from)
        else:
            print("\n\nUsage: {} [-y] [<IP/hostname>]\n"
                  "-y Automatically accept\n"
                  "<IP/hostname> of the host you want to setup\n".format(sys.argv[0]))


if __name__ == "__main__":
    main()
