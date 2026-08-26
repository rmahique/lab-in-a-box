#!/usr/bin/env python3
# Part of lab-in-a-box, it will destroy a VM
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalent of scripts/destroy_vm.sh — calls the python libraries
# directly, in-process. No bash is sourced or executed by this script.

"""
destroy_vm.py — destroy a single VM from a lab definition.

Usage:
    destroy_vm.py <lab.json> <vm_hostname>
"""

__version__ = "__LABVERSION__"

import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402
from lab_creation import load_vm_vars, del_from_dns, delete_vm, locate_kvm_host  # noqa: E402


def destroy_vm(definition, config, defaults, vm_name):
    """Destroy one VM: remove its DNS entries, then delete it. Mirrors destroy_vm.sh.

    Uses locate_kvm_host() (not resolve_kvm_host()) — this acts on a VM that
    may already exist, so it must find whichever host actually has it rather
    than resource-select a fresh one.
    """
    _, virt_srv = locate_kvm_host(definition, vm_name, config)
    env = {}
    env.update(defaults)
    env.update(config)
    env.update(load_vm_vars(definition, vm_name))

    del_from_dns(
        vm_name, env.get("myip", ""), env.get("mydomain", ""), env.get("mynet_reverse", ""),
        remote_dns_servers=env.get("REMOTE_DNS_SERVERS", "").split() or None,
    )
    delete_vm(virt_srv, vm_name)
    print('#\t\tVM "{}" destroyed\n'.format(vm_name))


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Usage:\n{} <configuration file> <vm_name>".format(sys.argv[0]))
        sys.exit(1)

    json_file = sys.argv[1]
    vm_name = sys.argv[2]

    defaults = primary.load_defaults()
    config = primary.load_config()
    definition = primary.load_definition(json_file)

    destroy_vm(definition, config, defaults, vm_name)


if __name__ == "__main__":
    main()
