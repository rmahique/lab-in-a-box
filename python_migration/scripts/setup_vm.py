#!/usr/bin/env python3
# Part of lab-in-a-box, it will setup a VM
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/setup_vm.sh — calls the python libraries
# (lab_creation, k8s, primary) directly, in-process. No bash is sourced or
# executed by this script.

"""
setup_vm.py — provision a single VM from a lab definition.

Usage:
    setup_vm.py <lab.json> <vm_hostname>
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

# Installed location (mirrors bash's _lib_path=/usr/local/lib/lab_creation);
# fall back to the repo copy for local development.
for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
from lab_creation import (  # noqa: E402
    die, log, warn,
    validate_lab_definition, load_vm_vars, resolve_kvm_host,
    check_or_generate_mac, copy_vm_image,
    prepare_ignition_combustion, prepare_cloud_init,
    prepare_virt_customize_for_vm, prepare_install_iso,
    copy_to_hypervisor, add_to_dns,
    create_vm, clean_ssh_keys, check_ssh_conn, reboot_vm,
)


def provision_vm(definition, config, defaults, json_file, vm_name):
    """
    Provision one VM. Mirrors setup_vm.sh end to end.

    definition : the loaded lab JSON/YAML dict.
    config     : lab_creation.cfg dict (REMOTE_HOST, VIRT_SRV, KVM_HOSTS, ROOT_SSH_KEY, …).
    defaults   : lab_creation.defaults dict (LAB_SETUP_PATH, VM_IMG_LOC, ISO_LOC, …).
    json_file  : path to the JSON file on disk (needed for validate/check_or_generate_mac,
                 which may rewrite it in place on a MAC conflict).
    vm_name    : the node key in definition["nodes"] to provision.
    """
    iso_loc        = defaults.get("ISO_LOC", "/var/lib/libvirt/images/sources")
    lab_setup_path = defaults.get("LAB_SETUP_PATH", "/srv/www/htdocs/lab_creation")
    vm_img_loc     = defaults.get("VM_IMG_LOC", "/var/lib/libvirt/images/").rstrip("/")

    # New VM placement — explicit nodes[vm_name].kvm_host override, else
    # resource-based selection across KVM_HOSTS, else the sole configured
    # host (see resolve_kvm_host() docstring in lab_creation.py).
    remote_host, virt_srv = resolve_kvm_host(definition, vm_name, config, vm_img_loc)

    if not validate_lab_definition(json_file, config, iso_loc, lab_setup_path,
                                    target_node=vm_name, vm_img_loc=vm_img_loc):
        sys.exit(1)

    # bash: defaults, then cfg, then JSON (load_vm_vars) last — JSON wins on
    # any name collision, since it's sourced/exported last in that pipeline.
    env = {}
    env.update(defaults)
    env.update(config)
    env.update(load_vm_vars(definition, vm_name))

    ign_file = "{}.ign".format(vm_name)
    com_file = vm_name

    mymac, network = check_or_generate_mac(
        virt_srv, vm_name, env.get("mymac", ""), json_file,
        bridge=env.get("BRIDGE", "br0"),
        vm_net_model=env.get("VM_NET_MODEL", "virtio"),
    )
    env["mymac"] = mymac

    config_method = env.get("config_method", "") or ""

    copy_vm_image(remote_host, iso_loc, env.get("ISO_IMAGE", ""), vm_img_loc, vm_name,
                   env.get("VM_DSK", ""), config_method=config_method)

    if config_method == "":
        prepare_ignition_combustion(
            vm_name, lab_setup_path,
            env.get("ROOT_PWD_HASH", ""), env.get("ROOT_SSH_KEY", ""),
            env.get("mysource", ""), env.get("sourcepath", ""),
            env.get("mydns", ""), env.get("myip", ""), env.get("mymask", ""), env.get("mygw", ""),
            env.get("SUSE_email", ""), env.get("SUSE_regcode", ""), env.get("SUSE_url", ""),
        )
    elif config_method == "cloud-init":
        prepare_cloud_init(vm_name, lab_setup_path, env)
    elif config_method == "virt_customize":
        prepare_virt_customize_for_vm(
            remote_host, vm_img_loc, vm_name,
            env.get("myip", ""), env.get("mymask", ""), env.get("mygw", ""),
            env.get("mydns", ""), env.get("mydomain", ""), mymac,
            vm_root_pass=env.get("VM_ROOT_PASS"), root_pwd_hash=env.get("ROOT_PWD_HASH"),
            root_ssh_key_path=env.get("ROOT_SSH_KEY"),
        )
    elif config_method == "install_iso":
        prepare_install_iso(
            vm_name, lab_setup_path, env.get("install_type", ""), env.get("ISO_IMAGE", ""),
            mymac, env.get("myip", ""), env.get("mymask", ""), env.get("mygw", ""),
            env.get("mydns", ""), env.get("mydomain", ""),
            env.get("ROOT_PWD_HASH", ""), root_ssh_key=env.get("ROOT_SSH_KEY"),
        )
    # else: "iso-cloud-init" — nothing to prepare (mirrors bash: no prepare_* branch for it)

    copy_to_hypervisor(remote_host, lab_setup_path, vm_name, config_method=config_method,
                        vm_img_loc=vm_img_loc)

    add_to_dns(vm_name, env.get("myip", ""), env.get("mydomain", ""), env.get("mynet_reverse", ""),
               remote_dns_servers=env.get("REMOTE_DNS_SERVERS", "").split() or None)

    create_vm(
        virt_srv, vm_name,
        env.get("VM_CPU", ""), env.get("VM_MEM", ""), env.get("VM_DSK", ""),
        vm_img_loc, network, remote_host,
        os_variant=env.get("VM_OSVARIANT", "slem5.4"),
        boot=env.get("VM_BOOT", "uefi"),
        config_method=config_method,
        lab_setup_path=lab_setup_path,
        extra_disks=env.get("extra_dsk", "").split() or None,
        extra_filesystems=env.get("extra_fs", "").split() or None,
        vm_dsk_bus=env.get("VM_DSK_BUS", "virtio"),
        ign_file=ign_file, com_file=com_file,
        salt_states=env.get("salt_states", ""),
        install_type=env.get("install_type", ""), iso_image=env.get("ISO_IMAGE", ""),
        iso_loc=iso_loc, mydns=env.get("mydns", ""),
        vcluster=env.get("vcluster", ""),
    )

    clean_ssh_keys(vm_name, env.get("myip", ""))

    # Wait for it to come online, reboot, then wait again — matches bash
    # exactly. check_ssh_conn() already dies internally on timeout (mirrors
    # fail_with_error inside bash's check_ssh_conn), so — as in bash — the
    # "VM failed to come online" messages there are effectively unreachable;
    # any real timeout aborts from inside check_ssh_conn itself.
    check_ssh_conn(vm_name)
    reboot_vm(virt_srv, vm_name)
    check_ssh_conn(vm_name)
    log("\t\tVM \"{}\" created".format(vm_name))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print(
            "Usage: setup_vm.py <lab.json> <vm_hostname>\n\n"
            "Provisions a single VM: copies the disk image, generates provisioning files\n"
            "(Ignition+Combustion or cloud-init), registers DNS, and calls virt-install.\n\n"
            "Run 'setup_lab.py --input-definition [json|yaml]' for the full lab definition schema."
        )
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] in ("--input-definition", "--schema"):
        import subprocess
        fmt = sys.argv[2] if len(sys.argv) > 2 else "json"
        sys.exit(subprocess.run(["setup_lab.py", "--input-definition", fmt]).returncode)

    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if len(sys.argv) < 3:
        die("Usage:\n{} <configuration file> <vm_name>".format(sys.argv[0]))

    json_file = sys.argv[1]
    vm_name = sys.argv[2]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    provision_vm(definition, config, defaults, json_file, vm_name)


if __name__ == "__main__":
    main()
