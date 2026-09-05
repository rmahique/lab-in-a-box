#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will setup a Lab
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/setup_lab.sh — calls the python libraries
# (lab_creation, k8s, primary) directly, in-process. No bash is sourced or
# executed by this script for the DNS/VM/Kubernetes/addon phases below.
# destroy_vm.py/setup_vm.py are still invoked as separate processes for VM
# create/destroy, matching bash's own architecture (setup_lab.sh always called
# destroy_vm.sh/setup_vm.sh as separate scripts too, never sourced them).
# install_<addon> scripts are likewise separate processes, exactly as in bash.

"""
setup_lab.py — provision all VMs defined in a lab JSON, set up Kubernetes
clusters, and install cluster-level and VM-level addons in order.

Usage:
    setup_lab.py [--keep] <lab.json>
"""

__version__ = "__LABVERSION__"
_SCHEMA_VERSION = "1.0"

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
import lab_creation as lc  # noqa: E402
import k8s  # noqa: E402
import targets  # noqa: E402
import apps  # noqa: E402
import services  # noqa: E402
from destroy_vm import destroy_vm  # noqa: E402
from setup_vm import provision_vm  # noqa: E402

_HELP_TEXT = """\
Usage: setup_lab.py [--keep] <lab.json>

Provisions all VMs defined in the lab JSON, sets up Kubernetes clusters, and
installs cluster-level and VM-level addons in order.

Options:
  --keep    Skip VMs that already exist, are running, match the defined IP and
            MAC address, and are accessible via SSH with default credentials.
            Without this flag (default) every VM is destroyed and recreated.

The lab definition JSON must contain:
  nodes      — map of VM hostname → node config (myip, mymac, kcluster, …)
  common     — shared VM settings (ISO_IMAGE, VM_MEM, VM_DSK, VM_CPU, …)
  kclusters  — map of cluster name → cluster config (clu_type, addons, …)
  <addon>    — one section per addon listed in kclusters[x].addons or nodes[x].addons

Run 'install_<addon> --help' for the options accepted by each addon section.
Run 'setup_lab.py --input-definition [json|yaml]' for the machine-readable schema.
"""


def _merged_env(definition, config, defaults, vm_name):
    """
    Merge defaults + cfg + per-VM JSON vars, in bash's actual precedence
    order (JSON wins — load_vm_vars runs last in the bash pipeline).
    """
    env = {}
    env.update(defaults)
    env.update(config)
    env.update(lc.load_vm_vars(definition, vm_name))
    return env


