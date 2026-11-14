#!/bin/bash
# Configuration steps of a lab-in-a-box automation node: a VM, a podman container or a Kubernetes pod.
#
# Run inside the node as "automation-node-lib.sh configure [--offline|--boot]" it configures the node from
# NODE_DIR (settings, this file and the lab-in-a-box tree), which setup_lab_automation.sh puts there:
#   --offline  in a chroot of the VM image before its first boot: services are enabled, not restarted
#   --boot     from lab-node-setup.service at every start of a container or pod: nothing is downloaded
# Sourced by setup_lab_automation.sh on the KVM host for write_node_settings and the write_* steps it needs
# before the node runs.
#
# write_* ROOT functions write files under the directory ROOT ("/" inside the node). Settings come from
# NODE_DIR/settings, see NODE_SETTINGS.

NODE_DIR=/etc/lab_creation/node

# Shell variables the node needs, written to NODE_DIR/settings by write_node_settings.
NODE_SETTINGS="AUTOMATION_HOSTNAME MYREG _myip _mymask_cidr _mygw _mydns _mydomain _mynetrev _timezone _virt_srv
ROOT_SSH_PUB_KEY HOST_SSH_PUB_KEY ROOT_PWD_HASH _host_ip _host_name _host_fqdn _node_kind _node_netstack
_node_network _python_bin _kubectl_install"

# sshfs mount options for the hypervisor's source images. accept-new: the node has never connected to the hypervisor
# before, so its host key is not known yet.
_SSHFS_OPTS="_netdev,reconnect,identityfile=/root/.ssh/id_rsa,allow_other,default_permissions,StrictHostKeyChecking=accept-new"

# Packages a VM or cloud node gets on top of the openSUSE Leap image; the container image has them already. Nothing
# that pulls in a kernel: installing one in the chroot rebuilds the initrd there, and the VM then cannot find its
# root disk.
_VM_PACKAGES="vim-small git rsync apache2 bind-utils bind docker podman libvirt-client jq virt-install salt-ssh
fuse3 sshfs netcat-openbsd openssl sudo qrencode"

# What each Leap version calls its Python, and the interpreter the lab scripts then run under.
# node_install_packages picks a set by the node's own VERSION_ID. Leap 16 packages no kubernetes client; kubectl
# comes from _kubectl_install there.
_LEAP15_PACKAGES="python311 python311-cryptography kubernetes1.35-client"
_LEAP15_PYTHON="python3.11"
_LEAP16_PACKAGES="python313 python313-cryptography python313-PyYAML"
_LEAP16_PYTHON="python3.13"

# Products a registered SLES node activates before installing _VM_PACKAGES: SLES ships salt-ssh and sshfs only in
# PackageHub (as libs/kvm_host_profiles.py's SLES profiles also activate); openSUSE Leap has them in its own repositories.
_SLES_PRODUCTS="PackageHub"

# Writes NODE_DIR/settings (mode 0600) under ROOT from the current values of NODE_SETTINGS. A setting that is unset
# is written empty: every optional one, such as _kubectl_install, is absent on the nodes that do not need it.
function write_node_settings() {
    local _root="$1" _v
    mkdir -p "${_root}${NODE_DIR}"
    for _v in ${NODE_SETTINGS}; do
        printf '%s=%q\n' "${_v}" "${!_v-}"
    done > "${_root}${NODE_DIR}/settings"
    chmod 0600 "${_root}${NODE_DIR}/settings"
}

# Networks this node will recurse for. A lab node's address is always one of the private ranges, and no address on
# the public internet is, which is what stops an automation node whose port 53 is reachable from the internet, as a
# cloud one's is, from being an open resolver usable for amplification attacks. Outside clients still get
# authoritative answers for the lab's own zones, which is all resolving a lab VM by name needs. Mirrors
# dns_recursion_networks() in libs/backends.py, which does the same for the cloud DNS VM.
_RECURSION_NETWORKS="10.0.0.0/8; 172.16.0.0/12; 192.168.0.0/16; 100.64.0.0/10;"

