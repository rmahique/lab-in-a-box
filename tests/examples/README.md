# README example deploy+check tests

Every example in the main [README.md](../../README.md#examples) has a
matching lab definition here (`<name>.json`) and a post-deploy check
(`checks/<name>_check.py`) that confirms the deploy actually *works* — a
cluster that's Ready, an addon that actually deployed, a VM that's really
reachable — not just that `setup_lab.py` exited 0.

## Why these aren't part of `tests/run_tests.sh`

`tests/run_tests.sh` only auto-discovers `tests/checks/*.sh`, each running
mocked-SSH unit tests in a disposable container — no real hardware, fast,
safe to run on every commit. These are the opposite: they deploy real VMs
against a real KVM hypervisor and a real automation VM stack
(`lab_creation.cfg`/`.defaults`, DNS, a source QCOW2/ISO already on the
hypervisor, the works). They're **present, but never auto-run** — run them
by hand, on the automation VM, before pushing a change that could affect
one of these documented flows.

## Running one

```shell
tests/examples/run_example.sh <name>
```

`<name>` is one of: `standalone`, `rancher-cluster`, `multi-host`,
`uyuni-lab`, `legacy`.

```shell
tests/examples/run_example.sh standalone
tests/examples/run_example.sh rancher-cluster --keep     # skip VMs already up
tests/examples/run_example.sh legacy --no-destroy         # leave it up after a pass, to poke at it
```

On success the lab is destroyed automatically (unless `--no-destroy`). On
failure it's always left up, so you can SSH in and see what actually
happened — the script tells you the `destroy_lab.py` command to clean up
once you're done.

`multi-host` additionally needs `KVM_HOSTS` already configured in
`/etc/lab_creation.cfg` (see the README's
[Multi-host labs](../../README.md#multi-host-labs) section) with at least
two real hosts, one of them `hv1.mydemo.lab`.

## Adding a check for a new documented example

1. Add `<name>.json` here — copy the example verbatim from the README.
2. Add `checks/<name>_check.py` — import from `checks/_common.py`
   (`require_ssh_reachable`, `require`, `require_all_nodes_ready`,
   `require_helm_release`, or a new helper if the example needs one) and
   raise `CheckFailure` with a clear message on anything that doesn't hold.
3. `tests/checks/33_examples_sync_test.py` (a normal, auto-run, no-hardware
   check) fails if this directory's `.json` files drift from what's
   actually embedded in the README — keep both in sync by construction, not
   by remembering to.
