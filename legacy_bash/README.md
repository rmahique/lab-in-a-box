# legacy_bash

Retired as of the `python_migration` cutover (2026-08-28). These are the pre-cutover bash
addon scripts, bash orchestration scripts, the old (architecturally different) repo-root
`setup_lab.py` YAML-to-JSON wrapper, and three stale pre-Phase4/5 `.py` library forks
(`lab_creation.py`, `k8s.py`, `primary.py`) that `libs/` carried alongside the bash
originals. None of this is deployed or referenced by `install_automation_node_scripts.sh`
any more — kept here for reference/rollback only, not as a fallback path.

The live equivalents now live in this repo's own top-level `scripts/` and `libs/`
(`python_migration/` was renamed away once the migration finished — see commit `ca2d2d5`).
See `/root/lab-in-a-box_rmahique/TODO` for the cutover's staging, verification, and open risks.

Not moved here, and still live: `scripts/install_ds389` (never ported — broken in bash
too), `scripts/lab_schema`, `scripts/pushDockerImage.sh`, `scripts/refresh_hypervisor_status.py`,
`scripts/dev_sync_nucs.sh`, `scripts/download_latest_helm.sh`, and `libs/{lab_creation.bash,
k8s_functions.bash,primary_functions.bash,extensions.sh,__init__.py}` (the bash helpers
`install_ds389` still needs).

**`setup_demo_server/setup_kvm_node.sh` (added here 2026-08-31, belatedly):** this one missed
the original 2026-08-28 sweep entirely — it lived in `setup_demo_server/`, outside that sweep's
`scripts/`/`libs/` scope, so it sat there dead and un-synced with its already-existing Python
replacement (`setup_kvm_node.py`, which the real install path — `install_demo_server_scripts.sh`
— has pointed users to the whole time). Confirmed dead via `git log`: its last real commit
predates the migration; `setup_kvm_node.py`'s did not. Anyone who'd followed README.md's own
stale `bash setup_kvm_node.sh <IP>` instruction instead of the installer's `./setup_kvm_node.py
<IP>` would have silently gotten the old bash implementation — same package list, but missing
every feature added to the Python version since (the opt-in `_network_mode=nat` support, most
recently). README.md fixed to match the installer.
