#!/usr/bin/env python3
# Mocked unit tests for lab_creation.run_libvirt_tool() — the SSH-fallback
# path used when no local virsh/virt-install binary exists (this test
# container is exactly such an environment, so the "local" branch is
# forced via _has_local_binary for its own tests, mirroring
# 10_lab_creation_core_test.py). Run from 28_run_libvirt_tool_ssh_fallback.sh,
# in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation as lc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── local branch: behaves exactly like a direct subprocess.run() call ──────

calls = []


def fake_local_run(argv, **kwargs):
    calls.append((argv, kwargs))
    return FakeCompleted(returncode=0, stdout="local-ok")


lc._has_local_binary = lambda binary: True
lc.subprocess.run = fake_local_run

result = lc.run_libvirt_tool("virsh", "kvm1.mydemo.lab", "qemu+ssh://root@kvm1.mydemo.lab/system",
                              ["dominfo", "vm1"], capture_output=True, text=True)
check("local branch: calls subprocess.run with the local virsh argv",
      calls and calls[-1][0] == ["virsh", "--connect", "qemu+ssh://root@kvm1.mydemo.lab/system", "dominfo", "vm1"])
check("local branch: passes kwargs through unchanged",
      calls[-1][1].get("capture_output") is True and calls[-1][1].get("text") is True)
check("local branch: returns subprocess.run's own result", result.stdout == "local-ok")

# ── remote (SSH) branch: no local binary, falls back to ssh_run() ──────────

ssh_calls = []


def fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False):
    ssh_calls.append((hostname, cmd, check, capture))
    return FakeCompleted(returncode=0, stdout="remote-ok")


lc._has_local_binary = lambda binary: False
lc.ssh_run = fake_ssh_run
# backends.py imports ssh_run by name into its own module namespace, but
# run_libvirt_tool() itself is defined in lab_creation.py and calls the
# bare name `ssh_run(...)`, which resolves against THIS module's globals —
# patching lc.ssh_run is what actually takes effect here.

result = lc.run_libvirt_tool("virsh", "kvm1.mydemo.lab", "qemu+ssh://root@kvm1.mydemo.lab/system",
                              ["dominfo", "vm1"], stdout=None, stderr=None)
check("remote branch: SSHes to remote_host, not the (unused, ssh-embedded) virt_srv host",
      ssh_calls and ssh_calls[-1][0] == "kvm1.mydemo.lab")
check("remote branch: runs virsh against the hypervisor's own local socket, not a nested qemu+ssh URI",
      "virsh --connect qemu:///system dominfo vm1" == ssh_calls[-1][1])
check("remote branch: returns ssh_run's own result", result.stdout == "remote-ok")

ssh_calls.clear()
lc.run_libvirt_tool("virsh", "kvm1.mydemo.lab", "irrelevant", ["dominfo", "vm1"],
                     stdout=lc.subprocess.DEVNULL, stderr=lc.subprocess.DEVNULL, check=False)
check("remote branch: stdout=DEVNULL calls map to capture=False (fire-and-forget), not raise-on-nonzero",
      ssh_calls[-1][2] is False and ssh_calls[-1][3] is False)

ssh_calls.clear()
lc.run_libvirt_tool("virsh", "kvm1.mydemo.lab", "irrelevant", ["list", "--all", "--name"],
                     capture_output=True, text=True)
check("remote branch: capture_output=True maps to ssh_run's capture=True",
      ssh_calls[-1][3] is True)

# A value containing shell metacharacters must be safely quoted, not
# interpolated raw into the remote command string.
ssh_calls.clear()
lc.run_libvirt_tool("virsh", "kvm1.mydemo.lab", "irrelevant", ["dominfo", "vm; rm -rf /"])
check("remote branch: shell-unsafe args are quoted, not interpolated raw",
      "'vm; rm -rf /'" in ssh_calls[-1][1] or '"vm; rm -rf /"' in ssh_calls[-1][1])

# No remote_host at all and no local binary: a clear error, not a crash
# reaching for a None hostname.
lc.ssh_run = None  # would blow up loudly if called — it must not be
try:
    lc.run_libvirt_tool("virsh", None, "irrelevant", ["dominfo", "vm1"])
    check("remote branch with no remote_host: raises instead of silently doing nothing", False)
except RuntimeError as e:
    check("remote branch with no remote_host: raises a clear RuntimeError", "virsh" in str(e))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all run_libvirt_tool SSH-fallback checks passed")
