#!/usr/bin/env python3
# Unit tests for libs/backends.py's AWSBackend — every `aws` CLI call
# (subprocess.run) is mocked (no real AWS account available anywhere in this
# environment, and no `aws` CLI needs to even be installed to run these).
# Asserts command construction, MAC/image handling, config_method
# enforcement, and instance-type sizing — not real API behavior; see
# AWSBackend's own docstring for exactly what remains unverified. Run from
# 35_aws_backend.sh, in its own container — see tests/run_tests.sh.
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import backends  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _cp(rc, stdout="", stderr=""):
    import subprocess
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


# ── resolve(): AWS_REGION + one of the two credential shapes are mandatory ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.AWSBackend.resolve({}, "vm1", {}, False)
    except SystemExit:
        pass
check("resolve() dies without AWS_REGION", any("AWS_REGION" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.AWSBackend.resolve({}, "vm1", {"AWS_REGION": "eu-central-1"}, False)
    except SystemExit:
        pass
check("resolve() dies with a region but no credentials at all",
      any("AWS_PROFILE" in m for m in died))

resolved = backends.AWSBackend.resolve(
    {}, "vm1", {"AWS_REGION": "eu-central-1", "AWS_PROFILE": "lab"}, False)
check("resolve() accepts AWS_PROFILE alone", resolved.profile == "lab")
check("resolve() picks up the region", resolved.region == "eu-central-1")

resolved = backends.AWSBackend.resolve(
    {}, "vm1",
    {"AWS_REGION": "eu-central-1", "AWS_ACCESS_KEY_ID": "AKIA...", "AWS_SECRET_ACCESS_KEY": "secret"},
    False)
check("resolve() accepts an access/secret key pair without a profile", resolved.access_key == "AKIA...")

# ── resolve(): a temporary/STS ("ASIA...") access key requires AWS_SESSION_TOKEN ──
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backends.AWSBackend.resolve(
            {}, "vm1",
            {"AWS_REGION": "eu-central-1", "AWS_ACCESS_KEY_ID": "ASIA...", "AWS_SECRET_ACCESS_KEY": "secret"},
            False)
    except SystemExit:
        pass
check("resolve() dies on an ASIA-prefixed key with no AWS_SESSION_TOKEN",
      any("AWS_SESSION_TOKEN" in m for m in died))

resolved = backends.AWSBackend.resolve(
    {}, "vm1",
    {"AWS_REGION": "eu-central-1", "AWS_ACCESS_KEY_ID": "ASIA...", "AWS_SECRET_ACCESS_KEY": "secret",
     "AWS_SESSION_TOKEN": "tok123"},
    False)
check("resolve() accepts an ASIA-prefixed key when AWS_SESSION_TOKEN is also set",
      resolved.session_token == "tok123")

resolved = backends.AWSBackend.resolve(
    {}, "vm1",
    {"AWS_REGION": "eu-central-1", "AWS_PROFILE": "lab", "AWS_SUBNET_ID": "subnet-1",
     "AWS_SECURITY_GROUP_ID": "sg-1", "AWS_KEY_NAME": "labkey"},
    False)
check("resolve() picks up optional networking/key fields",
      (resolved.subnet_id, resolved.security_group_id, resolved.key_name) == ("subnet-1", "sg-1", "labkey"))


backend = backends.AWSBackend("eu-central-1", profile="lab")


# ── config_method / ISO_IMAGE enforcement (same shape as the other backends) ─
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("ami-123", "vm1", 40, config_method="")
    except SystemExit:
        pass
check("copy_vm_image() dies on config_method != cloud-init", any("cloud-init" in m for m in died))

died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend.copy_vm_image("", "vm1", 40, config_method="cloud-init")
    except SystemExit:
        pass
check("copy_vm_image() dies on an empty ISO_IMAGE", any("AMI" in m for m in died))

ok = [False]
with mock.patch.object(backends, "die", side_effect=AssertionError("should not die")):
    backend.copy_vm_image("ami-0123456789abcdef0", "vm1", 40, config_method="cloud-init")
    ok[0] = True
check("copy_vm_image() accepts a real AMI ID with config_method=cloud-init", ok[0])


# ── push_provisioning_files(): stashes user-data for create_vm() to read ──
with tempfile.TemporaryDirectory() as tempfile_dir:
    cloud_init_dir = Path(tempfile_dir) / "cloud-init"
    cloud_init_dir.mkdir()
    (cloud_init_dir / "vm1_user-data").write_text("#cloud-config\nhostname: vm1\n")
    b2 = backends.AWSBackend("eu-central-1", profile="lab", lab_setup_path=tempfile_dir)
    b2.push_provisioning_files("vm1", config_method="cloud-init")
    check("push_provisioning_files() stashes the real file content",
          b2._user_data_by_vm.get("vm1") == "#cloud-config\nhostname: vm1\n")


# ── list_used_macs() / check_or_generate_mac(): no MAC concept on EC2 ─────
check("list_used_macs() returns empty (EC2 has no MAC concept this backend uses)",
      backend.list_used_macs() == ([], {}))
# _cloud_no_mac(): dropped 2026-09-09 — no MAC concept, no generation, pure passthrough
mymac, network = backend.check_or_generate_mac("vm1", "", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() does NOT generate a MAC when none was given (nothing to generate for)",
      mymac == "" and network is None)
mymac, network = backend.check_or_generate_mac("vm1", "aa:bb:cc:dd:ee:ff", {"nodes": {"vm1": {}}})
check("check_or_generate_mac() passes an existing mymac through unchanged (never sent to AWS)",
      mymac == "aa:bb:cc:dd:ee:ff" and network is None)


# ── _pick_instance_type(): smallest SKU that satisfies both cores and memory ─
check("_pick_instance_type() picks the smallest sufficient SKU (2 vCPU / 2048 MiB)",
      backend._pick_instance_type(2, 2048, "vm1") == "t3.medium")
check("_pick_instance_type() steps up when memory needs more than cores would suggest",
      backend._pick_instance_type(2, 16384, "vm1") == "t3.xlarge")
died = []
with mock.patch.object(backends, "die", side_effect=lambda msg: died.append(msg) or (_ for _ in ()).throw(SystemExit)):
    try:
        backend._pick_instance_type(999, 999999, "vm1")
    except SystemExit:
        pass
check("_pick_instance_type() dies clearly when nothing in the table is big enough",
      any("INSTANCE_TYPES" in m for m in died))


# ── vm_exists() / _find_instance(): describe-instances with the Name-tag filter ─
_describe_none = _cp(0, stdout=json.dumps({"Reservations": []}))
_describe_one = _cp(0, stdout=json.dumps(
    {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}]}))

