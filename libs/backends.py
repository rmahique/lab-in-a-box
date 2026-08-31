#!/usr/bin/env python3
# Part of lab-in-a-box — VM/hypervisor backend abstraction.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/backends.py — hypervisor/VM-backend abstraction.

VMBackend is the interface a compute backend implements to create/destroy/
manage the VMs a lab definition describes. LibvirtBackend is the only
implementation today (and the default) — it holds the logic that used to
live directly in lab_creation.py as flat functions taking a raw
virt_srv/remote_host pair (create_vm, delete_vm, copy_vm_image,
vm_is_reusable, reboot_vm, copy_to_hypervisor, _host_resources). Those flat
functions still exist in lab_creation.py as thin wrappers so every existing
caller (scripts + all 40 addons) keeps working unchanged: they build a
LibvirtBackend internally and delegate. check_or_generate_mac() got the
same treatment originally, but its flat wrapper had no real caller left
(everything goes through backend.check_or_generate_mac() via the object
get_backend() returns) — removed rather than kept as unused dead code.

get_backend() is the factory new code (Phase 5 tasks 5.2+) should call: it
resolves the KVM host (via resolve_kvm_host/locate_kvm_host, unchanged from
Phase 4) and returns a ready VMBackend, so callers never see host selection
or connection URIs. It is not yet wired into setup_vm.py/destroy_vm.py —
those still call resolve_kvm_host/locate_kvm_host directly, unchanged, to
keep this move zero-risk; a later task can switch them over.
"""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import primary
from lab_creation import (
    _RED, _YELLOW, _RESET,
    _empty,
    log, die,
    ssh_run, run_libvirt_tool,
    resolve_install_type, setup_salt,
    resolve_kvm_host, locate_kvm_host,
)
import lab_creation as _lc


def _read_conflict_confirmation():
    """
    Reads the y/N answer for a MAC-conflict prompt from the controlling TTY.
    Factored out of _check_or_generate_mac() so tests can monkeypatch it
    without needing a real /dev/tty (matches this codebase's own convention
    of reassigning a module-level name to fake something un-mockable
    otherwise — see e.g. lc.subprocess.run in the test suite).
    """
    with open("/dev/tty") as tty:
        return tty.readline().strip()


def _check_or_generate_mac(mac_by_domain, vm_name, mymac, definition, bridge, vm_net_model):
    """
    Shared MAC generate/validate/conflict-resolution logic — every backend's
    check_or_generate_mac() calls this with its own {domain: mac} map from
    its own list_used_macs(), so this one implementation isn't duplicated
    per backend. Returns (mymac, network) — the resolved MAC and the
    libvirt-style NETWORK string built from it (bridge=.../mac.address=...
    /model=...; used as-is by LibvirtBackend, ignored by HarvesterBackend,
    which builds its own KubeVirt interface spec instead).

    `definition` is the lab definition, already loaded once by the caller
    (primary.load_definition()) — a LabDefinition, so it already knows its
    own source path and format (see primary.py). This function has no
    business knowing either: it mutates `definition` in place and, on a
    conflict resolved by regenerating the MAC, calls
    primary.save_definition(definition) — which figures out where and in
    what format to write entirely on its own. There is no re-reading of the
    source file anywhere in this function, and no input_file/format
    parameter to thread through for a caller to get wrong.

    Behaviour:
      - mymac empty                              → generate a random locally-
        administered MAC not already in use.
      - mymac set, not in use (or used by this same VM) → use as-is.
      - mymac set, already used by a DIFFERENT VM → prompt on the controlling
        TTY to regenerate; on 'y'/'Y' update definition's nodes.<vm_name>.mymac
        in memory and save it (primary.save_definition — a new sibling file,
        the original source is never overwritten), then continue; on
        anything else, die().
    """
    used_macs = set(mac_by_domain.values())

    if _empty(mymac):
        mymac = _lc._generate_unused_mac(used_macs)
        log("- No MAC specified for \"{}{}{}\" — generated {}".format(_RED, vm_name, _RESET, mymac))
    else:
        mymac_lower = mymac.lower()
        owner = next((dom for dom, mac in mac_by_domain.items() if mac == mymac_lower), None)

        if owner and owner != vm_name:
            old_mac = mymac
            print("{}WARNING:{} MAC {} is already used by VM '{}'.".format(_YELLOW, _RESET, old_mac, owner),
                  file=sys.stderr)
            print("  Generate a new MAC and update {}? [y/N] ".format(definition.source_path), end="", flush=True)
            answer = _read_conflict_confirmation()
            if re.match(r"^[Yy]$", answer):
                mymac = _lc._generate_unused_mac(used_macs)
                definition["nodes"][vm_name]["mymac"] = mymac
                output_path = primary.save_definition(definition)
                log("- MAC updated to {} for \"{}{}{}\" — '{}' left untouched; updated copy written "
                    "to '{}' (merge it back by hand to keep this MAC on the next run)".format(
                        mymac, _RED, vm_name, _RESET, definition.source_path, output_path))
            else:
                die("MAC conflict on \"{}{}{}\" ({} owned by '{}') — aborting".format(
                    _RED, vm_name, _RESET, old_mac, owner))
        else:
            log("- MAC {} is available for \"{}{}{}\"".format(mymac, _RED, vm_name, _RESET))

    network = "bridge={},mac.address={},model={}".format(bridge, mymac, vm_net_model or "virtio")
    return mymac, network


def _parse_k8s_cpu(value):
    """Parse a Kubernetes CPU quantity ("500m", "2") into millicores."""
    value = str(value).strip()
    if value.endswith("m"):
        return int(value[:-1])
    return int(float(value) * 1000)


def _parse_k8s_memory(value):
    """Parse a Kubernetes memory quantity ("512Mi", "2Gi", "1024Ki", "2G", bare bytes) into KiB."""
    value = str(value).strip()
    units = {"Ki": 1.0, "Mi": 1024.0, "Gi": 1024.0 ** 2, "Ti": 1024.0 ** 3,
             "K": 1000.0 / 1024, "M": (1000.0 ** 2) / 1024, "G": (1000.0 ** 3) / 1024}
    for suffix in sorted(units, key=len, reverse=True):
        if value.endswith(suffix):
            return int(float(value[:-len(suffix)]) * units[suffix])
    return int(float(value) / 1024)  # bare bytes


class VMBackend(object):
    """Interface every compute backend implements."""

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        """
        Resolve wherever this backend's compute target actually is (a KVM
        host for LibvirtBackend, a kubeconfig/cluster for HarvesterBackend)
        and return a ready instance — the one place backend-specific
        connection/target resolution lives, so get_backend() itself never
        needs to know how a given backend finds its target.
        """
        raise NotImplementedError

    def create_vm(self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network, **kwargs):
        raise NotImplementedError

    def delete_vm(self, vm_name):
        raise NotImplementedError

    def vm_exists(self, vm_name):
        raise NotImplementedError

    def vm_is_reusable(self, vm_name, mymac, myip):
        raise NotImplementedError

    def reboot_vm(self, vm_name):
        raise NotImplementedError

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        raise NotImplementedError

    def list_used_macs(self):
        raise NotImplementedError

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        raise NotImplementedError

    def host_resources(self):
        raise NotImplementedError

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        raise NotImplementedError


class LibvirtBackend(VMBackend):
    """
    The default (and only, today) backend — talks to a libvirt hypervisor
    over `virsh --connect <virt_srv>` and provisioning files/images over SSH
    to `remote_host`. Every method body here is a straight move (unchanged
    logic) from what used to be a flat lab_creation.py function of the same
    behaviour, taking virt_srv/remote_host/vm_img_loc/lab_setup_path/iso_loc
    as explicit params — those are now constructor state instead.

    remote_host/iso_loc/vm_img_loc/lab_setup_path are optional because some
    operations (delete_vm, reboot_vm, vm_is_reusable, check_or_generate_mac,
    list_used_macs) only ever needed virt_srv.
    """

    def __init__(self, virt_srv, remote_host=None, iso_loc=None, vm_img_loc=None, lab_setup_path=None):
        self.virt_srv = virt_srv
        self.remote_host = remote_host
        self.iso_loc = iso_loc
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path

    def _virsh(self, *args, **kwargs):
        """See run_libvirt_tool()'s docstring: local virsh with --connect
        when available (unchanged, everywhere it already is), SSH to
        self.remote_host running against qemu:///system otherwise."""
        return run_libvirt_tool("virsh", self.remote_host, self.virt_srv, args, **kwargs)

    def _virt_install(self, *args, **kwargs):
        """Same fallback as _virsh(), for virt-install."""
        return run_libvirt_tool("virt-install", self.remote_host, self.virt_srv, args, **kwargs)

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        """
        for_existing=False (default) → placing a NEW VM: uses resolve_kvm_host()
        (resource-based selection across KVM_HOSTS, or the sole configured host).
        for_existing=True → an operation on an EXISTING VM (destroy/reboot/reuse
        check): uses locate_kvm_host() instead, which asks each host directly
        rather than re-running selection (see locate_kvm_host()'s docstring for
        why the two must never be conflated). Moved here verbatim from
        get_backend() — zero behavior change, just relocated to where a
        second backend's own resolve() can now live alongside it.
        """
        if for_existing:
            remote_host, virt_srv = locate_kvm_host(definition, vm_name, config)
        else:
            remote_host, virt_srv = resolve_kvm_host(definition, vm_name, config, vm_img_loc)
        return cls(virt_srv, remote_host=remote_host, iso_loc=iso_loc,
                   vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path)

    # ── MAC / domain introspection (moved from _list_domain_macs) ──────────

    def list_used_macs(self):
        """
        Returns (all_domain_lines, mac_by_domain) — the raw `virsh list --all
        --name` output lines and a {domain: lowercased_mac} map built from
        each domain's first vnet interface.
        """
        domains = self._virsh(
            "list", "--all", "--name",
            capture_output=True, text=True,
        ).stdout.splitlines()
        domains = [d.strip() for d in domains if d.strip()]

        mac_by_domain = {}
        for dom in domains:
            domif = self._virsh(
                "domiflist", dom,
                capture_output=True, text=True,
            ).stdout
            for line in domif.splitlines():
                if not line[:1].isspace():
                    continue
                fields = line.split()
                if len(fields) >= 5 and re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", fields[4].lower()):
                    mac_by_domain[dom] = fields[4].lower()
                    break
        return domains, mac_by_domain

    def vm_exists(self, vm_name):
        """True when a domain by this name is defined on this backend's host."""
        result = self._virsh(
            "dominfo", vm_name,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        """Validate or generate the MAC for a VM — see _check_or_generate_mac()'s
        docstring (this backend's own list_used_macs() supplies the map of
        MACs already in use)."""
        _, mac_by_domain = self.list_used_macs()
        return _check_or_generate_mac(mac_by_domain, vm_name, mymac, definition, bridge, vm_net_model)

    def vm_is_reusable(self, vm_name, mymac, myip):
        """
        Returns True when the VM should be kept, False when it must be destroyed
        and recreated. Checks in order: running on hypervisor, MAC matches (only
        when mymac is set), DNS resolves to expected IP, SSH accessible with
        default credentials. Any failed check returns False (safe default =
        recreate).
        """
        state = self._virsh(
            "domstate", vm_name, capture_output=True, text=True,
        ).stdout.strip()
        if state != "running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not running on hypervisor (state: {}) — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, state or "not found"))
            return False

        if not _empty(mymac):
            _, mac_by_domain = self.list_used_macs()
            actual_mac = mac_by_domain.get(vm_name)
            if mymac.lower() != (actual_mac or "NOT_FOUND"):
                log("  {}KEEP CHECK{} \"{}{}{}\": MAC mismatch (want \"{}\", got \"{}\") — will recreate".format(
                    _YELLOW, _RESET, _RED, vm_name, _RESET, mymac, actual_mac or "none"))
                return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        """
        Reboot a VM. Prefers a direct, guest-side reboot over anything
        virsh/ACPI-mediated, because BOTH are confirmed live (2026-08-28, on
        two separate disposable nuc6.mydemo.lab VMs) to be unreliable in
        this nested-virt environment in ways that matter:

        - `virsh reset` (the original, immediate fallback here) silently
          loses a just-installed transactional-update snapshot:
          `transactional-update pkg install` returns and correctly marks
          the new snapshot as default (confirmed via its own log: "New
          default snapshot is #N"), but `reset` — the hardware RESET line,
          equivalent to the physical reset button, not a guest- or
          qemu-mediated shutdown — can still boot back into the OLD
          snapshot. A plain guest-side `sync` first (an earlier attempted
          fix) does NOT prevent this — reproduced the bug again with it
          already in place.
        - A first fix escalated through ACPI `reboot` then ACPI `shutdown`
          before ever falling back to `reset`, on the theory that the
          guest's own clean shutdown sequence avoids whatever `reset`
          skips. Confirmed live that this HELPS (never loses a snapshot)
          but ACPI signals routinely never reach the guest in time at all
          in this environment — `virsh reboot` AND `virsh shutdown` each
          failed to produce a lifecycle event within 120s on the very same
          VM, still falling through to `reset` far more often than not.

        What actually works, confirmed live: a plain `ssh vm "reboot"` —
        bypassing ACPI-signal-forwarding through qemu entirely by running
        the reboot command directly in the guest's own init system —
        completed in ~15s on a VM where the ACPI path had just failed
        twice in a row. So: if the guest is currently reachable over SSH,
        reboot it that way and return immediately — the broken-pipe/
        connection-reset this causes is the expected, successful outcome,
        not a failure to check for (callers already poll for the guest
        coming back via check_ssh_conn(), same as every other reboot path
        here). Only fall back to the virsh-mediated ACPI/reset escalation
        below when the guest ISN'T reachable over SSH to begin with (there
        is no other way to intervene in that case).
        """
        try:
            probe = socket.create_connection((vm_name, 22), timeout=3)
            probe.close()
            reachable = True
        except OSError:
            reachable = False

        if reachable:
            ssh_run(vm_name, "sync && reboot", check=False)
            return

        self._virsh("reboot", vm_name, check=False)
        event = self._virsh(
            "event", vm_name, "--event", "lifecycle", "--timeout", "120",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if event.returncode == 0:
            return

        log("- \"{}{}{}\" did not reboot — trying a graceful shutdown+start cycle".format(_RED, vm_name, _RESET))
        self._virsh("shutdown", vm_name, check=False)
        stopped = self._virsh(
            "event", vm_name, "--event", "lifecycle", "--timeout", "120",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        state = self._virsh(
            "domstate", vm_name,
            capture_output=True, text=True, check=False,
        )
        if stopped.returncode == 0 or "shut off" in (state.stdout or ""):
            self._virsh("start", vm_name, check=False)
            return

        log("- \"{}{}{}\" did not shut down cleanly either — forcing a hard power cycle "
            "(last resort; may lose a just-written transactional-update snapshot)".format(
                _RED, vm_name, _RESET))
        self._virsh("reset", vm_name, check=False)

    def delete_vm(self, vm_name):
        """
        Remove a VM and all its storage from the hypervisor. Two calls, in
        this order:
          1. destroy (force power-off if running; no-op/fails harmlessly if
             already stopped — `destroy` never removes the domain's
             definition, only its running state)
          2. undefine --nvram --remove-all-storage (the one call that
             actually deletes disk images and the NVRAM/UEFI vars file, now
             guaranteed to still find the domain defined since step 1 never
             removes that definition)

        NOTE: an earlier version of this method (and bash's own delete_vm,
        libs/lab_creation.bash:1131-1137 — a pre-existing bug, faithfully
        ported, not introduced by this port) called a bare `undefine
        --nvram` (no --remove-all-storage) BEFORE `destroy`. That plain
        undefine succeeds regardless of whether the domain is running,
        removing its definition — so by the time the real
        `--remove-all-storage` undefine ran, the domain was already gone
        ("domain not found") and the disk image was silently never removed.
        Confirmed live on a disposable test VM (nuc6.mydemo.lab, 2026-08-28):
        the qcow2 file was left behind twice, cleaned up manually. Fixed by
        dropping the redundant/harmful first undefine entirely — `destroy`
        alone is enough to ensure a running domain is stopped before the one
        real undefine call removes both the definition and its storage.
        """
        log("Deleting VM '{}'".format(vm_name))
        self._virsh("destroy", vm_name,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self._virsh("undefine", vm_name, "--nvram", "--remove-all-storage",
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method="", disk_format="qcow2"):
        """
        Copy a QCOW2 source image and resize it on the hypervisor, landing
        it at the exact path create_vm()'s own disk_format expects
        (<vm_name>.qcow2, or <vm_name>.raw — see create_vm's docstring for
        why "raw" exists at all).

        install_iso: the disk is created empty by virt-install, so there's
        nothing to copy or resize.

        disk_format="raw": the source is genuinely QCOW2 content — a plain
        `cp` renamed to .raw would NOT be a valid raw disk (QCOW2 has its
        own container format/header), so this converts via `qemu-img
        convert -O raw`, not cp, when raw is requested. This is the one
        place the QCOW2->raw conversion cost is ever paid — once, here, at
        creation time — never later against an already-built multi-GB
        appliance image (see build_lab_usb.py / the plan's own reasoning
        for choosing raw from the start instead of converting at the end).
        """
        if config_method == "install_iso":
            log("- install_iso: skipping base image copy (disk created by virt-install)")
            return

        ext = "raw" if disk_format == "raw" else "qcow2"
        dest = "{}/{}.{}".format(self.vm_img_loc, vm_name, ext)

        log("- Copy the image for the new VM \"{}{}{}\"".format(_RED, vm_name, _RESET))
        if disk_format == "raw":
            result = ssh_run(self.remote_host, "qemu-img convert -O raw {}/{} {}".format(
                self.iso_loc, iso_image, dest), check=False)
            if result.returncode != 0:
                die("Failed to convert image for vm \"{}\" to raw".format(vm_name))
        else:
            result = ssh_run(self.remote_host, "cp {}/{} {}".format(self.iso_loc, iso_image, dest), check=False)
            if result.returncode != 0:
                die("Failed to copy image for vm  \"{}\"".format(vm_name))

        log("- Resize to {}G".format(vm_dsk_gb))
        result = ssh_run(self.remote_host, "qemu-img resize -f {} {} {}G".format(ext, dest, vm_dsk_gb), check=False)
        if result.returncode != 0:
            die("Failed to resize VM image \"{}\" to \"{}G\"".format(vm_name, vm_dsk_gb))

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        os_variant="slem5.4", boot="uefi", config_method="",  # boot: "uefi", "firmware=bios", "hd", …
        extra_disks=None, extra_filesystems=None, vm_dsk_bus="virtio",
        ign_file=None, com_file=None, salt_states="",
        install_type="", iso_image="", iso_loc="", mydns="",
        vcluster="", mymac=None,  # unused here — already embedded in `network` by check_or_generate_mac()
        disk_format="qcow2",  # "qcow2" (default, unchanged) or "raw" — see below
    ):
        """
        Create a VM on a KVM hypervisor via virt-install, covering all 6
        config_method branches:

            ""              → Ignition + Combustion (SLE Micro default)
            "install_iso"   → full OS install from installer ISO: autoyast/
                               kickstart/preseed (via --location/--extra-args,
                               blocks with --wait -1) or Ubuntu autoinstall (via
                               --cdrom + a seed CDROM built with mkisofs, also
                               --wait -1)
            "iso-cloud-init"→ NOTE: an incomplete stub inherited from bash —
                               this branch only computes an unused _boot_params
                               value and creates no VM at all. Preserved as a
                               no-op rather than guessing at the missing logic.
            "virt_customize"→ image already fully configured by
                               prepare_virt_customize_for_vm(); boot it directly
            "cloud-init"    → cloud-init ISO attached as a cdrom, then a 3-minute
                               wait, optional salt state apply, eject, reboot

        extra_disks entries look like "/dev/sdb,bus=scsi" or "UUID=xxx,bus=sata"
        (a path or a UUID= reference, with an optional per-disk bus override).
        """
        vm_img_loc = self.vm_img_loc
        remote_host = self.remote_host
        lab_setup_path = self.lab_setup_path

        log("Creating VM '{}'".format(vm_name))

        # Normalise boot flag: "uefi=off" / "bios" / "legacy" → "firmware=bios"
        _BIOS_ALIASES = {"uefi=off", "bios", "legacy"}
        boot_flag = "firmware=bios" if boot in _BIOS_ALIASES else boot

        extra_disk_args = []
        for dsk in (extra_disks or []):
            bus_match = re.search(r",bus=([a-z]+)", dsk)
            dsk_bus_override = bus_match.group(1) if bus_match else ""
            dsk_path = dsk.split(",")[0]
            if "UUID" in dsk_path:
                lookup = ssh_run(
                    remote_host,
                    "lsblk -o UUID,PATH | grep {} | cut -d' ' -f2".format(dsk_path.replace("UUID=", "")),
                    capture=True, check=False,
                )
                dsk_path = lookup.stdout.strip()
            extra_bus = dsk_bus_override or vm_dsk_bus or "virtio"
            extra_disk_args += ["--disk", "path={},bus={}".format(dsk_path, extra_bus)]

        extra_fs_args = []
        for fs in (extra_filesystems or []):
            extra_fs_args += ["--filesystem", fs]

        # disk_format="raw" is a deliberate, narrow exception to this
        # project's usual QCOW2-everywhere convention — used only for the
        # USB-delivery lab-host VM, whose own disk needs to be `dd`-able
        # directly onto a USB block device afterward (QCOW2's own container
        # format isn't). ".raw" filename + an explicit driver.type, same
        # dotted virt-install syntax already used for sparse=/boot.order=
        # above.
        disk_ext = "raw" if disk_format == "raw" else "qcow2"
        disk_type_arg = ",driver.type=raw" if disk_format == "raw" else ""
        base_args = [
            "--name", vm_name, "--autostart",
            "--boot", boot_flag, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
            "--os-variant", os_variant, "--import",
            "--disk", "size={},path={}/{}.{},sparse=no,bus={},boot.order=1{}".format(
                vm_dsk_gb, vm_img_loc, vm_name, disk_ext, vm_dsk_bus or "virtio", disk_type_arg),
            "--graphics", "spice,listen=0.0.0.0",
            "--network", network, "--noautoconsole",
        ]

        if config_method == "":
            ign = ign_file or vm_name
            com = com_file or vm_name
            qemu_args = (
                "-fw_cfg name=opt/com.coreos/config,"
                "file={}/ignition/{} "
                "-fw_cfg name=opt/org.opensuse.combustion/script,"
                "file={}/combustion/{}".format(lab_setup_path, ign, lab_setup_path, com)
            )
            # "--qemu-commandline", qemu_args (two argv elements) makes
            # argparse (virt-install's CLI parser) treat qemu_args as a new
            # option rather than this one's value, since it starts with "-"
            # (-fw_cfg ...) — "expected one argument". The single
            # --qemu-commandline=<value> form (bash's own
            # libs/lab_creation.bash uses this exact form) avoids the
            # ambiguity entirely.
            r = self._virt_install(
                *(base_args + extra_fs_args + extra_disk_args + ["--qemu-commandline={}".format(qemu_args)]))
            if r.returncode != 0:
                die("virt-install failed for '{}'".format(vm_name))

        elif config_method == "install_iso":
            itype = resolve_install_type(install_type, iso_image)

            if itype == "autoinstall":
                # Ubuntu 22+ subiquity: boot from --cdrom + a second "cidata" seed
                # CDROM. --wait -1 blocks until the installer powers the VM off.
                seed_local = tempfile.mktemp(prefix="seed_{}_".format(vm_name), suffix=".iso")
                seed_remote = "{}/seed_{}.iso".format(vm_img_loc, vm_name)
                mkiso = subprocess.run([
                    "mkisofs", "-J", "-l", "-R", "-V", "cidata", "-iso-level", "3",
                    "-o", seed_local,
                    "{}/install_iso/{}/user-data".format(lab_setup_path, vm_name),
                    "{}/install_iso/{}/meta-data".format(lab_setup_path, vm_name),
                ])
                if mkiso.returncode != 0:
                    die("mkisofs seed failed for '{}'".format(vm_name))
                scp = subprocess.run([
                    "scp", "-o", "StrictHostKeyChecking=accept-new", seed_local,
                    "root@{}:{}".format(remote_host, seed_remote),
                ])
                os.unlink(seed_local)
                if scp.returncode != 0:
                    die("scp seed failed for '{}'".format(vm_name))

                log("- Installing Ubuntu via autoinstall + seed CDROM (blocks until installer finishes)…")
                r = self._virt_install(
                    "--name", vm_name, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
                    "--os-variant", os_variant or "ubuntu24.04",
                    "--cdrom", "{}/{}".format(iso_loc, iso_image),
                    "--disk", "size={},path={}/{}.qcow2,sparse=no,bus={},boot.order=2".format(
                        vm_dsk_gb, vm_img_loc, vm_name, vm_dsk_bus or "virtio"),
                    "--disk", "path={},device=cdrom,readonly=on".format(seed_remote),
                    *(extra_disk_args + [
                        "--graphics", "spice,listen=0.0.0.0",
                        "--network", network, "--noautoconsole", "--wait", "-1",
                    ]))
                ssh_run(remote_host, "rm -f '{}'".format(seed_remote), check=False)
                if r.returncode != 0:
                    die("virt-install (autoinstall) failed for '{}'".format(vm_name))
                self._virsh("autostart", vm_name)
                self._virsh("start", vm_name)
                return

            location_arg = "{}/{}".format(iso_loc, iso_image)
            extra_args_by_type = {
                "autoyast": "autoyast=http://{}/lab_creation/install_iso/{}.xml".format(mydns, vm_name),
                "kickstart": "inst.ks=http://{}/lab_creation/install_iso/{}.ks inst.sshd".format(mydns, vm_name),
                "preseed": "auto=true priority=critical url=http://{}/lab_creation/install_iso/{}.preseed".format(mydns, vm_name),
            }
            extra_args = extra_args_by_type[itype]

            log("- Installing via {} (this will block until the installer finishes)…".format(itype))
            r = self._virt_install(
                "--name", vm_name, "--vcpus", str(vm_cpu), "--memory", str(vm_mem),
                "--os-variant", os_variant,
                "--location", location_arg,
                "--extra-args", "{} console=ttyS0,115200n8".format(extra_args),
                "--disk", "size={},path={}/{}.qcow2,sparse=no,bus={},boot.order=1".format(
                    vm_dsk_gb, vm_img_loc, vm_name, vm_dsk_bus or "virtio"),
                *(extra_disk_args + [
                    "--graphics", "spice,listen=0.0.0.0",
                    "--network", network, "--noautoconsole", "--wait", "-1",
                ]))
            if r.returncode != 0:
                die("virt-install (install_iso) failed for '{}'".format(vm_name))
            # Installer powered off the VM — bring it back up and mark autostart
            self._virsh("autostart", vm_name)
            self._virsh("start", vm_name)

        elif config_method == "iso-cloud-init":
            # Only ever computed an unused _boot_params value (a Harvester
            # config_url kernel arg) and never actually called virt-install —
            # a pre-existing incomplete stub, not something introduced by
            # this port. Left as a no-op.
            if vcluster == "harvester":
                pass  # _boot_params = "harvester.install.config_url=http://10.100.0.10/harvester/config-create.yaml"

        elif config_method == "virt_customize":
            # Image already fully configured by prepare_virt_customize_for_vm() —
            # boot it directly, no provisioning kernel args, no extra cdrom.
            r = self._virt_install(*(base_args + extra_fs_args + extra_disk_args))
            if r.returncode != 0:
                die("virt-install failed for '{}'".format(vm_name))

        elif config_method == "cloud-init":
            ci_iso = "{}/{}_ci.iso".format(vm_img_loc, vm_name)
            r = self._virt_install(*(base_args + extra_fs_args + extra_disk_args +
                                      ["--disk", "{},device=cdrom".format(ci_iso)]))
            if r.returncode != 0:
                die("virt-install for cloud-init failed for '{}'".format(vm_name))

            log("  - Waiting 3 minutes")
            time.sleep(180)

            if salt_states:
                log("  - applying salt states")
                setup_salt(vm_name, salt_states, lab_setup_path)
                for state in salt_states.split():
                    subprocess.run(["salt-ssh", "-i", "-v", "--update-roster", vm_name, "state.apply", state])

            log("  - eject media")
            self._virsh("change-media", vm_name, "--eject", ci_iso, check=False)

            log("- reboot node")
            self._virsh("reboot", vm_name, check=False)

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """
        Copy the provisioning materials needed for the install to the
        hypervisor.

        config_method:
          "virt_customize" / "install_iso" → nothing to copy — both are already
            entirely hypervisor-side (virt-customize) or automation-VM-HTTP-side
            (install_iso answer files), same early return as bash.
          ""  (ignition+combustion, the default) → rsync the per-VM combustion
            file and ignition file, then chmod them world-readable.
          anything else (e.g. "cloud-init") → rsync the per-VM template_* output
            files, then build a NoCloud cidata ISO from them on the hypervisor.
        """
        remote_host = self.remote_host
        lab_setup_path = self.lab_setup_path
        vm_img_loc = vm_img_loc or self.vm_img_loc

        log("- Copy accross the lab setup materials")
        mkdir_test = ssh_run(remote_host, "[[ -d {0}/ ]] || mkdir -p {0}/".format(lab_setup_path), check=False)
        if mkdir_test.returncode != 0:
            die("failed creating new folder {}".format(lab_setup_path))

        if config_method in ("virt_customize", "install_iso"):
            return

        if config_method == "":
            r = ssh_run(remote_host, "mkdir -p {}/{{combustion,ignition}}".format(lab_setup_path), check=False)
            if r.returncode != 0:
                die("failed creating combustion/ignition folders on {}".format(remote_host))

            r = subprocess.run(["rsync", "-aqv",
                                 "{}/combustion/{}".format(lab_setup_path, vm_name),
                                 "root@{}:{}/combustion/".format(remote_host, lab_setup_path)])
            if r.returncode != 0:
                die("failed to rsync combustion file for '{}'".format(vm_name))

            r = subprocess.run(["rsync", "-aqv",
                                 "{}/ignition/{}.ign".format(lab_setup_path, vm_name),
                                 "root@{}:{}/ignition/".format(remote_host, lab_setup_path)])
            if r.returncode != 0:
                die("failed to rsync ignition file for '{}'".format(vm_name))

            r = ssh_run(remote_host, "chmod 0644 {0}/ignition/* {0}/combustion/*".format(lab_setup_path), check=False)
            if r.returncode != 0:
                die("failed to chmod ignition/combustion files on {}".format(remote_host))
        else:
            r = ssh_run(remote_host, "mkdir -p {}/{}".format(lab_setup_path, config_method), check=False)
            if r.returncode != 0:
                die("failed creating '{}' folder on {}".format(config_method, remote_host))

            # bash relied on an unquoted shell glob (${_vm_name}*) which bash itself
            # expands before invoking rsync — expand it the same way here.
            sources = sorted(str(p) for p in Path(lab_setup_path, config_method).glob("{}*".format(vm_name)))
            if not sources:
                die("no '{}' files found for '{}' in {}/{}".format(config_method, vm_name, lab_setup_path, config_method))
            r = subprocess.run(["rsync", "-aqv"] + sources +
                                ["root@{}:{}/{}/".format(remote_host, lab_setup_path, config_method)])
            if r.returncode != 0:
                die("failed to rsync '{}' files for '{}'".format(config_method, vm_name))

            remote_cmd = (
                "cd {lsp}/{cm}/; "
                "for i in {vm}*; do cp ${{i}} /tmp/${{i/{vm}_/}}; done ; "
                "rm -f {img}/{vm}_ci.iso; "
                "mkisofs -J -l -R -V cidata -iso-level 3 -o /tmp/ci_{vm}.iso "
                "/tmp/user-data /tmp/meta-data /tmp/network-config "
                "&& mv /tmp/ci_{vm}.iso {img}/{vm}_ci.iso"
            ).format(lsp=lab_setup_path, cm=config_method, vm=vm_name, img=vm_img_loc)
            r = ssh_run(remote_host, remote_cmd, check=False)
            if r.returncode != 0:
                die("failed to build cidata ISO for '{}'".format(vm_name))

    def host_resources(self):
        """
        Query free vCPUs, free memory (MiB), and free disk (MiB) on
        self.vm_img_loc for this backend's host, over SSH. Raises
        RuntimeError/ValueError on any query failure — the caller (typically
        select_kvm_host) treats that host as disqualified rather than letting
        the whole selection blow up.

        virsh runs LOCALLY on `host` itself (qemu:///system), not via
        self.virt_srv's qemu+ssh:// URI — we're already executing remotely
        on that exact host via ssh_output, so reconnecting via
        qemu+ssh://root@{host} from within that same host is a redundant
        loopback SSH hop whose host key (for "localhost"/"::1" from that
        host's own perspective) is never pre-accepted, and hangs
        indefinitely waiting for interactive confirmation when run
        unattended — confirmed as a real bug (2026-08-27) via the identical
        pattern in scripts/refresh_hypervisor_status.py.
        """
        host = self.remote_host
        vm_img_loc = self.vm_img_loc
        total_cpus = int(_lc.ssh_output(host, "nproc"))

        running = [d for d in _lc.ssh_output(
            host, "virsh --connect qemu:///system list --name").splitlines() if d.strip()]
        used_cpus = 0
        for dom in running:
            used_cpus += int(_lc.ssh_output(
                host, "virsh --connect qemu:///system vcpucount --current {}".format(dom.strip())))
        free_cpu = max(total_cpus - used_cpus, 0)

        free_mem = int(_lc.ssh_output(host, "free -m | awk '/^Mem:/{print $7}'"))
        free_disk = int(re.sub(r"[^0-9]", "", _lc.ssh_output(
            host, "df -BM --output=avail {} | tail -1".format(vm_img_loc))))

        return free_cpu, free_mem, free_disk


class HarvesterBackend(VMBackend):
    """
    Provisions guest VMs on an already-running, externally-managed Harvester
    cluster instead of a KVM/libvirt hypervisor. NOT the same thing as
    scripts/install_harvester.py (a k8s addon that Helm-installs Harvester/
    SUSE Virtualization chart components INSIDE an RKE2/K3s cluster) — this
    backend instead CONSUMES an existing Harvester cluster, the same
    relationship LibvirtBackend has to an existing KVM hypervisor.

    v1 scope, fixed by design (not re-litigated here):
      - config_method="cloud-init" ONLY. Ignition+Combustion's fw_cfg
        delivery channel has no KubeVirt analogue.
      - single-cluster: kubeconfig/namespace come from /etc/lab_creation.cfg
        (HARVESTER_KUBECONFIG/HARVESTER_NAMESPACE), not per-node lab-JSON
        fields — matches this project's preference to avoid new lab-JSON
        parameters for backend-specific config.
      - the Harvester cluster is assumed to share the same bridge/L2 segment
        as the automation VM, so this project's existing DNS/BIND logic
        carries over unchanged; nothing here configures cluster networking.
      - copy_vm_image() does NOT import/upload an image — the operator must
        pre-import a VirtualMachineImage named after ISO_IMAGE in Harvester
        before deploying; dies clearly if it's missing rather than silently
        creating a VM with no boot disk.
      - create_vm()'s VM gets a real LAN-routable IP (matching this
        backend's own bridge/L2-sharing assumption above, and this
        project's DNS/SSH conventions) only when HARVESTER_NETWORK is set
        in /etc/lab_creation.cfg to a pre-existing Multus
        NetworkAttachmentDefinition ("<namespace>/<name>", or a bare name
        for one in HARVESTER_NAMESPACE) — same "operator pre-configures it,
        this backend only consumes it" stance as the VirtualMachineImage
        above: dies clearly if the NAD is missing, never creates one
        itself (the underlying Harvester VLAN network — which physical NIC,
        which VLAN ID — is a one-time cluster-level decision this project
        has no way to make generically, exactly like a KVM host's own
        bridge configuration). Omitting HARVESTER_NETWORK keeps the
        original pod-network behavior (backward compatible) — confirmed
        live 2026-08-30 that this is the real, previously-undocumented gap
        the docstring below used to flag as a TODO: a pod-network VM is
        never reachable via this project's DNS/SSH conventions the way any
        other backend's VM is.

    CRD/CLI shapes (verified via WebSearch against current docs, not
    assumed): VirtualMachine is kubevirt.io/v1; VirtualMachineImage is
    harvesterhci.io/v1beta1; NetworkAttachmentDefinition is
    k8s.cni.cncf.io/v1 (multus.networkName references it as
    "<namespace>/<name>"; the VM's own interface stays "bridge" binding
    regardless of pod vs. multus — only the networks[] entry differs,
    confirmed against Harvester's own documented VLAN-network VM example);
    a DataVolume booting from an existing image
    needs a harvesterhci.io/imageId annotation (<namespace>/<image-name>)
    and the image's own status.storageClassName (Harvester generates one
    per image, "longhorn-image-<suffix>") — read from the VirtualMachineImage
    at create_vm() time rather than guessed. virtctl start/stop/restart is
    the VM lifecycle control tool.

    LIVE-VERIFIED twice against real Harvester clusters (2026-08-29 against
    an ISO-installed cluster, 2026-08-30 against a PXE-installed one — see
    scripts/setup_harvester_cluster.py): a full check_or_generate_mac() →
    copy_vm_image() → prepare_cloud_init() → push_provisioning_files() →
    create_vm() → delete_vm() round trip, with create_vm() reaching a real
    Running VirtualMachine + VirtualMachineInstance with a real pod-network
    IP each time (status verified via kubectl/virtctl — neither run set
    HARVESTER_NETWORK, so both got the original pod-network path). That
    round trip surfaced a real gap since fixed (2026-08-30): the VM's
    pod-network IP is never reachable via this project's DNS/SSH
    conventions the way a libvirt-backed VM's is — provision_vm()'s own
    check_ssh_conn()/reboot_vm() steps would hang forever against one.
    HARVESTER_NETWORK (see v1-scope above) now lets create_vm() attach the
    VM to a pre-existing Multus VLAN network instead, giving it a real
    LAN-routable IP. LIVE-TESTED end-to-end 2026-08-30: a real ClusterNetwork
    + VlanConfig bound to a second, dedicated NIC (deliberately not the
    node's own mgmt NIC) + a VLAN-1 NetworkAttachmentDefinition, all
    hand-crafted against Harvester's real CRDs; create_vm() with real
    cloud-init static networking came up with the configured static IP (not
    a DHCP lease) and real SSH login succeeded — a Harvester-backed VM now
    behaves like any other backend's VM. HarvesterBackend deliberately does
    NOT create the ClusterNetwork/VlanConfig/NetworkAttachmentDefinition
    itself — that's a one-time, cluster-level physical-network decision
    (which NIC, which VLAN) an operator makes once, not something safe to
    infer per-VM.
    """

    def __init__(self, kubeconfig, namespace="default", vm_img_loc=None, lab_setup_path=None,
                 network_attachment=None):
        self.kubeconfig = kubeconfig
        self.namespace = namespace
        self.vm_img_loc = vm_img_loc
        self.lab_setup_path = lab_setup_path
        # <namespace>/<name> of a pre-existing Multus NetworkAttachmentDefinition
        # (k8s.cni.cncf.io/v1) — see create_vm()'s docstring for why this backend
        # doesn't create one itself. None (the default) preserves the original
        # pod-network behavior — backward compatible, no config change required.
        self.network_attachment = network_attachment

    @classmethod
    def resolve(cls, definition, vm_name, config, for_existing, vm_img_loc=None,
                iso_loc=None, lab_setup_path=None):
        kubeconfig = config.get("HARVESTER_KUBECONFIG")
        if not kubeconfig:
            die("backend 'harvester' requires HARVESTER_KUBECONFIG to be set in /etc/lab_creation.cfg "
                "(VM '{}')".format(vm_name))
        namespace = config.get("HARVESTER_NAMESPACE") or "default"
        network_attachment = config.get("HARVESTER_NETWORK") or None
        return cls(kubeconfig, namespace=namespace, vm_img_loc=vm_img_loc, lab_setup_path=lab_setup_path,
                   network_attachment=network_attachment)

    def _kubectl(self, *args, **kwargs):
        return subprocess.run(
            ["kubectl", "--kubeconfig", self.kubeconfig, "-n", self.namespace] + list(args), **kwargs)

    def _virtctl(self, *args, **kwargs):
        return subprocess.run(
            ["virtctl", "--kubeconfig", self.kubeconfig, "-n", self.namespace] + list(args), **kwargs)

    @staticmethod
    def _image_name(iso_image):
        """Derive a DNS-1123-safe VirtualMachineImage name from an ISO_IMAGE filename."""
        base = Path(iso_image or "").stem
        return re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "image"

    def _require_cloud_init(self, config_method, vm_name):
        if config_method != "cloud-init":
            die("HarvesterBackend only supports config_method=\"cloud-init\" (got '{}') for VM '{}'".format(
                config_method or "<empty>", vm_name))

    def vm_exists(self, vm_name):
        result = self._kubectl("get", "virtualmachine", vm_name,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0

    def list_used_macs(self):
        """Returns (vm_names, {vm_name: lowercased_mac}) from every VirtualMachineInstance's
        first interface with a MAC address."""
        result = self._kubectl("get", "vmi", "-o", "json", capture_output=True, text=True)
        if result.returncode != 0:
            return [], {}
        items = json.loads(result.stdout or "{}").get("items", [])
        names = []
        mac_by_name = {}
        for item in items:
            name = item.get("metadata", {}).get("name")
            if not name:
                continue
            names.append(name)
            interfaces = item.get("spec", {}).get("domain", {}).get("devices", {}).get("interfaces", []) or []
            for iface in interfaces:
                mac = iface.get("macAddress")
                if mac:
                    mac_by_name[name] = mac.lower()
                    break
        return names, mac_by_name

    def check_or_generate_mac(self, vm_name, mymac, definition, bridge="br0", vm_net_model="virtio"):
        _, mac_by_name = self.list_used_macs()
        return _check_or_generate_mac(mac_by_name, vm_name, mymac, definition, bridge, vm_net_model)

    def vm_is_reusable(self, vm_name, mymac, myip):
        """Same intent as LibvirtBackend's: True = keep, False = destroy and
        recreate. Checks the VirtualMachine's own printStatus (Running),
        then falls through to the same MAC/DNS/SSH checks."""
        result = self._kubectl("get", "virtualmachine", vm_name,
                                "-o", "jsonpath={.status.printableStatus}",
                                capture_output=True, text=True)
        status = (result.stdout or "").strip()
        if result.returncode != 0 or status != "Running":
            log("  {}KEEP CHECK{} \"{}{}{}\": not Running on the Harvester cluster (status: {}) — "
                "will recreate".format(_YELLOW, _RESET, _RED, vm_name, _RESET, status or "not found"))
            return False

        if not _empty(mymac):
            _, mac_by_name = self.list_used_macs()
            actual_mac = mac_by_name.get(vm_name)
            if mymac.lower() != (actual_mac or "NOT_FOUND"):
                log("  {}KEEP CHECK{} \"{}{}{}\": MAC mismatch (want \"{}\", got \"{}\") — will recreate".format(
                    _YELLOW, _RESET, _RED, vm_name, _RESET, mymac, actual_mac or "none"))
                return False

        try:
            resolved_ip = socket.gethostbyname(vm_name)
        except OSError:
            resolved_ip = None
        if resolved_ip != myip:
            log("  {}KEEP CHECK{} \"{}{}{}\": IP mismatch (want \"{}\", DNS gives \"{}\") — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET, myip, resolved_ip or "none"))
            return False

        ssh_test = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "root@{}".format(vm_name), "exit 0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ssh_test.returncode != 0:
            log("  {}KEEP CHECK{} \"{}{}{}\": SSH not accessible — will recreate".format(
                _YELLOW, _RESET, _RED, vm_name, _RESET))
            return False

        return True

    def reboot_vm(self, vm_name):
        result = self._virtctl("restart", vm_name)
        if result.returncode != 0:
            die("virtctl restart failed for '{}'".format(vm_name))

    def delete_vm(self, vm_name):
        """Graceful virtctl stop first (short timeout), then kubectl delete —
        mirrors this project's existing graceful-then-forceful pattern
        (reboot_vm's SSH-first, ACPI-then-hard-reset escalation)."""
        log("Deleting VM '{}'".format(vm_name))
        self._virtctl("stop", vm_name, timeout=30)
        result = self._kubectl("delete", "virtualmachine", vm_name, "--ignore-not-found",
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            die("kubectl delete virtualmachine failed for '{}'".format(vm_name))

    def copy_vm_image(self, iso_image, vm_name, vm_dsk_gb, config_method=""):
        self._require_cloud_init(config_method, vm_name)
        image_name = self._image_name(iso_image)
        result = self._kubectl("get", "virtualmachineimage", image_name,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            die("Harvester VirtualMachineImage '{}' not found in namespace '{}' — pre-import it "
                "before deploying VM '{}' (HarvesterBackend does not auto-import images)".format(
                    image_name, self.namespace, vm_name))

    def push_provisioning_files(self, vm_name, config_method="", vm_img_loc=None):
        """Applies a cloud-init Secret (userdata/networkdata) that create_vm()'s
        VirtualMachine manifest references via BOTH cloudInitNoCloud.secretRef
        (userdata) and cloudInitNoCloud.networkDataSecretRef (networkdata) —
        the cloud-init files themselves are generated the same
        backend-agnostic way as for LibvirtBackend (prepare_cloud_init()),
        just delivered differently."""
        self._require_cloud_init(config_method, vm_name)
        base = Path(self.lab_setup_path) / "cloud-init"
        userdata_path = base / "{}_user-data".format(vm_name)
        networkdata_path = base / "{}_network-config".format(vm_name)
        if not userdata_path.is_file():
            die("cloud-init user-data not found for '{}' at {}".format(vm_name, userdata_path))

        secret_manifest = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "{}-cloudinit".format(vm_name), "namespace": self.namespace},
            "stringData": {
                "userdata": userdata_path.read_text(),
                "networkdata": networkdata_path.read_text() if networkdata_path.is_file() else "",
            },
        }
        result = self._kubectl("apply", "-f", "-", input=json.dumps(secret_manifest), text=True)
        if result.returncode != 0:
            die("failed to apply cloud-init Secret for '{}'".format(vm_name))

    def create_vm(
        self, vm_name, vm_cpu, vm_mem, vm_dsk_gb, network,
        config_method="", iso_image="", mymac=None, **kwargs
    ):
        self._require_cloud_init(config_method, vm_name)
        image_name = self._image_name(iso_image)

        image_result = self._kubectl("get", "virtualmachineimage", image_name,
                                      "-o", "json", capture_output=True, text=True)
        if image_result.returncode != 0:
            die("Harvester VirtualMachineImage '{}' not found for VM '{}'".format(image_name, vm_name))
        image_status = json.loads(image_result.stdout or "{}").get("status", {}) or {}
        storage_class = image_status.get("storageClassName")
        if not storage_class:
            die("VirtualMachineImage '{}' has no status.storageClassName yet — it may still be "
                "importing; wait for it to become Ready before deploying VM '{}'".format(
                    image_name, vm_name))

        if self.network_attachment:
            nad_namespace, _, nad_name = self.network_attachment.rpartition("/")
            nad_namespace = nad_namespace or self.namespace
            nad_result = self._kubectl("get", "network-attachment-definitions.k8s.cni.cncf.io", nad_name,
                                        "-n", nad_namespace,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if nad_result.returncode != 0:
                die("Harvester NetworkAttachmentDefinition '{}' not found — pre-create it (a VLAN "
                    "network, in Harvester's own terms) before deploying VM '{}' with "
                    "HARVESTER_NETWORK set (HarvesterBackend does not create one itself)".format(
                        self.network_attachment, vm_name))
            vm_network = {"name": "default", "multus": {"networkName": self.network_attachment}}
        else:
            vm_network = {"name": "default", "pod": {}}

        secret_name = "{}-cloudinit".format(vm_name)
        # "bridge" is the correct KubeVirt interface binding for BOTH pod and
        # multus network types — only the networks[] entry above (pod vs.
        # multus) actually changes which one a VM gets. Confirmed against
        # Harvester's own documented VLAN-network VM example.
        interface = {"name": "default", "bridge": {}}
        if mymac:
            interface["macAddress"] = mymac

        vm_manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {"name": vm_name, "namespace": self.namespace},
            "spec": {
                "running": True,
                "dataVolumeTemplates": [{
                    "metadata": {
                        "name": "{}-rootdisk".format(vm_name),
                        "annotations": {"harvesterhci.io/imageId": "{}/{}".format(self.namespace, image_name)},
                    },
                    "spec": {
                        "pvc": {
                            "accessModes": ["ReadWriteMany"],
                            "volumeMode": "Block",
                            "storageClassName": storage_class,
                            "resources": {"requests": {"storage": "{}Gi".format(vm_dsk_gb)}},
                        },
                        # Confirmed live (2026-08-29) against a real Harvester
                        # v1.7.1 cluster: a VirtualMachineImage import does NOT
                        # create a clonable PVC at all (`kubectl get pvc`: none
                        # exist) — this version's storage backend is Longhorn's
                        # own BackingImage feature instead. The original guess
                        # here (source.pvc, cloning from a same-named PVC) failed
                        # outright: "The source pvc <image> doesn't exist". The
                        # per-image storageClassName (status.storageClassName,
                        # already used just above) is itself backed by that
                        # BackingImage, so a plain source.blank PVC provisioned
                        # under it comes back pre-populated with the image
                        # content via Longhorn's CSI driver — confirmed live:
                        # the resulting VM actually booted the real image.
                        "source": {"blank": {}},
                    },
                }],
                "template": {
                    "metadata": {"labels": {"kubevirt.io/vm": vm_name}},
                    "spec": {
                        "domain": {
                            "cpu": {"cores": int(vm_cpu)},
                            # KubeVirt requires memory.guest or resources.limits.memory —
                            # requests alone is rejected outright ("either memory.guest or
                            # resources.limits.memory must be set") — confirmed live
                            # 2026-08-29 against a real Harvester cluster. No overcommit:
                            # limits == requests, same value the VM is actually sized for.
                            "resources": {"requests": {"memory": "{}Mi".format(vm_mem)},
                                          "limits": {"memory": "{}Mi".format(vm_mem)}},
                            "devices": {
                                "disks": [
                                    {"name": "rootdisk", "disk": {"bus": "virtio"}},
                                    {"name": "cloudinitdisk", "disk": {"bus": "virtio"}},
                                ],
                                "interfaces": [interface],
                            },
                        },
                        "networks": [vm_network],
                        "volumes": [
                            {"name": "rootdisk", "dataVolume": {"name": "{}-rootdisk".format(vm_name)}},
                            # networkDataSecretRef, not just secretRef: confirmed live 2026-08-30
                            # (during the Multus network-attachment live test — never caught
                            # against pod networking, which doesn't need custom guest network
                            # config at all) that KubeVirt's cloudInitNoCloud volume type reads
                            # userdata from secretRef but SEPARATELY reads networkdata from
                            # networkDataSecretRef — a Secret's own "networkdata" key sitting
                            # inside secretRef's target is never even looked at. Without this,
                            # the NoCloud seed simply has no network-config file at all, and
                            # cloud-init falls back to its own auto-generated (DHCP) config for
                            # every detected interface — a real, previously-undiscovered
                            # HarvesterBackend bug (both fields point at the same Secret, which
                            # push_provisioning_files() already populates with both keys).
                            {"name": "cloudinitdisk", "cloudInitNoCloud": {
                                "secretRef": {"name": secret_name},
                                "networkDataSecretRef": {"name": secret_name},
                            }},
                        ],
                    },
                },
            },
        }

        log("Creating VM '{}' on Harvester".format(vm_name))
        result = self._kubectl("apply", "-f", "-", input=json.dumps(vm_manifest), text=True)
        if result.returncode != 0:
            die("kubectl apply failed for VirtualMachine '{}'".format(vm_name))

    def host_resources(self):
        """
        Best-effort free-capacity signal for select_kvm_host()'s host-picking
        logic: allocatable capacity (kubectl get nodes) minus current usage
        (kubectl top nodes, needs metrics-server), summed across the
        cluster. free_disk_mb has no cluster-wide equivalent via kubectl
        alone — returns 0 (never disqualifies a Harvester target on disk)
        until a real cluster is available to wire up Harvester's own
        storage-capacity API instead.
        """
        nodes_result = self._kubectl("get", "nodes", "-o", "json", capture_output=True, text=True)
        if nodes_result.returncode != 0:
            raise RuntimeError("kubectl get nodes failed: {}".format(nodes_result.stderr))
        nodes = json.loads(nodes_result.stdout or "{}").get("items", [])

        total_cpu_m = 0
        total_mem_ki = 0
        for n in nodes:
            alloc = n.get("status", {}).get("allocatable", {})
            total_cpu_m += _parse_k8s_cpu(alloc.get("cpu", "0"))
            total_mem_ki += _parse_k8s_memory(alloc.get("memory", "0Ki"))

        used_cpu_m = 0
        used_mem_ki = 0
        top_result = self._kubectl("top", "nodes", "--no-headers", capture_output=True, text=True)
        if top_result.returncode == 0:
            for line in top_result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 5:
                    used_cpu_m += _parse_k8s_cpu(fields[1])
                    used_mem_ki += _parse_k8s_memory(fields[3])

        free_cpu = max((total_cpu_m - used_cpu_m) // 1000, 0)
        free_mem_mb = max((total_mem_ki - used_mem_ki) // 1024, 0)
        free_disk_mb = 0
        return free_cpu, free_mem_mb, free_disk_mb


BACKENDS = {
    "libvirt": LibvirtBackend,
    "harvester": HarvesterBackend,
}


def get_backend(definition, config, vm_name, for_existing=False, vm_img_loc=None,
                 iso_loc=None, lab_setup_path=None):
    """
    Resolve which backend a VM should use and return a ready instance,
    hiding host/cluster selection and connection-detail construction from
    the caller. Backend selection: optional nodes[vm_name].backend, else
    common.backend, else config["BACKEND"], else "libvirt". Unknown name
    dies listing the known backends. The actual target-resolution work
    (which KVM host, which Harvester cluster) is delegated to the chosen
    backend's own resolve() classmethod — see VMBackend.resolve()'s
    docstring for why that split exists.
    """
    node_cfg = definition.get("nodes", {}).get(vm_name, {}) or {}
    common_cfg = definition.get("common", {}) or {}
    backend_name = node_cfg.get("backend") or common_cfg.get("backend") or config.get("BACKEND") or "libvirt"

    backend_cls = BACKENDS.get(backend_name)
    if backend_cls is None:
        die("Unknown backend '{}' for VM '{}' — supported backends: {}".format(
            backend_name, vm_name, ", ".join(sorted(BACKENDS))))

    return backend_cls.resolve(definition, vm_name, config, for_existing,
                                vm_img_loc=vm_img_loc, iso_loc=iso_loc, lab_setup_path=lab_setup_path)
