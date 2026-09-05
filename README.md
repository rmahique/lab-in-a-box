<a id="top"></a>
# lab-in-a-box

<p align="center">
  <img src="media/logo.png" width="180" alt="lab-in-a-box logo: nested glowing cubes inside a glass box, representing nested VMs inside a physical host" />
  <br/>
  <img src="media/logo-text.png" width="420" alt="lab-in-a-box wordmark" />
</p>

<p align="center">
  <a href="https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="License" src="https://img.shields.io/badge/license-GPLv3-blue.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11-blue.svg">
  <img alt="Tests" src="https://img.shields.io/badge/tests-containerized%20(podman)-success.svg">
  <img alt="Add-ons" src="https://img.shields.io/badge/add--ons-41-informational.svg">
</p>

<p align="center">
  <sub>🌐 <a href="README.md"><strong>English</strong></a> · <a href="README.es.md">Español</a> · <a href="README.de.md">Deutsch</a> · <a href="README.fr.md">Français</a> · <a href="README.pt-BR.md">Português (Brasil)</a> · <a href="README.ja.md">日本語</a> · <a href="README.zh-CN.md">简体中文</a></sub>
</p>

<p align="center"><em>Point it at a JSON or YAML file. Get back a working lab — VMs, DNS, Kubernetes, and add-ons, all wired up.</em></p>

<p align="center" float="left">
  <kbd><img src="media/NUC.jpg" width="400" alt="One of the NUCs used to develop and test this project." /></kbd>
</p>

**lab-in-a-box** turns a single bare-metal machine into a self-contained lab factory: point it at a JSON or YAML file describing the VMs, Kubernetes clusters, and software you want, and it builds the whole thing — DNS, provisioning, cluster bring-up, and add-ons — without you touching `virt-install` or Ansible by hand.

## Why lab-in-a-box?

<table>
<tr>
<td width="50%" valign="top">

`setup_lab.py` · **One JSON/YAML file, one command.**
Describe VMs, Kubernetes clusters (RKE2/K3s), and add-ons declaratively; it builds everything in the right order.

`install_<addon>` · **41 ready-made add-ons.**
Rancher, Longhorn, NeuVector, Harbor, Keycloak, Jenkins, Argo CD, SUSE Manager/Uyuni (activation keys, RBAC, Content Lifecycle Management, Ansible integration, and more), vulnerable demo apps for security training, and more.

