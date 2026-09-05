#!/usr/bin/env python3
# Pure-logic unit tests for libs/lab_usb.py — no real VM/SSH needed for any
# of this, it's all in-memory data transformation. Run from 32_lab_usb.sh,
# in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_usb  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── remap_lab_definition_to_nat: core remapping ──────────────────────────────
original = {
    "common": {"ISO_IMAGE": "img.qcow2", "VM_MEM": "4096", "VM_CPU": "2", "VM_DSK": "40",
               "mygw": "192.168.88.1", "mymask": "24", "mydns": "192.168.88.73",
               "mydomain": "mydemo.lab"},
    "nodes": {
        "srv1.mydemo.lab": {"myip": "192.168.88.101", "mymac": "34:8a:b1:4b:1a:c1"},
        "agent1.mydemo.lab": {"myip": "192.168.88.102", "mymac": "34:8a:b1:4b:1a:c2"},
    },
}

remapped = lab_usb.remap_lab_definition_to_nat(
    original, nat_cidr="192.168.150.0/24", nested_automation_ip="192.168.150.2")

check("remap: never mutates the original definition in place",
      original["nodes"]["srv1.mydemo.lab"]["myip"] == "192.168.88.101"
      and original["common"]["mygw"] == "192.168.88.1")

check("remap: every node gets a new myip inside the NAT range",
      all(n["myip"].startswith("192.168.150.") for n in remapped["nodes"].values()))
check("remap: node order is preserved (deterministic remapping, srv1 before agent1)",
      remapped["nodes"]["srv1.mydemo.lab"]["myip"] < remapped["nodes"]["agent1.mydemo.lab"]["myip"])
check("remap: node addresses start at the given offset (default 10), not the network address itself",
      remapped["nodes"]["srv1.mydemo.lab"]["myip"] == "192.168.150.10")
check("remap: distinct nodes get distinct addresses",
      remapped["nodes"]["srv1.mydemo.lab"]["myip"] != remapped["nodes"]["agent1.mydemo.lab"]["myip"])

check("remap: mymac is left untouched — a MAC doesn't need to be 'in range'",
      remapped["nodes"]["srv1.mydemo.lab"]["mymac"] == "34:8a:b1:4b:1a:c1")

check("remap: common.mygw becomes the NAT network's own gateway (first usable host)",
      remapped["common"]["mygw"] == "192.168.150.1")
check("remap: common.mymask reflects the NAT network's own prefix length",
      remapped["common"]["mymask"] == "24")
check("remap: common.mydns points at the nested automation VM's own static IP",
      remapped["common"]["mydns"] == "192.168.150.2")

check("remap: other common fields (ISO_IMAGE, VM_MEM, …) pass through unchanged",
      remapped["common"]["ISO_IMAGE"] == "img.qcow2" and remapped["common"]["VM_MEM"] == "4096")

# A custom start_offset shifts where remapped addresses begin.
remapped2 = lab_usb.remap_lab_definition_to_nat(
    original, nat_cidr="192.168.150.0/24", nested_automation_ip="192.168.150.2", start_offset=50)
check("remap: a custom start_offset is honored",
      remapped2["nodes"]["srv1.mydemo.lab"]["myip"] == "192.168.150.50")

# nested_automation_ip is mandatory — this whole scheme depends on knowing
# where the lab's own DNS server lives, there is no safe default to guess.
raised = False
try:
    lab_usb.remap_lab_definition_to_nat(original, nat_cidr="192.168.150.0/24")
except ValueError:
    raised = True
check("remap: dies with a clear error when nested_automation_ip is omitted", raised)

# A NAT range too small for the lab is a clear error, not silent truncation
# or an IndexError.
big_lab = {
    "common": {},
    "nodes": {"vm{}".format(i): {"myip": "10.0.0.{}".format(i)} for i in range(1, 6)},
}
raised = False
try:
    lab_usb.remap_lab_definition_to_nat(
        big_lab, nat_cidr="192.168.150.0/30", nested_automation_ip="192.168.150.2")
except ValueError:
    raised = True
check("remap: a NAT range too small for the lab raises a clear ValueError, not an IndexError", raised)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab_usb checks passed")