def phase_services(definition, config, defaults):
    """
    Configure+enable every service listed in common.services (optional;
    absent means today's implicit default of nothing new — DNS/HTTP are
    already running from the automation VM's own bootstrap). Order runs
    before phase_create_vms so PXE/DHCP infrastructure is ready before any
    node might try to boot from it.
    """
    service_names = definition.get("common", {}).get("services") or []
    if not service_names:
        return
    lc.log("Configuring lab services")
    lc._level += 1
    for name in service_names:
        svc = services.get(name, lab_setup_path=defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation"))
        lc.log("Service \"{}{}{}\"".format(lc._RED, name, lc._RESET))
        svc.install()
        svc.configure(definition, config)
        svc.enable()
    lc._level -= 1


def phase_dns(definition, remote_dns_servers):
    lc.log("Add Kubernetes cluster DNS entries")
    lc._level += 1
    for clu_name in k8s.list_kclusters(definition):
        clu_cfg = k8s.load_kclu_vars(definition, clu_name)
        k8s.add_kclu_dns(definition, clu_name, clu_cfg.get("clu_type", ""), clu_cfg.get("mydomain", ""),
                          remote_dns_servers=remote_dns_servers)
    lc._level -= 1


def phase_create_vms(definition, config, defaults, json_file, keep):
    lc.log("Creating VMs")
    lc._level += 1
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        lc.log("Node: \"{}{}{}\"".format(lc._RED, vm_name, lc._RESET))

        if targets.is_existing_node(node_cfg):
            lc.log("  Using existing host \"{}{}{}\" — not creating a VM for it".format(
                lc._RED, vm_name, lc._RESET))
            lc.check_ssh_conn(vm_name)
            continue

        env = _merged_env(definition, config, defaults, vm_name)
        # --keep's reusability check looks at a VM that may already exist,
        # so it must find whichever host actually has it (locate_kvm_host),
        # not resource-select a fresh one (resolve_kvm_host, used below by
        # provision_vm() for genuinely new placement).
        try:
            keep_remote_host, keep_virt_srv = lc.locate_kvm_host(definition, vm_name, config)
        except SystemExit:
            keep_remote_host, keep_virt_srv = None, None
        if keep and keep_virt_srv and lc.vm_is_reusable(
                keep_virt_srv, vm_name, env.get("mymac", ""), env.get("myip", ""),
                remote_host=keep_remote_host):
            lc.log("  Skipping \"{}{}{}\" — existing VM matches definition".format(lc._RED, vm_name, lc._RESET))
            continue

        lc.purge_known_host(vm_name)
        # bash's `destroy_vm.sh "${inputFile}" "${_vm_name}"` here has no `||`
        # error check — a failed/no-op destroy (e.g. the VM never existed on
        # a first run) must NOT stop the pipeline. Mirror that explicitly,
        # since the python destroy_vm() raises on real ssh/virsh failures.
        try:
            destroy_vm(definition, config, defaults, vm_name)
        except SystemExit:
            pass
        except RuntimeError as e:
            lc.warn("destroy before recreate failed for '{}' (continuing): {}".format(vm_name, e))

        # A single node's boot-wait timing out (check_ssh_conn's own die(),
        # inside provision_vm()) must not abort the whole multi-node deploy —
        # reported live 2026-09-01: one slow/failed node ("ERROR: retry
        # limit ( 100 ) exceeded waiting for X to boot.") killed the entire
        # run instead of continuing with the rest. Mirrors the destroy_vm()
        # error handling just above: log and move on to the next node.
        try:
            provision_vm(definition, config, defaults, vm_name)
        except SystemExit:
            lc.warn("provisioning '{}' failed (continuing with the remaining nodes)".format(vm_name))
        except RuntimeError as e:
            lc.warn("provisioning '{}' failed (continuing with the remaining nodes): {}".format(vm_name, e))
    lc._level -= 1


def phase_reboot_and_wait_kept_nodes(definition, config, keep):
    # bash gates both of these loops on the GLOBAL --keep flag alone (not on
    # whether any given node was actually reused vs. recreated in phase 2) —
    # matched literally here, even though it means a just-recreated node
    # (which setup_vm.py already rebooted once) gets rebooted again.
    if not keep:
        return

    lc.log("Rebooting kept cluster nodes")
    lc._level += 1
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if not node_cfg.get("kcluster"):
            continue
        if targets.is_existing_node(node_cfg):
            # Not a VM this tool owns — nothing to reboot via libvirt.
            continue
        lc.log("Restart node {}{}{} (cluster {}{}{})".format(
            lc._RED, vm_name, lc._RESET, lc._RED, node_cfg["kcluster"], lc._RESET))
        # These are existing (kept) VMs — locate_kvm_host(), not resolve_kvm_host().
        remote_host, virt_srv = lc.locate_kvm_host(definition, vm_name, config)
        lc.reboot_vm(virt_srv, vm_name, remote_host=remote_host)
    lc._level -= 1

    time.sleep(5)
    lc.log("Waiting for cluster nodes to come back online")
    lc._level += 1
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if not node_cfg.get("kcluster"):
            continue
        lc.check_ssh_conn(vm_name)
    lc._level -= 1


def _install_k8s_on_cluster(definition, clu_name, clu_type, clu_cfg):
    lc.log("Installing \"{}{}{}\" cluster \"{}{}{}\"".format(
        lc._RED, clu_type, lc._RESET, lc._RED, clu_name, lc._RESET))
    lc._level += 1
    distro = k8s.get_distro(clu_type)
    token = None
    rancher1_ip = None
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        if node_cfg.get("kcluster") != clu_name:
            continue
        if node_cfg.get("INSTALL_RKE2_TYPE", "server") == "agent":
            token, rancher1_ip = distro.install_agent(vm_name, clu_name, clu_cfg, token, rancher1_ip)
        else:
            token, rancher1_ip = distro.install_server(vm_name, clu_name, clu_cfg, token=token, rancher1_ip=rancher1_ip)
    lc._level -= 1


def _install_cluster_addons(definition, config, defaults, json_file, clu_name, clu_cfg):
    addons = clu_cfg.get("addons", [])
    if not addons:
        lc.log("No Kubernetes cluster addons for \"{}{}{}\"".format(lc._RED, clu_name, lc._RESET))
        return

    mgm_node = clu_cfg.get("mgm_node", "")
    vm_name = mgm_node
    if not vm_name:
        for name, node_cfg in definition.get("nodes", {}).items():
            if node_cfg.get("kcluster") == clu_name:
                vm_name = name
                break

    clu_type = clu_cfg.get("clu_type", "")

    lc.log("Installing cluster \"{}{}{}\" addon/s ( {} ) from \"{}{}{}\"".format(
        lc._RED, clu_name, lc._RESET, " ".join(addons), lc._RED, vm_name, lc._RESET))
    lc._level += 1
    installed = set()
    for addon in addons:
        if addon in installed:
            continue
        installer = shutil.which("install_{}".format(addon))
        if not installer:
            lc.die("FAILED! Addon script \"install_{}\" not found".format(addon))
        apps.check_requirements(apps.load_plugin(addon), targets.TARGET_CONTAINER, clu_type=clu_type)
        lc.log("Running addon \"{}{}{}\" on \"{}{}{}\" for cluster \"{}{}{}\"".format(
            lc._RED, addon, lc._RESET, lc._RED, vm_name, lc._RESET, lc._RED, clu_name, lc._RESET))
        env = dict(os.environ)
        env["_vm_name"] = vm_name
        env["clu_name"] = clu_name
        # bash never checks this call's exit code (no `||` on either addon
        # invocation in setup_lab.sh) — a failing addon does not stop the
        # pipeline. Matched exactly: run it, ignore the result, move on.
        subprocess.run([installer, json_file], env=env)
        installed.add(addon)
        lc.log("Installed addon \"{}{}{}\" on cluster \"{}{}{}\"".format(
            lc._RED, addon, lc._RESET, lc._RED, clu_name, lc._RESET))
    lc._level -= 1
    # bash prints its "No more addons" message from inside the per-addon loop
    # (bash:209), so it fires after every addon rather than once at the end —
    # clearly a misplaced statement, not intentional per-addon behaviour.
    # Fixed here to print once, after all of this cluster's addons are done.
    lc.log("No more addons for cluster \"{}{}{}\"".format(lc._RED, clu_name, lc._RESET))


def phase_install_k8s_and_addons(definition, config, defaults, json_file):
    delay_min = int(definition.get("common", {}).get("delay_min", defaults.get("delay_min", 2)))
    for clu_name in k8s.list_kclusters(definition):
        clu_cfg = k8s.load_kclu_vars(definition, clu_name)
        clu_type = clu_cfg.get("clu_type", "")

        _install_k8s_on_cluster(definition, clu_name, clu_type, clu_cfg)

        total_wait = 2 + delay_min
        lc.log("Wait {} min for cluster \"{}{}{}\" to stabilise".format(total_wait, lc._RED, clu_name, lc._RESET))
        time.sleep(60 * total_wait)

        _install_cluster_addons(definition, config, defaults, json_file, clu_name, clu_cfg)


def phase_vm_addons(definition, json_file):
    for vm_name, node_cfg in definition.get("nodes", {}).items():
        addons = node_cfg.get("addons", [])
        if not addons:
            continue
        lc.log("Installing VM \"{}{}{}\" addons".format(lc._RED, vm_name, lc._RESET))
        lc._level += 1
        node_target = targets.node_kind(definition, vm_name)
        for addon in addons:
            installer = shutil.which("install_{}".format(addon))
            if not installer:
                lc.die("Addon script \"install_{}\" not found".format(addon))
            apps.check_requirements(apps.load_plugin(addon), node_target)
            lc.log("Running addon \"{}{}{}\" on \"{}{}{}\"".format(
                lc._RED, addon, lc._RESET, lc._RED, vm_name, lc._RESET))
            env = dict(os.environ)
            env["_vm_name"] = vm_name
            # Same as the cluster-addon loop: bash never checks this call's
            # exit code either, so a failing addon must not stop the pipeline.
            subprocess.run([installer, json_file], env=env)
        lc._level -= 1


def setup_lab(definition, config, defaults, json_file, keep=False):
    lab_name = definition.get("common", {}).get("lab_name") or Path(json_file).name
    has_k8s = bool(definition.get("kclusters"))
    kind = "VMs + Kubernetes clusters" if has_k8s else "VMs only"
    lc.log("\nSetup lab \"{}{}{}\" ({})".format(lc._RED, lab_name, lc._RESET, kind))

    remote_dns_servers = config.get("REMOTE_DNS_SERVERS", "").split() or None

    phase_services(definition, config, defaults)

    if has_k8s:
        phase_dns(definition, remote_dns_servers)

    phase_create_vms(definition, config, defaults, json_file, keep)

    if has_k8s:
        phase_reboot_and_wait_kept_nodes(definition, config, keep)
        phase_install_k8s_and_addons(definition, config, defaults, json_file)

    phase_vm_addons(definition, json_file)

    lc.log("LAB setup completed")


def main():
    args = sys.argv[1:]

    if args and args[0] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if args and args[0] == "--help":
        print(_HELP_TEXT)
        sys.exit(0)

    if args and args[0] in ("--input-definition", "--schema"):
        fmt = args[1] if len(args) > 1 else "json"
        sys.exit(subprocess.run(["lab_schema", "--base", fmt]).returncode)

    keep = "--keep" in args
    positional = [a for a in args if a != "--keep"]
    if not positional:
        lc.die("Usage: setup_lab.py [--keep] <lab.json>")
    json_file = positional[0]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    iso_loc        = defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")
    vm_img_loc     = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")
    if not lc.validate_lab_definition(definition, config, iso_loc, lab_setup_path, vm_img_loc=vm_img_loc):
        sys.exit(1)

    total_cpu, total_mem, total_disk = lc.total_lab_resources(definition)
    lc.log("This lab needs {} vCPU, {} MiB RAM, {} GiB disk in total across {} node(s)".format(
        total_cpu, total_mem, total_disk, len(definition.get("nodes", {}))))

    setup_lab(definition, config, defaults, json_file, keep=keep)


if __name__ == "__main__":
    main()