[`lab-builder`](#web-ui-lab-builder) · **A dynamic web UI.**
Renders forms straight from the add-ons' own schemas — add a field to a script, and the UI picks it up with zero front-end changes.

</td>
<td width="50%" valign="top">

`KVM_HOSTS` · **Multi-hypervisor aware.**
One lab definition can spread VMs across several KVM hosts, auto-selected by free CPU/RAM/disk, or pinned per node.

`podman` · **Fully containerized test suite.**
Every check runs in its own disposable container, wired into a pre-commit hook.

`config_method` · **Pluggable provisioning.**
Ignition+Combustion (SLE Micro), cloud-init (openSUSE/Ubuntu), `virt-customize` (legacy distros with no cloud-init/Ignition support), or a scripted ISO install (AutoYaST/Kickstart/Preseed/AutoInstall).

</td>
</tr>
</table>

---

## Table of Contents

- [Architecture](#architecture)
- [How it works](#how-it-works)
- [Quick Start](#quick-start)
- [Web UI (lab-builder)](#web-ui-lab-builder)
- [Lab definition format](#lab-definition-format)
- [Examples](#examples)
- [Step-by-Step Walkthroughs](#step-by-step-walkthroughs)
- [Available commands](#available-commands)
- [Available addons](#available-addons)
- [Configuration reference](#configuration-reference)
- [Testing](#testing)
- [Contributing / Developer setup](#contributing--developer-setup)

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Architecture

<p align="center" float="left">
  <kbd><img src="media/diagram1.svg" width="800" alt="Architecture overview diagram"/></kbd>
  <kbd><img src="media/diagram2.svg" width="800" alt="Network and services diagram"/></kbd>
</p>

The system is built around a **two-tier architecture**:

```mermaid
graph TB
    Operator["Operator's client"] -->|"SSH / DNS / HTTP"| AutoVM
    subgraph HV["Hypervisor node(s) — KVM/QEMU"]
        AutoVM["Automation VM<br/>DNS · HTTP · scripts · web UI"]
        AutoVM -->|"virt-install / virsh"| VM1["Lab VM"]
        AutoVM -->|"virt-install / virsh"| VM2["Lab VM"]
        AutoVM -->|"virt-install / virsh"| VM3["Lab VM"]
    end
```

### Hypervisor node(s)

One or more physical/bare-metal machines running KVM/QEMU. Each hosts lab VMs and holds QCOW2 source images in `/var/lib/libvirt/images/sources/`. A NUC, a workstation, or any x86_64 machine capable of running KVM will do. Labs that need more capacity than one box can span **multiple KVM hosts** — see [multi-host labs](#multi-host-labs) below.

### Automation VM

A small VM running on the hypervisor that acts as the control plane for the entire lab. It provides:

- **DNS** — BIND (`named`) serves the lab domain and forwards external requests, so all lab hostnames resolve from any client pointing at it
- **HTTP** — serves provisioning files (Ignition, Combustion, cloud-init) at `/srv/www/htdocs/lab_creation/`
- **Scripts** — all lab management commands installed at `/usr/local/bin/`
- **Web UI** (optional) — [lab-builder](#web-ui-lab-builder), a browser-based lab.json designer

All user commands are run **on the automation VM**. It connects to the hypervisor(s) and to created VMs via SSH. No direct hypervisor access is needed after initial setup.

### Under the hood

The command-line tools and every add-on are Python 3.11, living in `libs/` and `scripts/` and installed to `/usr/local/lib/lab_creation/` — organized around a small set of shared library modules (`lab_creation.py`, `backends.py`, `services.py`, `spacecmd_common.py`, …) rather than one another. VM creation is behind a pluggable `VMBackend` interface (`LibvirtBackend` today), so the same orchestration code can eventually target other virtualization backends (KubeVirt, Harvester) without touching add-ons. One legacy add-on (`install_ds389`) is still plain bash — it predates the Python port and was already broken in bash, so it wasn't worth porting. The bash-era implementation these replaced lives on, archived, under `legacy_bash/`.

<p align="right"><a href="#top">↑ back to top</a></p>

---

## How it works

### Deploy pipeline

`setup_lab.py` runs a fixed sequence of phases; the two Kubernetes-only phases are skipped entirely for a VM-only lab (no `kclusters` section):

```mermaid
flowchart LR
    A["phase_services"] -->|"has kclusters"| C["phase_dns"]
    A -->|"no kclusters"| D["phase_create_vms"]
    C --> D["phase_create_vms"]
    D -->|"has kclusters"| F["phase_reboot_and_wait_kept_nodes"]
    D -->|"no kclusters"| H["phase_vm_addons"]
    F --> G["phase_install_k8s_and_addons"]
    G --> H["phase_vm_addons"]
```

### VM provisioning

Each VM is created by:
1. Resolving which KVM host it belongs on (the explicit `kvm_host` field, or auto-selected by free capacity — see [multi-host labs](#multi-host-labs))
2. Copying and resizing a QCOW2 source image on that host
3. Generating provisioning files from templates, per its `config_method`
4. Registering a DNS entry in BIND
5. Running `virt-install` on the hypervisor over SSH
6. Waiting for SSH to become available

Provisioning method is controlled by `config_method` in the lab JSON (per-node or per-`common`):

| Value | Method | Used for |
|---|---|---|
| _(empty, default)_ | Ignition + Combustion | SLE Micro |
| `cloud-init` | cloud-init ISO | openSUSE Leap, Ubuntu |
| `virt_customize` | Modifies the QCOW2 directly on the hypervisor (`virt-customize`) — no Ignition/cloud-init support needed on the guest | CentOS 7, old Debian/RHEL, or any image lacking Ignition/cloud-init |
| `install_iso` | Scripted install from a real installer ISO (AutoYaST, Kickstart, Preseed, or AutoInstall, selected by `install_type`) | Distros with no other provisioning path |

### VM backends

Which hypervisor technology actually creates a node is decided by a pluggable `VMBackend` interface, resolved once per node (`backend: harvester` in that node's config selects `HarvesterBackend`; anything else defaults to `LibvirtBackend`) — every add-on and orchestration script talks to the resolved backend the same way regardless of which one it is:

```mermaid
graph TD
    SV["setup_vm.py / setup_lab.py"] --> GB["backends.get_backend()"]
    GB -- "default" --> LB["LibvirtBackend"]
    GB -- "backend: harvester" --> HB["HarvesterBackend"]
    LB --> KVM["virt-install / virsh<br/>on a KVM hypervisor"]
    HB --> KV["KubeVirt VirtualMachine<br/>on a Harvester cluster"]
```

### Kubernetes setup

After VMs are up, `setup_lab.py` installs Kubernetes on each node according to the `kclusters` section of the JSON. RKE2 and K3s are both supported. Once a cluster is ready, its add-ons run in sequence; VM-level add-ons (attached to a single node rather than a cluster) run after that node is provisioned.

### Multi-host labs

A lab isn't limited to one hypervisor. Set `KVM_HOSTS` (space-separated) in `/etc/lab_creation.cfg` on the automation VM to make more than one hypervisor available:

```ini
KVM_HOSTS="hv1.mydemo.lab hv2.mydemo.lab hv3.mydemo.lab"
```

Then, for each node in the lab JSON, either:
- **pin it explicitly** — `"kvm_host": "hv2.mydemo.lab"` in that node's config, or
- **let it auto-select** — omit `kvm_host`; the node lands on whichever configured host currently has enough free CPU/RAM/disk for it (probed live over SSH).

Nodes that don't specify `kvm_host` and boxes with only one configured host behave exactly as before this feature existed — nothing changes for a single-hypervisor lab.

### Library loading order

Every script sources configuration in this order:

1. `/etc/lab_creation.defaults` — system defaults, paths, package lists
2. `/usr/local/lib/lab_creation/primary.py` — input validation, config loading
3. `/etc/lab_creation.cfg` — node-specific settings (`REMOTE_HOST`, `ROOT_SSH_KEY`, `VIRT_SRV`, `KVM_HOSTS`, etc.)
4. `/usr/local/lib/lab_creation/lab_creation.py` — VM, DNS, and orchestration functions
5. `/usr/local/lib/lab_creation/k8s.py` — Kubernetes cluster functions

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Quick Start

```mermaid
flowchart TD
    S1["1. Prepare the hypervisor OS"] --> S2["2. Bootstrap the setup scripts"]
    S2 --> S3["3. Configure and run the KVM node setup"]
    S3 --> S4["4. Configure the automation VM"]
    S4 --> S5["5. Point your client DNS at the automation VM"]
    S5 --> S6["6. Build your first lab"]
```

### Requirements

- A machine capable of running KVM (Intel VT-x or AMD-V enabled)
- Internet access (or a local mirror) for package and image downloads
- A QCOW2 image for your chosen OS placed at `/var/lib/libvirt/images/sources/` on the hypervisor

> [!IMPORTANT]
> The automation VM needs `python3.11` specifically — the toolchain pins to it explicitly. Most distros ship an older default `python3` alongside it; the install script refuses to proceed if `python3.11` is missing.

Tested images:
- [SLE Micro](https://www.suse.com/download/sle-micro/) — recommended, used with Ignition+Combustion
- openSUSE Leap Micro — supported, used with cloud-init

### Step 1 — Prepare the hypervisor OS

Install SLES (or another KVM-capable Linux) on your hardware. During install, choose:
- **Network**: create a bridge interface (`br0`) linked to your main NIC with a static IP
- **System role**: KVM Virtualization Host

<details>
<summary>Writing a bootable USB from Linux</summary>

```shell
# Before inserting USB:
cat /proc/partitions > /tmp/partb4

# Insert USB, then:
cat /proc/partitions > /tmp/parta

# Find the new device:
diff /tmp/part*
```

> [!WARNING]
> The next command **destroys all data** on the target device. Double-check `sdX` against the `diff` output above before running it.

```shell
# Write the ISO (replace sdX with your device):
dd if=SLE-15-SP6-Online-x86_64-GM-Media1.iso of=/dev/sdX bs=4k && sync
```

</details>

### Step 2 — Bootstrap the setup scripts

From any Linux machine with SSH access to the hypervisor:

```shell
curl https://raw.githubusercontent.com/SUSE-Technical-Marketing/lab-in-a-box/main/install_demo_server_scripts.sh | bash -
```

This downloads the setup scripts to `/var/tmp/setup_demo_server/`.

### Step 3 — Configure and run the KVM node setup

```shell
cd /var/tmp/setup_demo_server/setup_demo_server/
vim lab.cfg
```

Key settings in `lab.cfg`:

| Setting | Description |
|---|---|
| `ROOT_PWD_HASH` | Hashed root password — generate with `mkpasswd --method=SHA-512 --stdin` |
| `ROOT_SSH_PUB_KEY` | Your SSH public key for passwordless access |
| `AUTOMATION_HOSTNAME` | Hostname for the automation VM (e.g. `automation.mydemo.lab`) |
| `_QCOW_IMAGE` | Filename of the source QCOW2 image |
| Network settings | IP, gateway, mask, DNS for the lab network |

Then run the setup (replace `<IP>` with your hypervisor IP, or omit for local):

```shell
./setup_kvm_node.py <IP>
```

This provisions the automation VM and starts all required services.

### Step 4 — Configure the automation VM

SSH into the automation VM and install the lab scripts:

```shell
ssh <AUTOMATION_HOSTNAME>
./install_automation_node_scripts.sh

cp /etc/lab_creation.cfg.example /etc/lab_creation.cfg
vim /etc/lab_creation.cfg
```

Key settings in `lab_creation.cfg`:

| Setting | Description |
|---|---|
| `REMOTE_HOST` | Hostname or IP of the (primary) KVM hypervisor |
| `KVM_HOSTS` | _(optional)_ space-separated list of additional hypervisors for a [multi-host lab](#multi-host-labs) |
| `ROOT_SSH_KEY` | Content of the SSH public key to inject into VMs |
| `VIRT_SRV` | libvirt connection URI (e.g. `qemu+ssh://root@hypervisor/system`) |
| `NETWORK` | Default libvirt network for VMs (e.g. `bridge=br0`) |

### Step 5 — Point your client DNS at the automation VM

For hostnames to resolve from your desktop:

```shell
# Linux (NetworkManager):
nmcli con mod <connection> ipv4.dns <AUTOMATION_IP>

# Or add to /etc/resolv.conf:
nameserver <AUTOMATION_IP>
```

### Step 6 — Build your first lab

```shell
setup_lab.py examples/cluster.json.template
```

See [Examples](#examples) below for more starting points, or open the [web UI](#web-ui-lab-builder) instead of writing JSON by hand.

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Web UI (lab-builder)

A browser-based designer for `lab.json` files that **introspects the project's own Python libraries at run time** — it has no hardcoded knowledge of any add-on. Pick a component and it renders a form straight from that component's schema; add a field to a script and the UI shows it with zero front-end changes.

```shell
# Fastest way to try it — zero dependencies beyond Python:
python3.11 webui/run-local.py            # → http://localhost:8677/
```

For production deployment (Apache, or a standalone systemd/init-independent service, plus HTTPS via an idempotently-generated self-signed cert), see **[README.webui.md](README.webui.md)** — it covers all three deploy modes, the HTTP API, and troubleshooting.

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Lab definition format

Labs are defined as JSON or YAML files (auto-detected — see the note below). The current format supports multiple Kubernetes clusters per lab (`kclusters`); see `examples/cluster.json.template` for the legacy single-cluster format (`cluster`).

```mermaid
graph TD
    Lab["lab.json"] --> Nodes["nodes<br/>per-VM: myip, mymac, kcluster, addons..."]
    Lab --> Common["common<br/>shared defaults: ISO_IMAGE, VM_MEM, VM_DSK..."]
    Lab --> KClusters["kclusters<br/>clu_type, clu_rel, mydomain, addons"]
    Lab --> AddonSections["one section per add-on<br/>e.g. rancher, longhorn"]
    Nodes -. "kcluster" .-> KClusters
    KClusters -. "addons" .-> AddonSections
    Nodes -. "addons" .-> AddonSections
```

> [!NOTE]
> A `.yaml`/`.yml` file works too — the format is auto-detected from the extension (or by falling back to YAML if a file isn't valid JSON), the same way through `setup_lab.py`'s preflight check, `install_<addon> --validate`, and every addon's own load path. YAML input requires `pyyaml` (`pip install pyyaml`) on the automation VM; without it you get a clear error telling you to install it, not a silent JSON-only failure.

```jsonc
{
  "nodes": {
    "node101.mydemo.lab": {
      "myip":  "192.168.88.101",
      "mymac": "34:8a:b1:4b:1a:c1",
      "INSTALL_RKE2_TYPE": "server",   // "server" or "agent"
      "kcluster": "cluster1"           // which kclusters entry this node belongs to
    },
    "node102.mydemo.lab": {
      "myip":  "192.168.88.102",
      "mymac": "34:8a:b1:4b:1a:c2",
      "INSTALL_RKE2_TYPE": "agent",
      "kcluster": "cluster1"
    }
  },
  "common": {
    "ISO_IMAGE":  "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
    "VM_MEM":     "24576",
    "VM_DSK":     "80",
    "VM_CPU":     "6",
    "VM_BOOT":    "uefi",             // uefi (default), firmware=bios, bios, uefi=off
    "mymask":     "24",
    "mygw":       "192.168.88.1",
    "mydns":      "192.168.88.73",
    "mynet_reverse": "88.168.192"
  },
  "kclusters": {
    "cluster1": {
      "clu_type":  "rke2",             // "rke2" or "k3s"
      "clu_rel":   "stable",
      "mydomain":  "mydemo.lab",
      "addons": ["rancher", "longhorn"]
    }
  },
  "rancher": {
    "rancher_shorthn":   "rancher",
    "rancher_rel":       "rancher-prime",
    "rancher_repo_url":  "https://charts.rancher.com/server-charts/prime",
    "rancher_helm_rel":  "rancher",
    "rancher_helm_chart": "rancher-prime/rancher",
    "rancher_Version":   "--version 2.13.3",
    "cert_manager_ver":  "--version v1.14.4"
  }
}
```

<details>
<summary>Same lab, as YAML</summary>

```yaml
nodes:
  node101.mydemo.lab:
    myip: "192.168.88.101"
    mymac: "34:8a:b1:4b:1a:c1"
    INSTALL_RKE2_TYPE: server   # "server" or "agent"
    kcluster: cluster1          # which kclusters entry this node belongs to
  node102.mydemo.lab:
    myip: "192.168.88.102"
    mymac: "34:8a:b1:4b:1a:c2"
    INSTALL_RKE2_TYPE: agent
    kcluster: cluster1

common:
  ISO_IMAGE: SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2
  VM_MEM: "24576"
  VM_DSK: "80"
  VM_CPU: "6"
  VM_BOOT: uefi                # uefi (default), firmware=bios, bios, uefi=off
  mymask: "24"
  mygw: "192.168.88.1"
  mydns: "192.168.88.73"
  mynet_reverse: "88.168.192"

kclusters:
  cluster1:
    clu_type: rke2              # "rke2" or "k3s"
    clu_rel: stable
    mydomain: mydemo.lab
    addons: [rancher, longhorn]

rancher:
  rancher_shorthn: rancher
  rancher_rel: rancher-prime
  rancher_repo_url: https://charts.rancher.com/server-charts/prime
  rancher_helm_rel: rancher
  rancher_helm_chart: rancher-prime/rancher
  rancher_Version: "--version 2.13.3"
  cert_manager_ver: "--version v1.14.4"
```

</details>

Optional node-level fields:

| Field | Description |
|---|---|
| `addons` | List of addon scripts to run for this specific VM only |
| `config_method` | Override provisioning method (`cloud-init`, `virt_customize`, `install_iso`) |
| `kvm_host` | Pin this VM to a specific hypervisor in a [multi-host lab](#multi-host-labs) |
| `extra_dsk` | Additional disk(s) to attach — `"/dev/sdb"`, or `"/dev/sdb,bus=scsi"` to override the default bus per disk |
| `salt_states` | Salt states to apply (cloud-init method only) |
| `VM_MACHINE` | virt-install machine type override — `""` (default, virt-install's own choice, currently `q35`) or `"pc"` (legacy i440fx), for an old guest whose kernel/GRUB can't find its root disk under q35 — see [Deploying a legacy image (CentOS 7)](#deploying-a-legacy-image-centos-7) |

Optional kcluster fields:

| Field | Description |
|---|---|
| `mgm_node` | Hostname of the node that runs cluster addon installers; defaults to first server node |

Every add-on script also accepts `--schema` (alias for `--input-definition`), which prints its own configuration keys as machine-readable JSON or YAML — the same schema the [web UI](#web-ui-lab-builder) reads to build its forms:

```shell
install_longhorn --schema
setup_lab.py --input-definition yaml   # base topology schema (common/nodes/kclusters)
```

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Examples

### Minimal single-VM lab

The smallest possible lab — one VM, no Kubernetes:

```jsonc
{
  "nodes": {
    "standalone.mydemo.lab": { "myip": "192.168.88.50" }
  },
  "common": {
    "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
    "VM_MEM": "4096", "VM_DSK": "40", "VM_CPU": "2",
    "mymask": "24", "mygw": "192.168.88.1", "mydns": "192.168.88.73"
  }
}
```

```shell
setup_lab.py standalone.json
```

### RKE2 + Rancher + Longhorn (the "hello world" cluster)

A 2-node cluster with a management platform and distributed storage — see the full [Lab definition format](#lab-definition-format) example above.

```shell
setup_lab.py rancher-cluster.json
# Re-run later, skipping any VM that's already up and reachable:
setup_lab.py --keep rancher-cluster.json
```

### Spreading a cluster across two hosts

Pin the server to one hypervisor and let the agents auto-place on whichever of the [configured hosts](#multi-host-labs) has room:

```jsonc
{
  "nodes": {
    "srv1.mydemo.lab":   { "myip": "192.168.88.10", "kvm_host": "hv1.mydemo.lab", "kcluster": "prod" },
    "agent1.mydemo.lab": { "myip": "192.168.88.11", "kcluster": "prod" },
    "agent2.mydemo.lab": { "myip": "192.168.88.12", "kcluster": "prod" }
  },
  "common": { "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2", "VM_MEM": "8192", "VM_DSK": "60", "VM_CPU": "4" },
  "kclusters": { "prod": { "clu_type": "rke2", "clu_rel": "stable", "mydomain": "mydemo.lab" } }
}
```

### SUSE Manager (Uyuni) server + a registered client

Stand up an Uyuni server with an activation key, then register a second VM against it as a Salt client — see [Available addons](#available-addons) for the full feature set (`orgs`, RBAC, Content Lifecycle Management, Ansible integration, and more):

```jsonc
{
  "nodes": {
    "uyuni.mydemo.lab":  { "myip": "192.168.88.30", "addons": ["uyuni"] },
    "client1.mydemo.lab": { "myip": "192.168.88.31", "addons": ["client_registration"] }
  },
  "common": { "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2", "VM_MEM": "8192", "VM_DSK": "60", "VM_CPU": "4" },
  "uyuni": {
    "uyuni_admin": "admin", "uyuni_password": "Uyuni12345",
    "uyuni_activation_key": "1-lab-clients", "uyuni_activation_key_base_channel": "sle-micro-6.1-pool"
  },
  "client_registration": {
    "client_registration_server_type": "uyuni",
    "client_registration_server": "uyuni.mydemo.lab",
    "client_registration_activation_key": "1-lab-clients"
  }
}
```

### Deploying a legacy image (CentOS 7)

CentOS 7 (and other pre-built `.qcow2` images that old) has neither Ignition/Combustion nor
cloud-init built in, so `config_method` must be `virt_customize` (see
[VM provisioning](#vm-provisioning)) — it edits the qcow2 filesystem directly instead of relying
on an in-guest agent. Two more overrides matter for an image this old:

- `VM_BOOT: "bios"` — CentOS 7's GRUB expects legacy BIOS, not this project's UEFI default.
- `VM_MACHINE: "pc"` — confirmed live against a 2015 CentOS 7 GenericCloud image (kernel
  `3.10.0-229`): booted under virt-install's own machine-type default (currently `q35`), it hangs
  forever in a dracut emergency shell (`Not all disks have been found`) — its virtio-blk root disk
  never shows up in time under Q35's PCIe topology. The identical disk boots straight through
  under the legacy i440fx chipset (`"pc"`). This is a chipset/old-kernel incompatibility, not
  anything specific to this project — the same override applies to any sufficiently old guest.

```jsonc
{
  "nodes": {
    "legacy1.mydemo.lab": {
      "myip": "192.168.88.120",
      "config_method": "virt_customize",
      "ISO_IMAGE": "CentOS-7-x86_64-GenericCloud-20150628_01.qcow2",
      "VM_BOOT": "bios",
      "VM_MACHINE": "pc"
    }
  },
  "common": { "VM_MEM": "2048", "VM_DSK": "30", "VM_CPU": "1", "VM_ROOT_PASS": "12345678" }
}
```

```shell
setup_lab.py legacy.json
```

`VM_ROOT_PASS` (in `common`, or per-node) sets the root password `virt_customize` bakes into the
image directly — omit it to reuse `ROOT_PWD_HASH` from `lab_creation.cfg` instead, the same
fallback cloud-init and Ignition use.

More distros (AlmaLinux, Debian, Rocky Linux, Alibaba Cloud Linux, and a documented
not-yet-working attempt at Raspberry Pi OS) are in **[EXAMPLES.md](EXAMPLES.md)**, kept separate
so this section doesn't grow without bound.

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Step-by-Step Walkthroughs

The [Examples](#examples) above are copy-paste starting points. These three walk through complete, real scenarios end to end — what to run, what happens at each step, and how to verify it actually worked. Every JSON field and command shape below matches this project's own test suite (`tests/run_tests.sh`) and source.

> [!TIP]
> Walkthroughs 2 and 3 are **live-tested** — run against a real server/hardware, not just checked in isolation.

### Walkthrough 1 — Your first cluster: RKE2 + Rancher + Longhorn

Goal: two SLE Micro VMs, an RKE2 cluster, Rancher for management, Longhorn for storage — reachable from your browser at the end.

1. **Write the lab file.** Save this as `rancher-cluster.json` (adjust IPs/network to your lab domain):

   ```jsonc
   {
     "nodes": {
       "node101.mydemo.lab": {
         "myip": "192.168.88.101", "mymac": "34:8a:b1:4b:1a:c1",
         "INSTALL_RKE2_TYPE": "server", "kcluster": "cluster1"
       },
       "node102.mydemo.lab": {
         "myip": "192.168.88.102", "mymac": "34:8a:b1:4b:1a:c2",
         "INSTALL_RKE2_TYPE": "agent", "kcluster": "cluster1"
       }
     },
     "common": {
       "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
       "VM_MEM": "24576", "VM_DSK": "80", "VM_CPU": "6",
       "mymask": "24", "mygw": "192.168.88.1", "mydns": "192.168.88.73"
     },
     "kclusters": {
       "cluster1": {
         "clu_type": "rke2", "clu_rel": "stable", "mydomain": "mydemo.lab",
         "addons": ["rancher", "longhorn"]
       }
     },
     "rancher": {
       "rancher_shorthn": "rancher", "rancher_rel": "rancher-prime",
       "rancher_repo_url": "https://charts.rancher.com/server-charts/prime",
       "rancher_helm_rel": "rancher", "rancher_helm_chart": "rancher-prime/rancher",
       "rancher_Version": "--version 2.13.3", "cert_manager_ver": "--version v1.14.4"
     }
   }
   ```

2. **Build it:**

   ```shell
   setup_lab.py rancher-cluster.json
   ```

   `setup_lab.py` preflights the file automatically before doing anything else — bad IPs, a `kcluster` reference that doesn't exist, a missing `ISO_IMAGE`, and similar mistakes get caught and printed (`✗ Preflight FAILED — N error(s)`) with nothing created, rather than failing partway through. A clean file prints `✓ Preflight passed` and proceeds straight to building.

   In order, this: registers both nodes in DNS → creates both VMs (copies the QCOW2 image, generates Combustion files, boots them, waits for SSH) → installs RKE2 on `node101` as server, then `node102` as agent → installs `rancher` and `longhorn` on the cluster's management node (`mgm_node`, defaulting to the first server node — `node101` here). A 2-node cluster with Rancher typically takes 15–25 minutes; most of it is RKE2 bootstrapping and Rancher's own Helm install.

3. **Verify DNS resolves** (from your own desktop, once it's [pointed at the automation VM's DNS](#step-5--point-your-client-dns-at-the-automation-vm)):

   ```shell
   dig +short node101.mydemo.lab rancher.mydemo.lab
   ```

   Both should return `192.168.88.101` (Rancher's ingress hostname is the `rancher_shorthn` value, `rancher`, under the cluster's `mydomain`).

4. **Log in.** Browse to `https://rancher.mydemo.lab` (self-signed cert — your browser will warn once) and log in with `rancher_initial_pwd` from `/etc/lab_creation.cfg` on the automation VM.

5. **Iterate without rebuilding everything.** Changed one node's config, or a VM crashed? Re-run with `--keep`: any VM that already exists, matches its defined IP/MAC, and is SSH-reachable is left alone; only what's actually missing or broken gets (re)created:

   ```shell
   setup_lab.py --keep rancher-cluster.json
   ```

6. **Tear it down** when you're done:

   ```shell
   destroy_lab.py rancher-cluster.json
   ```

### Walkthrough 2 — SUSE Manager (Uyuni) server with a registered client

Goal: an Uyuni server with a real activation key, and a second VM that registers itself as a Salt-managed client against it. **Live-tested** end to end against a real Uyuni server.

1. **Write the lab file** — one node for the Uyuni server, one for the client:

   ```jsonc
   {
     "nodes": {
       "uyuni.mydemo.lab":   { "myip": "192.168.88.30", "addons": ["uyuni"] },
       "client1.mydemo.lab": { "myip": "192.168.88.31", "addons": ["client_registration"] }
     },
     "common": {
       "ISO_IMAGE": "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2",
       "VM_MEM": "8192", "VM_DSK": "60", "VM_CPU": "4",
       "mymask": "24", "mygw": "192.168.88.1", "mydns": "192.168.88.73", "mydomain": "mydemo.lab"
     },
     "uyuni": {
       "uyuni_admin": "admin", "uyuni_password": "Uyuni12345",
       "uyuni_activation_key": "1-lab-clients", "uyuni_activation_key_base_channel": "sle-micro-6.1-pool"
     },
     "client_registration": {
       "client_registration_server_type": "uyuni",
       "client_registration_server": "uyuni.mydemo.lab",
       "client_registration_activation_key": "1-lab-clients"
     }
   }
   ```

2. **Build it:**

   ```shell
   setup_lab.py uyuni-lab.json
   ```

   VM-level addons (both `uyuni` and `client_registration` are node-attached, not cluster-attached, since there's no `kclusters` section here) run once their own node is up. `install_uyuni` brings up the server, waits for it to become reachable, then creates the activation key. `install_client_registration` then bootstraps `client1` against it — installs the bootstrap script, runs it, and polls until the new minion's Salt key shows up as pending, then accepts it.

3. **Verify the client actually registered.** SSH into the Uyuni server and ask it directly:

   ```shell
   ssh uyuni.mydemo.lab
   mgrctl exec 'spacecmd -- system_list'
   ```

   `client1.mydemo.lab` should be in the list.

4. **Log in to the web UI** at `https://uyuni.mydemo.lab` with `uyuni_admin`/`uyuni_password` to see the same thing visually, browse the activation key, or run a highstate.

Known upstream rough edge (not this project's bug, documented in case you hit it): `salt-transactional-update`'s own package upgrade scriptlet can leave a duplicate YAML key in `/etc/salt/minion.d/transactional_update.conf` on the client, crash-looping `salt-minion` until it's manually deduplicated. Nothing in this repo touches that file.

### Walkthrough 3 — Automation VM under NAT (single-NIC laptop as the hypervisor)

Goal: bootstrap the automation VM on a host with no spare NIC to bridge — a private libvirt-managed network instead, with specific ports DNAT'd in from the host's own real IP. **Live-tested** end to end on a disposable nested VM.

This changes nothing about the [default Quick Start](#quick-start) flow if you don't opt in — `_network_mode` defaults to `"bridge"`, byte-for-byte the same as every existing setup.

1. **In `lab.cfg`** (Quick Start [Step 3](#step-3--configure-and-run-the-kvm-node-setup)), set:

   ```ini
   _network_mode="nat"
   _nat_network_name="labnat"          # default shown — a new libvirt virtual network, not your host's real LAN
   _nat_network_cidr="192.168.150.0/24" # default shown
   _nat_forwarded_ports="22:22/TCP 80:80/TCP 443:443/TCP"  # default shown — "<port on the HYPERVISOR's real IP>:<port on the AUTOMATION VM>/<protocol>"
   ```

   This forwards ports 22/80/443 on the **hypervisor's own real, externally-reachable IP** (`<external>`) through to the same ports on the **automation VM's private NAT address** (`<internal>`) — the automation VM is the only thing listening on this private network at this point, so "internal" always means "on the automation VM" here. (Step 5 below reuses this exact same `<external>:<internal>/<protocol>` syntax to forward into a *lab* VM instead, once one exists — there, "internal" shifts to mean that VM's own private NAT address, not the automation VM's.)

2. **Run the setup exactly as usual:**

   ```shell
   ./setup_kvm_node.py
   ```

   This defines the `labnat` libvirt network (NAT'd, DHCP/gateway handled by libvirt itself — the same mechanism as libvirt's own built-in `default` network, just under this project's own name/CIDR) instead of a bridge, then creates the automation VM on it with a static IP inside that private range, then DNAT-forwards the three ports above in from the host's own real IP.

3. **Verify the network and forwarding rules exist**, on the hypervisor:

   ```shell
   virsh net-list                              # labnat: active
   iptables -t nat -L LAB_PORTFWD -n -v         # DNAT rules to the automation VM's private IP
   iptables -L LAB_PORTFWD_FWD -n -v            # matching FORWARD-chain ACCEPT rules
   ```

4. **Reach the automation VM from outside the hypervisor**, using the hypervisor's own real IP — not the automation VM's private `192.168.150.x` address, which isn't routable from anywhere else:

   ```shell
   ssh root@<hypervisor-real-ip>          # DNAT'd to the automation VM's SSH, port 22
   ```

   Ports 80 and 443 are forwarded too by default (the provisioning-file HTTP server and, once you set up the [web UI](#web-ui-lab-builder), its HTTPS listener) — reachable the same way, through the hypervisor's real IP.

5. **Add forwarding for a lab VM**, not just the automation VM itself: give that node a `forwarded_ports` field and enable the `portforward` service once, in `common.services`:

   ```jsonc
   {
     "nodes": {
       "app1.mydemo.lab": {
         "myip": "192.168.150.20",
         "forwarded_ports": ["8080:80/TCP", "8443:443/TCP"]
       }
     },
     "common": { "services": ["portforward"], "...": "…rest of common as usual" }
   }
   ```

   `setup_lab.py`/`setup_vm.py` DNAT-forward those two ports in from the hypervisor's real IP the same way, the first time any node in the lab declares `forwarded_ports`.

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Available commands

All commands run on the **automation VM** and take a JSON lab definition file as their first argument.

| Command | Description |
|---|---|
| `setup_lab.py [--keep] <lab.json>` | Create all VMs, set up Kubernetes clusters, and install every cluster-level and VM-level addon in order. `--keep` skips any VM that already exists, matches the defined IP/MAC, and is SSH-reachable — without it, every VM is destroyed and recreated. |
| `setup_vm.py <lab.json> <hostname>` | Create or recreate a single VM |
| `destroy_vm.py <lab.json> <hostname>` | Destroy a single VM |
| `destroy_lab.py <lab.json>` | Destroy all VMs in a lab |

Every command and every `install_<addon>` script supports:

```shell
setup_lab.py --version              # print the installed version
install_longhorn --schema           # print this addon's configuration schema (JSON)
install_longhorn --schema yaml      # ...or YAML
```

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Available addons

Addons are referenced by name in the `addons` array of a kcluster or node. The corresponding `install_<name>` script must be on `PATH`.

<sub>Jump to: <a href="#addons-k8s">Kubernetes &amp; GitOps</a> · <a href="#addons-security">Security &amp; compliance</a> · <a href="#addons-suma">SUSE Manager / Uyuni</a> · <a href="#addons-storage">Storage &amp; databases</a> · <a href="#addons-cicd">CI/CD &amp; tooling</a> · <a href="#addons-ai">AI / ML</a> · <a href="#addons-virt">Virtualization &amp; demos</a></sub>

<a id="addons-k8s"></a>
<details open>
<summary><strong>Kubernetes platform &amp; GitOps</strong></summary>

| Addon name | Description |
|---|---|
| `rancher` | SUSE Rancher Prime Kubernetes management platform |
| `longhorn` | SUSE Longhorn distributed block storage |
| `harbor` | Container registry |
| `argocd` | Argo CD GitOps controller |
| `kubewarden` | Kubernetes policy engine |
| `istio` | Service mesh |
| `linkerd` | Service mesh |
| `traefik` | Ingress controller |
| `nginx` | Ingress controller / reverse proxy |
| `coredns` | Cluster DNS |
| `kucero` | Kubernetes cluster certificate rotation |
| `fluid` | Data orchestration/caching for cloud-native workloads |

</details>

<a id="addons-security"></a>
<details open>
<summary><strong>Security &amp; compliance</strong></summary>

| Addon name | Description |
|---|---|
| `neuvector` | SUSE NeuVector container security platform |
| `nv_testing` | NeuVector security testing workloads (nginx/node/redis pods) |
| `nv-demo-helm` | NeuVector Helm-based demo workloads |
| `complianceascode` | OpenSCAP/ComplianceAsCode operator |
| `keycloak` | Identity and access management |
| `kagent` | Kubernetes agentic-AI security assistant |
| `insecure_app` | Intentionally vulnerable web application (demo/training) |
| `struts_demo` | Apache Struts2 vulnerable demo application (CVE-2017-5638) |

</details>

<a id="addons-suma"></a>
<details open>
<summary><strong>SUSE Manager / Uyuni</strong></summary>

| Addon name | Description |
|---|---|
| `uyuni` | Uyuni server (upstream): activation keys, orgs, RBAC, Content Lifecycle Management, Ansible integration, SCAP/CVE auditing, dev/QA/prod environment topology — see `install_uyuni --schema` for the full field list |
| `smlm` | SUSE Manager Lifecycle Management server — the same feature set as `uyuni`, Kubernetes/Helm-deployed |
| `smlm_proxy` | SMLM proxy |
| `client_registration` | Register any VM as a Salt client of an existing `uyuni`/`smlm` server (activation key bootstrap + salt-key acceptance) |
| `suma` | SUSE Manager (SUMA), installed directly on the OS via `mgradm` — not Kubernetes |

</details>

<a id="addons-storage"></a>
<details open>
<summary><strong>Storage &amp; databases</strong></summary>

| Addon name | Description |
|---|---|
| `mariadb` | MariaDB database |
| `postgresql` | PostgreSQL database |
| `openldap` | OpenLDAP directory service |
| `ds389` | 389 Directory Server (LDAP) — the one add-on still implemented in bash |

</details>

<a id="addons-cicd"></a>
<details open>
<summary><strong>CI/CD &amp; developer tooling</strong></summary>

| Addon name | Description |
|---|---|
| `jenkins` | Jenkins CI |
| `appcollection` | SUSE Application Collection |
| `stackpack` | StackState monitoring integration |
| `trento` | SAP infrastructure monitoring |

</details>

<a id="addons-ai"></a>
<details open>
<summary><strong>AI / ML</strong></summary>

| Addon name | Description |
|---|---|
| `ollama` | Local LLM runtime |
| `deepseek` | DeepSeek model, served via Ollama |
| `gemini` | Google Gemini API integration |
| `phoebe` | (see `install_phoebe --schema`) |

</details>

<a id="addons-virt"></a>
<details open>
<summary><strong>Virtualization &amp; demos</strong></summary>

| Addon name | Description |
|---|---|
| `harvester` | SUSE Virtualization (Harvester/KubeVirt) node provisioning |
| `wordpress` | WordPress + MySQL demo application |
| `kiwi` | KIWI appliance builder |
| `fluentd` | Log aggregation |

</details>

To add a new addon: create `scripts/install_<name>.py` following the pattern of an existing one (source `addon_common`, load the relevant JSON section via `load_definition()`, do the work over SSH), add templates under `templates/addons/<name>/` if needed, and reference `"<name>"` in the `addons` array of your JSON — `install_automation_node_scripts.sh`'s deploy loop and the web UI both discover it automatically.

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Configuration reference

### `/etc/lab_creation.defaults`

System-wide defaults loaded by every script. Defines paths, default delay timers, and package lists. **Do not edit** unless you know what you are doing.

### `/etc/lab_creation.cfg`

Node-specific configuration for the automation VM. Copied from `/etc/lab_creation.cfg.example` during setup. Key variables:

| Variable | Description |
|---|---|
| `REMOTE_HOST` | KVM hypervisor hostname or IP |
| `KVM_HOSTS` | _(optional)_ space-separated list of hypervisors for a [multi-host lab](#multi-host-labs); defaults to just `REMOTE_HOST` |
| `VIRT_SRV` | libvirt URI for remote hypervisor |
| `ROOT_SSH_KEY` | SSH public key content injected into provisioned VMs |
| `NETWORK` | Default libvirt network string |
| `REMOTE_DNS_SERVERS` | Space-separated list of additional DNS servers to update |
| `delay_min` | Minutes to wait between provisioning stages (increase on slow hardware) |

### `/usr/local/lib/lab_creation/`

Installed Python library modules. Updated by running `install_automation_node_scripts.sh` from the repo on the automation VM.

| File | Contents |
|---|---|
| `lab_creation.py` | VM lifecycle, DNS, multi-host resolution, and orchestration helpers |
| `backends.py` | `VMBackend` interface + `LibvirtBackend` (create/delete/reboot a VM, provisioning-file push) |
| `services.py` | DNS service management |
| `spacecmd_common.py` | Shared SUSE Manager/Uyuni automation (activation keys, orgs, RBAC, CLM, Ansible, SCAP/CVE) used by `install_uyuni`/`install_smlm`/`install_client_registration` |
| `primary.py` | Input validation and config loading |
| `k8s.py` | Kubernetes cluster distro interface (RKE2/K3s) |
| `addon_common.py` | Shared CLI plumbing every `install_*` addon uses (`--help`/`--version`/`--schema` dispatch, schema validation) |

The four bash helpers (`lab_creation.bash`, `k8s_functions.bash`, `primary_functions.bash`, `extensions.sh`) are also still installed alongside these — kept indefinitely for `install_ds389`, the one addon that never got a Python port.

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Testing

Every check runs in its **own independent, disposable `podman` container** — a crash, hang, or leftover state in one can't touch any other:

```shell
tests/run_tests.sh
```

Covers bash and Python syntax across the whole tree, schema/webui consistency, mocked-SSH unit tests for every core library and orchestration script, and regression tests for bugs found during live testing. Add a new check by dropping an executable script into `tests/checks/` — it's picked up automatically, no wiring needed.

Wired into a pre-commit hook (enable once per clone, see [Contributing](#contributing--developer-setup)) — it runs automatically on every commit and skips with a warning if `podman` isn't installed, rather than blocking the commit.

> [!NOTE]
> The [Examples](#examples) above each have a matching real-hardware deploy+check test under `tests/examples/` — they need a working KVM hypervisor and automation VM, so they're **not** part of `tests/run_tests.sh`'s automatic sweep, but they're present and runnable by hand (`tests/examples/run_example.sh <name>`) before pushing a change that could affect one of these documented flows. See [tests/examples/README.md](tests/examples/README.md).

<p align="right"><a href="#top">↑ back to top</a></p>

---

## Contributing / Developer setup

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full guide (dev setup, coding conventions, how to add an add-on, PR process). This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md); see [SECURITY.md](SECURITY.md) to report a vulnerability. Every push and pull request runs through [CI](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml) — Python 3.11 syntax/import checks, every add-on's schema, `shellcheck`, and the full containerized test suite below.

### One-time git setup

After cloning the repository, run:

```shell
git config core.hooksPath .githooks
```

This activates the hooks in `.githooks/`, which:
- run the full [test suite](#testing) before every commit
- manage per-script version stamping (see below)

### How versioning works

> [!NOTE]
> This is handled entirely by the git hooks above — you never edit `__LABVERSION__` by hand.

Every script contains the placeholder:

```python
__LABVERSION__ = "__LABVERSION__"
```

The hooks in `.githooks/` expand and restore this placeholder automatically:

| Hook | Trigger | Action |
|---|---|---|
| `post-checkout` | `git checkout` / `git switch` | Replaces `__LABVERSION__` in each script with the hash of the last commit that touched that file |
| `post-merge` | `git pull` / `git merge` | Same as above |
| `post-rewrite` | `git rebase` / `git commit --amend` | Same as above |
| `pre-commit` | `git commit` | Restores `__LABVERSION__` in any staged scripts before the commit is written, so hashes are never stored in the repository |

The result: every script in your working tree shows its own version via `--version`, and the repo itself always stores the clean placeholder. When scripts are installed via `install_automation_node_scripts.sh`, the same per-file hash substitution is applied at install time using `git log -1 --format=%h`.

### Installing scripts onto the automation VM

From the repo root on the automation VM (or any machine with the repo cloned):

```shell
./install_automation_node_scripts.sh
```

This backs up the existing installation (both its own timestamped tarball and, separately, whatever your own backup process keeps), copies every script/library/template to its system path, and stamps each installed file with its version hash.

### Dependencies

Runtime (on the automation VM):
`python3.11`, `jq`, `ssh`, `rsync`, `nc`, `helm`, `kubectl`, `named` (BIND)

Hypervisor setup additionally requires:
`virt-install`, `virsh`, `qemu-img`, `zypper`, QCOW2 source images in `/var/lib/libvirt/images/sources/`

Running the [test suite](#testing) additionally requires:
`podman`

Using a YAML (rather than JSON) [lab definition](#lab-definition-format) additionally requires:
`pyyaml` (`pip install pyyaml`)
