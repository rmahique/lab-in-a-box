"""
kvm_host_profiles.py — modular per-OS package/repo setup for KVM hypervisor hosts.

Mirrors the OS dispatch in setup_demo_server/setup_kvm_node.sh (bash), which
started as a hardcoded if/elif for opensuse-leap/sles. Restructured here as a
small profile registry so adding a new host OS means adding one class, not
editing a growing if/elif chain.

Each profile is responsible for:
  - detecting whether it applies to a given /etc/os-release id/id_like
  - refreshing package metadata
  - installing its package list
  - any one-time repo/subscription registration (SUSEConnect, apt repos, etc.)

Usage:
    profile = detect_profile()
    if profile is None:
        die(...)
    profile.register_repos()
    profile.refresh()
    profile.update()
    profile.install()
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import platform
import re
import subprocess
from pathlib import Path


def _read_os_release():
    info = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                info[key] = val.strip('"')
    except OSError:
        pass
    return info


class HostOSProfile:
    """
    Base class for a host OS package-installation profile.

    packages           : reliably-available packages installed straight from
                          the distro's own default repos.
    unmapped_packages  : bash's package list for this OS family that this
                          profile does NOT (yet) know how to install
                          automatically here — printed as warnings rather than
                          silently skipped or guessed at with an unverified
                          package/repo name. Empty for the two OSes bash
                          already handled (their lists are ported verbatim).
    """

    name = "generic"
    packages = []
    unmapped_packages = []

    def __init__(self, os_info):
        self.os_info = os_info

    def register_repos(self):
        """Override for any one-time repo/subscription registration. No-op by default."""
        pass

    def refresh(self):
        raise NotImplementedError

    def update(self):
        raise NotImplementedError

    def install(self):
        raise NotImplementedError

    def configure_dns(self, automation_ip, mydomain):
        """
        Point this host's DNS resolution at the automation host. Default
        implementation: rewrite /etc/resolv.conf directly with a fresh
        `search`/`nameserver` pair, dropping any previous ones — simplest
        portable approach, used as-is for OS families with no existing bash
        behavior to match (Debian/RHEL families below). SUSE profiles
        override this with the netconfig-based approach the existing bash
        configure_host_dns() already uses (see setup_lab_automation.sh),
        since that's the established, working mechanism there.

        Only called once an automation host already exists — see
        setup_kvm_node.py, which gates this on /etc/lab_creation.cfg being
        present (never called during the very first bootstrap of the first
        KVM node, before any automation host exists to point at).
        """
        resolv = Path("/etc/resolv.conf")
        try:
            lines = resolv.read_text().splitlines()
        except OSError:
            lines = []
        kept = [l for l in lines if not l.strip().startswith(("nameserver", "search"))]
        new_lines = ["search {}".format(mydomain), "nameserver {}".format(automation_ip)] + kept
        resolv.write_text("\n".join(new_lines) + "\n")

    def _run(self, cmd):
        subprocess.run(cmd, check=True)


# ── openSUSE Leap / SLES (zypper) ───────────────────────────────────────────
# Ported verbatim from bash's opensuse-leap/sles package lists — including the
# gap already flagged in bash: the SLES list is missing libvirt-daemon-qemu/
# qemu-tools/virt-install/libguestfs (present in the Leap list), and bash's own
# comment says guestmount's real dependency there hasn't been verified. Not
# "fixed" here — preserved exactly, since inventing a package name for that
# gap without verifying it against a real SLES host would just be a different
# kind of guess.

class OpenSUSELeapProfile(HostOSProfile):
    name = "opensuse-leap"
    packages = [
        "libvirt", "podman", "docker", "cri-tools", "minikube-bash-completion",
        "kubectl-who-can", "kubevirt-virtctl", "kubernetes1.28-client",
        "gpgme-devel", "device-mapper-devel", "libbtrfs-devel", "git-core", "mc",
        "bridge-utils", "tcpdump", "sensors", "ftsteutates-sensors",
        "netcat-openbsd", "gptfdisk", "libvirt-daemon-qemu", "qemu-tools",
        "virt-install", "libguestfs",
    ]

    def refresh(self):
        self._run(["zypper", "refresh"])

    def update(self):
        self._run(["zypper", "update", "-y"])

    def install(self):
        self._run(["zypper", "install", "-y"] + self.packages)

    def configure_dns(self, automation_ip, mydomain):
        """
        Mirrors configure_host_dns() in setup_lab_automation.sh: SUSE's
        netconfig mechanism regenerates /etc/resolv.conf from
        NETCONFIG_DNS_STATIC_SERVERS/_SEARCHLIST in
        /etc/sysconfig/network/config, so that's the file to edit rather than
        resolv.conf directly (which netconfig would just overwrite again).
        """
        cfg = Path("/etc/sysconfig/network/config")
        text = cfg.read_text()
        text = re.sub(r'^NETCONFIG_DNS_STATIC_SERVERS=.*$',
                      'NETCONFIG_DNS_STATIC_SERVERS="{}"'.format(automation_ip), text, flags=re.M)
        text = re.sub(r'^NETCONFIG_DNS_STATIC_SEARCHLIST=.*$',
                      'NETCONFIG_DNS_STATIC_SEARCHLIST="{}"'.format(mydomain), text, flags=re.M)
        cfg.write_text(text)
        subprocess.run(["netconfig", "update", "-f"], check=False)


class SLESProfile(OpenSUSELeapProfile):
    name = "sles"
    packages = [
        "libvirt", "podman", "docker", "cri-tools", "minikube-bash-completion",
        "kubectl-who-can", "kubevirt-virtctl", "kubernetes1.28-client",
        "gpgme-devel", "device-mapper-devel", "libbtrfs-devel", "git-core", "mc",
        "bridge-utils", "tcpdump", "sensors", "ftsteutates-sensors",
        "netcat-openbsd", "gptfdisk",
        # NOTE: bash's SLES list stops here — no libvirt-daemon-qemu/qemu-tools/
        # virt-install/libguestfs, unlike the Leap list above. Preserved as-is.
    ]
    _products = ("PackageHub", "sle-module-containers", "sle-module-basesystem", "sle-module-legacy")

    def register_repos(self):
        ver_id = self.os_info.get("VERSION_ID", "")
        arch = platform.machine()
        for product in self._products:
            self._run(["SUSEConnect", "--product", "{}/{}/{}".format(product, ver_id, arch)])


# ── Ubuntu / Debian (apt) ────────────────────────────────────────────────────
#
# NOTE ON COMPLETENESS: this profile is a best-effort mapping of the CORE
# virtualization/networking packages from bash's opensuse-leap list to their
# Debian-family equivalents. Several entries in bash's list have NO reliable
# apt equivalent installable from default repos without adding a third-party
# repository first (Docker's own repo for a current `docker`, the Kubernetes
# project's apt repo for `kubectl`/cri-tools, and minikube/virtctl/
# kubectl-who-can aren't distro-packaged at all — they're installed via direct
# binary download upstream). Rather than guess an apt repo URL/GPG key I can't
# verify, those are listed in unmapped_packages and reported as warnings
# instead of silently attempted or silently dropped. ftsteutates-sensors is a
# SUSE-specific kernel-module package with no Debian equivalent at all.
class DebianProfile(HostOSProfile):
    name = "debian"
    packages = [
        "libvirt-daemon-system", "libvirt-clients", "qemu-kvm", "qemu-utils",
        "virtinst", "libguestfs-tools",
        "libgpgme-dev", "libdevmapper-dev", "libbtrfs-dev",
        "git", "mc", "bridge-utils", "tcpdump", "lm-sensors",
        "netcat-openbsd", "gdisk", "podman",
    ]
    unmapped_packages = [
        "docker", "cri-tools", "minikube-bash-completion", "kubectl-who-can",
        "kubevirt-virtctl", "kubernetes1.28-client", "ftsteutates-sensors",
    ]

    def refresh(self):
        self._run(["apt-get", "update"])

    def update(self):
        self._run(["apt-get", "upgrade", "-y"])

    def install(self):
        self._run(["apt-get", "install", "-y"] + self.packages)


# ── RHEL / CentOS / Rocky / AlmaLinux / Fedora (dnf) ────────────────────────
#
# Same caveat as DebianProfile: docker/cri-tools/kubectl need their own repos
# (Docker CE repo, Kubernetes project repo) which aren't added here; minikube/
# virtctl/kubectl-who-can/ftsteutates-sensors have no dnf equivalent. Listed in
# unmapped_packages rather than guessed.
class RHELProfile(HostOSProfile):
    name = "rhel"
    packages = [
        "libvirt", "libvirt-client", "qemu-kvm", "virt-install", "libguestfs-tools",
        "gpgme-devel", "device-mapper-devel", "libbtrfs-devel",
        "git", "mc", "bridge-utils", "tcpdump", "lm_sensors",
        "nmap-ncat", "gdisk", "podman",
    ]
    unmapped_packages = [
        "docker", "cri-tools", "minikube-bash-completion", "kubectl-who-can",
        "kubevirt-virtctl", "kubernetes1.28-client", "ftsteutates-sensors",
    ]

    def refresh(self):
        pass  # dnf has no separate metadata-refresh step distinct from install/update

    def update(self):
        self._run(["dnf", "update", "-y"])

    def install(self):
        self._run(["dnf", "install", "-y"] + self.packages)


# ── Registry ──────────────────────────────────────────────────────────────────
# Keyed by /etc/os-release ID; ID_LIKE is checked as a fallback for anything
# not directly listed, same two-tier pattern used by install_postgresql.py's
# OS dispatch.

_BY_ID = {
    "opensuse-leap": OpenSUSELeapProfile,
    "sles": SLESProfile,
    "ubuntu": DebianProfile,
    "debian": DebianProfile,
    "linuxmint": DebianProfile,
    "pop": DebianProfile,
    "raspbian": DebianProfile,
    "rhel": RHELProfile,
    "centos": RHELProfile,
    "rocky": RHELProfile,
    "almalinux": RHELProfile,
    "fedora": RHELProfile,
    "ol": RHELProfile,
    "scientific": RHELProfile,
}


def detect_profile():
    """
    Detect the local host's OS and return an instantiated profile, or None if
    unrecognised (mirrors bash's OS-detection block and its "Unsupported OS"
    exit path — the caller decides how to report that).
    """
    os_info = _read_os_release()
    os_id = os_info.get("ID", "")

    cls = _BY_ID.get(os_id)
    if cls is None:
        os_like = os_info.get("ID_LIKE", "").lower()
        if "suse" in os_like:
            cls = OpenSUSELeapProfile
        elif "debian" in os_like:
            cls = DebianProfile
        elif "rhel" in os_like or "fedora" in os_like:
            cls = RHELProfile

    return cls(os_info) if cls else None