# Writes the BIND configuration and, unless they exist, the lab zones (labs add records to them) under ROOT.
function write_dns_files() {
    local _root="$1" _host_a="" _host_ptr=""
    mkdir -p "${_root}/etc" "${_root}/var/lib/named"
    # The KVM host's own records, only when there is a KVM host. A cloud automation node has none, and an empty
    # name or address here would write a malformed line that named refuses the whole zone over.
    if [[ -n "${_host_name}" && -n "${_host_ip}" ]]; then
        _host_a="${_host_name}         IN  A       ${_host_ip}"
        _host_ptr="${_host_ip##*.}      IN  PTR     ${_host_fqdn}."
    fi
    cat > "${_root}/etc/named.conf" <<EOF
options {
        directory "/var/lib/named";
        managed-keys-directory "/var/lib/named/dyn/";
        dump-file "/var/log/named_dump.db";
        statistics-file "/var/log/named.stats";
        listen-on port 53 { any; };
        listen-on-v6 { any; };
        allow-query { any; };
        allow-query-cache { ${_RECURSION_NETWORKS} localhost; localnets; };
        allow-recursion { ${_RECURSION_NETWORKS} localhost; localnets; };
        allow-transfer { none; };
        allow-update { none; };
        version none;
        recursion yes;
        rate-limit { responses-per-second 15; window 5; };
        dnssec-validation no;
        forward only;
        forwarders {
            ${_mydns};
        };
};
zone "." in {
        type hint;
        file "root.hint";
};
zone "localhost" in {
        type master;
        file "localhost.zone";
};
zone "0.0.127.in-addr.arpa" in {
        type master;
        file "127.0.0.zone";
};
zone "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa" IN {
        type master;
        file "127.0.0.zone";
};
zone "${_mydomain}" in {
        type master;
        file "${_mydomain}.lan";
        allow-update { none; };
};
zone "${_mynetrev}.in-addr.arpa" in {
        type master;
        file "${_mynetrev}.db";
        allow-update { none; };
};
EOF
    chmod 0644 "${_root}/etc/named.conf"

    [[ -f "${_root}/var/lib/named/${_mynetrev}.db" ]] || cat > "${_root}/var/lib/named/${_mynetrev}.db" <<EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011601  ;Serial
        3600        ;Refresh
        1800        ;Retry
        604800      ;Expire
        86400       ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.
        IN  PTR     ${_mydomain}.

${_myip//*.}      IN  PTR     ${AUTOMATION_HOSTNAME}.
${_host_ptr}

EOF

    [[ -f "${_root}/var/lib/named/${_mydomain}.lan" ]] || cat > "${_root}/var/lib/named/${_mydomain}.lan" <<EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011603  ;Serial
        1m        ;Refresh
        15m        ;Retry
        3w        ;Expire
        2h        ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.
        IN  A       ${_myip}
        IN  MX 10   ${AUTOMATION_HOSTNAME}.

${AUTOMATION_HOSTNAME//.$_mydomain}         IN  A       ${_myip}
${MYREG//.$_mydomain}         IN  CNAME   ${AUTOMATION_HOSTNAME}
bastion          IN  CNAME   ${AUTOMATION_HOSTNAME}.
${_host_a}

EOF
    chmod 0644 "${_root}/var/lib/named/${_mydomain}.lan" "${_root}/var/lib/named/${_mynetrev}.db"
}

# Helm builds the automation node serves, in helm's own architecture names; install_helm.sh maps a lab node's
# `uname -m` onto one of them.
_HELM_ARCHES="amd64 arm64"

# Writes download_latest_helm.sh (run on the automation node) and install_helm.sh (served to lab nodes) under ROOT.
function write_helm_scripts() {
    local _root="$1"
    mkdir -p "${_root}/usr/local/bin" "${_root}/srv/www/htdocs/helm"
    cat > "${_root}/usr/local/bin/download_latest_helm.sh" << HELMSCRIPT
#!/bin/bash
# Downloads helm's KEYS and the latest helm release for each architecture (${_HELM_ARCHES}) into directory \$1
# (default /srv/www/htdocs/helm), as helm-latest-linux-<arch>.tar.gz. A download that fails, or a tarball that is not
# a gzip archive, leaves the file already there in place, is reported, and makes the script exit 1.
dir="\${1:-/srv/www/htdocs/helm}" rc=0
is_gzip() { [[ "\$(head -c2 "\$1" 2>/dev/null | od -An -tx1 | tr -d ' \\n')" == 1f8b ]]; }
fetch() {
    curl -fsSL "\$1" -o "\$2.part" && { [[ "\$2" != *.tar.gz ]] || is_gzip "\$2.part"; } \\
        && chmod 0644 "\$2.part" && mv -f "\$2.part" "\$2" && return 0
    rm -f "\$2.part"
    echo "ERROR: could not download \$1" >&2
    return 1
}
version=\$(curl -fsSL https://get.helm.sh/helm-latest-version | grep '^v[0-9]') \\
    || { echo "ERROR: could not read the latest helm version from get.helm.sh" >&2; exit 1; }
mkdir -p "\${dir}"
fetch https://raw.githubusercontent.com/helm/helm/main/KEYS "\${dir}/KEYS" || rc=1
for arch in ${_HELM_ARCHES}; do
    fetch "https://get.helm.sh/helm-\${version}-linux-\${arch}.tar.gz" "\${dir}/helm-latest-linux-\${arch}.tar.gz" || rc=1
done
exit \${rc}
HELMSCRIPT

    # install_helm.sh: served by the automation node, run on client nodes
    cat > "${_root}/srv/www/htdocs/helm/install_helm.sh" << HELMSCRIPT
#!/bin/bash
case "\$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "ERROR: the automation node serves no helm build for architecture \$(uname -m)" >&2; exit 1 ;;
esac
mkdir -p /tmp/helm
curl -fsSL \${LAB_CURL_TLS-} "\${LAB_PROVISIONING_URL:-http://${AUTOMATION_HOSTNAME}}/helm/helm-latest-linux-\${arch}.tar.gz" -o /tmp/helm/helm.tar.gz || exit 1
tar xzf /tmp/helm/helm.tar.gz -C /tmp/helm || exit 1
install -m 0755 "/tmp/helm/linux-\${arch}/helm" /usr/local/bin/helm
HELMSCRIPT

    chmod 0755 "${_root}/usr/local/bin/download_latest_helm.sh" "${_root}/srv/www/htdocs/helm/install_helm.sh"
}

# Fills directory $1 with download_latest_helm.sh's files, unless every helm tarball there is a gzip archive and
# KEYS is there. Checked by the gzip magic number, since a minimal image may have no gzip.
function download_helm_files() {
    local _dir="$1" _arch
    for _arch in ${_HELM_ARCHES}; do
        [[ "$(head -c2 "${_dir}/helm-latest-linux-${_arch}.tar.gz" 2>/dev/null | od -An -tx1 | tr -d ' \n')" == 1f8b ]] \
            || { /usr/local/bin/download_latest_helm.sh "${_dir}"; return; }
    done
    [[ -s "${_dir}/KEYS" ]] || /usr/local/bin/download_latest_helm.sh "${_dir}"
}

# Writes the sshfs automount of the hypervisor's source images on /srv/www/htdocs/sources under ROOT: an fstab line on
# a VM, systemd units in a container or pod (systemd's fstab generator does not run there).
function write_sshfs_mount() {
    local _root="$1" _what="${_virt_srv:-root@hypervisor}:/var/lib/libvirt/images/sources"
    # A cloud node has no hypervisor: its labs run on the provider's own images, named in SOURCE_IMAGE, so there is
    # no source-image directory to mount and nothing on the other end of an sshfs to the home lab.
    if [[ "${_node_kind}" == cloud ]]; then
        return
    fi
    if [[ "${_node_kind}" == vm ]]; then
        grep -qs " /srv/www/htdocs/sources fuse.sshfs " "${_root}/etc/fstab" \
            || echo "${_what} /srv/www/htdocs/sources fuse.sshfs  noauto,x-systemd.automount,${_SSHFS_OPTS} 0 0" >> "${_root}/etc/fstab"
        return
    fi
    mkdir -p "${_root}/etc/systemd/system"
    cat > "${_root}/etc/systemd/system/srv-www-htdocs-sources.mount" <<EOF
[Unit]
Description=Hypervisor source images (sshfs)
After=network-online.target

[Mount]
What=${_what}
Where=/srv/www/htdocs/sources
Type=fuse.sshfs
Options=${_SSHFS_OPTS}
EOF
    cat > "${_root}/etc/systemd/system/srv-www-htdocs-sources.automount" <<EOF
[Unit]
Description=Hypervisor source images (sshfs), mounted on first access

[Automount]
Where=/srv/www/htdocs/sources

[Install]
WantedBy=multi-user.target
EOF
}

# Writes /etc/resolv.conf under ROOT: the node's own named first in a container or pod; on a VM only _mydns, since
# named is not running yet while packages are installed (NetworkManager adds _myip once the VM runs).
function write_resolv_conf() {
    local _root="$1"
    mkdir -p "${_root}/etc"
    [[ -L "${_root}/etc/resolv.conf" ]] && rm -f "${_root}/etc/resolv.conf"
    {
        echo "search ${_mydomain}"
        [[ "${_node_kind}" == vm ]] || echo "nameserver 127.0.0.1"
        echo "nameserver ${_mydns}"
    } > "${_root}/etc/resolv.conf"
}

# Writes a VM's hostname, keymap, static network configuration (_node_netstack: nm or wicked) under ROOT.
function write_vm_system() {
    local _root="$1"
    mkdir -p "${_root}/etc"
    echo "${AUTOMATION_HOSTNAME}" > "${_root}/etc/hostname"
    grep -qs "^KEYMAP=" "${_root}/etc/vconsole.conf" || echo "KEYMAP=us" >> "${_root}/etc/vconsole.conf"
    if [[ "${_node_netstack}" == wicked ]]; then
        mkdir -p "${_root}/etc/sysconfig/network"
        printf "BOOTPROTO='static'\nIPADDR='%s'\nPREFIXLEN='%s'\nSTARTMODE='auto'\n" "${_myip}" "${_mymask_cidr}" \
            > "${_root}/etc/sysconfig/network/ifcfg-eth0"
        echo "default ${_mygw} - -" > "${_root}/etc/sysconfig/network/routes"
    else
        mkdir -p "${_root}/etc/NetworkManager/system-connections"
        cat > "${_root}/etc/NetworkManager/system-connections/static.nmconnection" <<EOF
[connection]
id=static
type=ethernet
autoconnect=true

[ipv4]
method=manual
dns-search=${_mydomain}
dns=${_myip};${_mydns}
address1=${_myip}/${_mymask_cidr}
gateway=${_mygw}
EOF
        chmod 0600 "${_root}/etc/NetworkManager/system-connections/static.nmconnection"
    fi
}

# Appends each key given as an argument to ROOT/root/.ssh/authorized_keys unless it is there already.
function write_authorized_keys() {
    local _root="$1" _f="$1/root/.ssh/authorized_keys" _k
    shift
    mkdir -p "${_root}/root/.ssh"
    chmod 0700 "${_root}/root/.ssh"
    touch "${_f}"
    for _k in "$@"; do
        [[ -z "${_k}" ]] || grep -qxF "${_k}" "${_f}" || echo "${_k}" >> "${_f}"
    done
    chmod 0600 "${_f}"
}

# Activates _SLES_PRODUCTS on a registered SLES node, best effort: a product that cannot be activated leaves the
# package installation to name what is missing. Does nothing on other distributions or an unregistered SLES.
# $1: the os-release file to read (default /etc/os-release).
function node_enable_products() {
    local _os_release="${1:-/etc/os-release}" _id="" _ver="" _p
    _id="$(. "${_os_release}" && echo "${ID}")"
    [[ "${_id}" == sles ]] || return 0
    if ! command -v SUSEConnect >/dev/null || ! SUSEConnect --status 2>/dev/null | grep -q '"status":"Registered"'; then
        echo "WARNING: this SLES node is not registered; ${_SLES_PRODUCTS} cannot be activated" >&2
        return 0
    fi
    _ver="$(. "${_os_release}" && echo "${VERSION_ID}")"
    for _p in ${_SLES_PRODUCTS}; do
        SUSEConnect --product "${_p}/${_ver}/$(uname -m)" \
            || echo "WARNING: activating ${_p}/${_ver} failed; the packages only it provides will be missing" >&2
    done
}

# Installs the VM's packages and sets which services start at boot.
function node_install_packages() {
    local _pkgs="${_VM_PACKAGES}" _version=""
    node_enable_products
    _version="$(. /etc/os-release && echo "${VERSION_ID%%.*}")"
    case "${_version}" in
        16) _pkgs+=" ${_LEAP16_PACKAGES}"; _python_bin="${_python_bin:-${_LEAP16_PYTHON}}" ;;
        *)  _pkgs+=" ${_LEAP15_PACKAGES}"; _python_bin="${_python_bin:-${_LEAP15_PYTHON}}" ;;
    esac
    [[ "${_node_netstack}" == wicked ]] || _pkgs+=" NetworkManager"
    # shellcheck disable=SC2086
    zypper --non-interactive --gpg-auto-import-keys install -y ${_pkgs} || return 1
    # Leap 16 packages no kubernetes client, so the pinned upstream binary is installed instead. The script comes
    # from libs/kvm_host_profiles.py's kubectl_binary_script(), the same one the container image is built with, so
    # the version is pinned in exactly one place.
    if ! command -v kubectl >/dev/null && [[ -n "${_kubectl_install}" ]]; then
        bash -c "${_kubectl_install}" || { echo "ERROR: installing kubectl failed" >&2; return 1; }
    fi
    systemctl disable jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null
    systemctl disable --now firewalld.service 2>/dev/null
    if [[ "${_node_netstack}" != wicked ]]; then
        systemctl disable wicked.service 2>/dev/null
        systemctl enable NetworkManager.service
    fi
    systemctl enable sshd.service named.service apache2.service
}

# Creates the node's own SSH key once and publishes its public half for the lab VMs.
function node_ssh_key() {
    [[ -f /root/.ssh/id_rsa ]] || ssh-keygen -q -b 4096 -N '' -t rsa -f /root/.ssh/id_rsa
    mkdir -p /srv/www/htdocs
    cp /root/.ssh/id_rsa.pub /srv/www/htdocs/id_rsa.pub && chmod 0644 /srv/www/htdocs/id_rsa.pub
}

# Writes ROOT/etc/lab_creation.cfg from ROOT/etc/lab_creation.cfg.example (installed by install_automation_node_scripts.sh)
# with this node's values, only when it does not exist: an existing one belongs to its operator and is kept. Set:
# ROOT_SSH_KEY (the operator's key, for the lab VMs), REMOTE_HOST (the KVM host; its line and VIRT_SRV's are commented
# out on a node without one), ROOT_PWD_HASH (commented out when there is none, never left at the example's), MYREG
# and mysource. SUSE_email/SUSE_regcode are left empty: credentials live in the encrypted store (setup_credentials.py).
function write_lab_creation_cfg() {
    local _root="$1" _cfg="$1/etc/lab_creation.cfg" _example="$1/etc/lab_creation.cfg.example" _host
    [[ -e "${_cfg}" ]] && return 0
    if [[ ! -f "${_example}" ]]; then
        echo "WARNING: ${_example} not found; /etc/lab_creation.cfg is not written" >&2
        return 0
    fi
    _host="${_host_fqdn:-${_host_ip}}"
    ( umask 077
      awk -v key="${ROOT_SSH_PUB_KEY}" -v host="${_host}" -v hash="${ROOT_PWD_HASH}" -v reg="${MYREG}" \
          -v src="${AUTOMATION_HOSTNAME}" '
          function q(v) { gsub(/\047/, "\047\\\047\047", v); return "\047" v "\047" }
          /^ROOT_SSH_KEY=/  { print "ROOT_SSH_KEY=" q(key); next }
          /^REMOTE_HOST=/   { print (host == "" ? "#" : "") "REMOTE_HOST=" q(host); next }
          /^VIRT_SRV=/      { print (host == "" ? "#" : "") $0; next }
          /^SUSE_email=/    { print "SUSE_email=\047\047"; next }
          /^SUSE_regcode=/  { print "SUSE_regcode=\047\047"; next }
          /^ROOT_PWD_HASH=/ { print (hash == "" ? "#ROOT_PWD_HASH=\047\047" : "ROOT_PWD_HASH=" q(hash)); next }
          /^MYREG=/         { print "MYREG=" q(reg); next }
          /^mysource=/      { print "mysource=" q(src); next }
          { print }' "${_example}" > "${_cfg}" )
}

# Installs the lab scripts from NODE_DIR/lab-in-a-box.
function node_install_lab_scripts() {
    (cd "${NODE_DIR}/lab-in-a-box" \
        && _python_bin="${_python_bin}" _tls_ips="${_myip}" _backup=off bash install_automation_node_scripts.sh)
}

# Configures the node from NODE_DIR. $1: --offline or --boot (see the header), or nothing.
function configure_node() {
    local _mode="${1:-}" _services="named apache2 sshd"
    # shellcheck source=/dev/null
    . "${NODE_DIR}/settings" || return 1
    if [[ "${_node_kind}" == vm ]]; then
        write_resolv_conf /
        write_vm_system /
        node_install_packages || { echo "ERROR: installing the automation node's packages failed" >&2; return 1; }
    elif [[ "${_node_kind}" == cloud ]]; then
        # A plain provider image needs the same packages a VM image does, but its address, routes and hostname come
        # from the provider over DHCP, so write_vm_system's static configuration would break its networking.
        write_resolv_conf /
        node_install_packages || { echo "ERROR: installing the automation node's packages failed" >&2; return 1; }
    else
        write_resolv_conf /
    fi
    ln -sf "/usr/share/zoneinfo/${_timezone:-Europe/Zurich}" /etc/localtime
    write_dns_files /
    write_helm_scripts /
    [[ "${_mode}" == --boot ]] || download_helm_files /srv/www/htdocs/helm
    write_sshfs_mount /
    write_authorized_keys / "${ROOT_SSH_PUB_KEY}" "${HOST_SSH_PUB_KEY}"
    # Only when there is a hash to set. "chpasswd -e" with an empty value writes an empty password field,
    # which is a passwordless root account, not a locked one.
    [[ -z "${ROOT_PWD_HASH}" ]] || echo "root:${ROOT_PWD_HASH}" | chpasswd -e
    node_ssh_key
    node_install_lab_scripts || { echo "ERROR: install_automation_node_scripts.sh failed" >&2; return 1; }
    write_lab_creation_cfg /
    if [[ "${_mode}" == --offline ]]; then
        return 0
    fi
    systemctl daemon-reload
    if [[ "${_node_kind}" == cloud ]]; then
        :                                     # no hypervisor source images to mount, see write_sshfs_mount
    elif [[ "${_node_kind}" == vm ]]; then
        systemctl start srv-www-htdocs-sources.automount
    else
        systemctl enable --now srv-www-htdocs-sources.automount
    fi
    # A container on its host's network (_node_network=host) shares the host's port 22: it runs no sshd, the host's
    # wrappers reach it with podman exec instead.
    if [[ "${_node_network}" == host ]]; then
        systemctl disable --now sshd.service 2>/dev/null
        # It failed at the container's first start, before this disabled it: that state is cleared.
        systemctl reset-failed sshd.service 2>/dev/null
        _services="named apache2"
    fi
    if [[ "${_mode}" == --boot ]]; then
        # shellcheck disable=SC2086
        systemctl restart --no-block ${_services}
    else
        # shellcheck disable=SC2086
        systemctl restart ${_services}
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    case "${1:-}" in
        configure) shift; configure_node "$@" ;;
        *) echo "Usage: $0 configure [--offline|--boot]" >&2; exit 2 ;;
    esac
fi