with mock.patch.object(backends.subprocess, "run", return_value=_describe_one) as m_run:
    check("vm_exists() returns True when describe-instances lists a match", backend.vm_exists("vm1") is True)
    called_args = m_run.call_args[0][0]
    check("vm_exists() filters by the Name tag", any("tag:Name,Values=vm1" in a for a in called_args))

with mock.patch.object(backends.subprocess, "run", return_value=_describe_none):
    check("vm_exists() returns False when describe-instances lists nothing", backend.vm_exists("vm1") is False)


# ── get_ip(): prefers PublicIpAddress, falls back to PrivateIpAddress, None if unassigned ──
_with_public = _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [
    {"InstanceId": "i-1", "PublicIpAddress": "203.0.113.10", "PrivateIpAddress": "10.0.0.5"}]}]}))
_private_only = _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [
    {"InstanceId": "i-1", "PrivateIpAddress": "10.0.0.5"}]}]}))
_no_ip_yet = _cp(0, stdout=json.dumps({"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}))

with mock.patch.object(backends.subprocess, "run", return_value=_with_public):
    check("get_ip() prefers the public IP when both are assigned", backend.get_ip("vm1") == "203.0.113.10")
with mock.patch.object(backends.subprocess, "run", return_value=_private_only):
    check("get_ip() falls back to the private IP when no public one is assigned",
          backend.get_ip("vm1") == "10.0.0.5")
with mock.patch.object(backends.subprocess, "run", return_value=_no_ip_yet):
    check("get_ip() returns None while the instance has no IP yet (still Pending)",
          backend.get_ip("vm1") is None)
with mock.patch.object(backends.subprocess, "run", return_value=_describe_none):
    check("get_ip() returns None when the instance doesn't exist at all", backend.get_ip("vm1") is None)


# ── delete_vm(): idempotent when the instance is already gone ─────────────
with mock.patch.object(backends.subprocess, "run", return_value=_describe_none) as m_run:
    backend.delete_vm("vm1")  # must not raise/die
    check("delete_vm() only calls describe-instances (no terminate) when nothing exists",
          m_run.call_count == 1)


# ── create_vm(): real command construction, incl. the root-device lookup ──
b3 = backends.AWSBackend("eu-central-1", profile="lab")
b3._user_data_by_vm["vm1"] = "#cloud-config\n"
calls = []


def _fake_run(args, **kwargs):
    calls.append(args)
    if "describe-images" in args:
        return _cp(0, stdout=json.dumps({"Images": [{"RootDeviceName": "/dev/sda1"}]}))
    if "describe-instances" in args:
        # Serves get_ip()'s post-create poll (create_vm() calls it via _poll_for_ip) — a real
        # PublicIpAddress here so the poll succeeds on its first check, not a 180s timeout.
        return _cp(0, stdout=json.dumps(
            {"Reservations": [{"Instances": [{"InstanceId": "i-new", "PublicIpAddress": "203.0.113.10"}]}]}))
    if "run-instances" in args:
        return _cp(0, stdout=json.dumps({"Instances": [{"InstanceId": "i-new"}]}))
    return _cp(0, stdout="")


with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
    returned_ip = b3.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init",
                                iso_image="ami-0123456789abcdef0")

check("create_vm() returns the real IP once the instance is confirmed running (2026-09-09 "
      "contract — see VMBackend.create_vm()'s own docstring)", returned_ip == "203.0.113.10")

run_instances_call = next(c for c in calls if "run-instances" in c)
check("create_vm() looks up the AMI's real root device name before building block-device-mappings",
      any("describe-images" in c for c in calls))
check("create_vm() picks a real instance type", "t3.medium" in run_instances_call)
check("create_vm() uses the real root device name from describe-images, not a hardcoded default",
      json.loads(run_instances_call[run_instances_call.index("--block-device-mappings") + 1])[0]["DeviceName"]
      == "/dev/sda1")
check("create_vm() sends the stashed user-data", "#cloud-config\n" in run_instances_call)
check("create_vm() tags the instance with a real Name tag",
      any("Key=Name,Value=vm1" in a for a in run_instances_call))
check("create_vm() omits subnet/security-group/key-name flags when none were configured",
      "--subnet-id" not in run_instances_call and "--key-name" not in run_instances_call)

b4 = backends.AWSBackend("eu-central-1", profile="lab", subnet_id="subnet-1",
                          security_group_id="sg-1", key_name="labkey")
b4._user_data_by_vm["vm1"] = ""
calls = []
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run):
    b4.create_vm("vm1", 2, 4096, 40, None, config_method="cloud-init", iso_image="ami-0123456789abcdef0")
run_instances_call = next(c for c in calls if "run-instances" in c)
check("create_vm() includes subnet/security-group/key-name when configured",
      "subnet-1" in run_instances_call and "sg-1" in run_instances_call and "labkey" in run_instances_call)


# ── _aws(): AWS_SESSION_TOKEN is passed through the subprocess env when set ─
b5 = backends.AWSBackend("eu-central-1", access_key="ASIA...", secret_key="secret", session_token="tok123")
seen_env = {}


def _fake_run_capture_env(args, env=None, **kwargs):
    seen_env.update(env or {})
    return _cp(0, stdout=json.dumps({"Reservations": []}))


with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_capture_env):
    b5.vm_exists("vm1")
check("_aws() passes AWS_SESSION_TOKEN through to the subprocess env when set",
      seen_env.get("AWS_SESSION_TOKEN") == "tok123")

b6 = backends.AWSBackend("eu-central-1", access_key="AKIA...", secret_key="secret")
seen_env = {}
with mock.patch.object(backends.subprocess, "run", side_effect=_fake_run_capture_env):
    b6.vm_exists("vm1")
check("_aws() omits AWS_SESSION_TOKEN entirely for a long-lived key pair (none configured)",
      "AWS_SESSION_TOKEN" not in seen_env)


# ── host_resources(): a large constant, not a real capacity query ─────────
check("host_resources() returns a (cpu, mem_mb, disk_mb) tuple that never reads as 'no capacity'",
      backend.host_resources() == (9999, 999999, 999999))


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all aws_backend checks passed")
