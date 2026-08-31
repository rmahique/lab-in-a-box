#!/bin/bash
# Part of lab-in-a-box, it will create the automation VM that orchestrates the creation of the labs
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


if [[ -f lab.cfg ]]; then
    echo "Loading configuration file lab.cfg"
    . lab.cfg
else
    echo -e "\033[1;31mERROR\033[0m: Missing configuration file lab.cfg"
    exit 1
fi

function show_nicer_messages() {
    tput bold
    echo -e "\n###._ ${_msg} _.###\n"
    tput sgr0
}

# Derive CIDR prefix length from _mynet (e.g. 192.168.8.0/24 → "24")
_mymask_cidr="${_mynet##*/}"

# Auto-detect KVM bridge if _bridge_name not set in lab.cfg
function detect_bridge() {
    [[ -n "${_bridge_name}" ]] && return
    _bridge_name=$(ip -br link show type bridge | awk 'NR==1{print $1}')
    [[ -z "${_bridge_name}" ]] && _bridge_name="br0"
    echo "Using bridge: ${_bridge_name}"
}

# Create _bridge_name and enslave _bridge_nic to it, if _bridge_nic is set in
# lab.cfg (empty = skip entirely, matching detect_bridge()'s existing
# assume-it-already-exists default). Mirrors
# kvm_host_profiles.py's configure_bridge(): nmcli when NetworkManager is
# live, wicked ifcfg files otherwise — which stack is live is a runtime
# question, not an OS-version one, so it's detected here rather than
# hardcoded per OS.
function configure_bridge() {
    [[ -z "${_bridge_nic}" ]] && return
    _msg="Configure network bridge ${_bridge_name} (${_bridge_nic})" show_nicer_messages
    if systemctl is-active --quiet NetworkManager; then
        nmcli con add type bridge con-name "${_bridge_name}" ifname "${_bridge_name}"
        nmcli con add type bridge-slave ifname "${_bridge_nic}" master "${_bridge_name}"
        nmcli con up "${_bridge_name}"
    elif systemctl is-active --quiet wickedd; then
        cat > "/etc/sysconfig/network/ifcfg-${_bridge_name}" <<EOF
BOOTPROTO='dhcp'
STARTMODE='auto'
BRIDGE='yes'
BRIDGE_PORTS='${_bridge_nic}'
EOF
        cat > "/etc/sysconfig/network/ifcfg-${_bridge_nic}" <<EOF
BOOTPROTO='none'
STARTMODE='auto'
EOF
        wicked ifreload all
    else
        echo -e "\033[1;31mERROR\033[0m: neither NetworkManager nor wicked is active — cannot configure bridge ${_bridge_name}" >&2
        exit 1
    fi
}

# Derive MAC from last 3 octets of _myip using QEMU OUI (52:54:00)
function generate_mac() {
    [[ -n "${_automation_mac}" ]] && return
    _automation_mac=$(printf "52:54:00:%02x:%02x:%02x" \
        $(echo "${_myip}" | awk -F. '{print $2, $3, $4}'))
}

# Get virt-install --osinfo from _QCOW_IMAGE filename; fall back to nearest supported version
function detect_vm_osinfo() {
    local _vm_ver _fb
    _vm_ver=$(grep -oP '\d+\.\d+' <<< "${_QCOW_IMAGE##*/}" | head -1)
    _vm_osinfo="opensuse${_vm_ver:-15.5}"
    if command -v osinfo-query &>/dev/null; then
        if ! osinfo-query os short-id="${_vm_osinfo}" &>/dev/null 2>&1; then
            # 16.0 added explicitly rather than derived — osinfo-query's own
            # database may simply lack an "opensuse16.0" short-id regardless
            # of whether the OS itself is fine, so this is a real fallback
            # entry, not a guess.
            for _fb in 16.0 15.5 15.4 15.3; do
                if osinfo-query os short-id="opensuse${_fb}" &>/dev/null 2>&1; then
                    _vm_osinfo="opensuse${_fb}"
                    break
                fi
            done
        fi
    fi
}

function configure_image() {
    _msg="Copy image and resize" show_nicer_messages
    cp "${_QCOW_IMAGE}" /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2
    qemu-img resize /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 ${_disk_size:-40}G
    trap 'guestunmount /mnt 2>/dev/null' EXIT
    _msg="Mount image for configuration" show_nicer_messages
    guestmount -i --rw -a /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 /mnt/
}

function configure_os() {
    rm /mnt/var/lib/YaST2/reconfig_system
    cp /etc/resolv.conf /mnt/etc/

    echo "${AUTOMATION_HOSTNAME}" > /mnt/etc/hostname

    umask 077
    mkdir -p /mnt/etc/NetworkManager/system-connections/
    cat > /mnt/etc/NetworkManager/system-connections/static.nmconnection <<EOF
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
    umask 022

    echo "KEYMAP=us" >> /mnt/etc/vconsole.conf
    ln -sf "/usr/share/zoneinfo/${_timezone:-Europe/Zurich}" /mnt/etc/localtime
}

function install_packages() {
    _msg="Install required packages" show_nicer_messages
    chroot /mnt/ zypper install -y vim-small git rsync apache2 bind-utils bind docker podman \
        libvirt-client jq NetworkManager virt-install salt-ssh ipcalc fuse3 sshfs netcat-openbsd
    _msg="Enable/Disable services" show_nicer_messages
    chroot /mnt/ bash -c "
        systemctl disable firewalld.service wicked.service
        systemctl enable sshd.service NetworkManager.service named apache2
    "
}

function configure_ssh() {
    _msg="Generate SSH key" show_nicer_messages
    chroot /mnt/ ssh-keygen -b 4096 -N '' -t rsa -f /root/.ssh/id_rsa
    cp /mnt/root/.ssh/id_rsa.pub /mnt/srv/www/htdocs/ && chmod 0644 /mnt/srv/www/htdocs/id_rsa.pub
    _msg="Setup SSH keys" show_nicer_messages
    cat /mnt/root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys
    echo -e "\n# Automation VM public key:\n$(cat /mnt/root/.ssh/id_rsa.pub)\n"
    echo "${ROOT_SSH_PUB_KEY}" >> /mnt/root/.ssh/authorized_keys
    echo "root:${root_pwd}" | chroot /mnt/ chpasswd -c SHA512
}

function install_lab_scripts() {
    _msg="Clone repository" show_nicer_messages
    git clone https://github.com/SUSE-Technical-Marketing/lab-in-a-box.git /mnt/var/tmp/lab-in-a-box
    export _scripts_path=/var/tmp/lab-in-a-box/
    _msg="Run install_automation_node_scripts.sh" show_nicer_messages
    chroot /mnt/ bash /var/tmp/lab-in-a-box/install_automation_node_scripts.sh
}

function configure_helm() {
    _msg="Download latest helm and install it" show_nicer_messages
    mkdir -p /mnt/srv/www/htdocs/helm /mnt/srv/www/sources
    chmod 0755 /mnt/srv/www/htdocs/helm /mnt/srv/www/sources
    curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS \
        --output /mnt/srv/www/htdocs/helm/KEYS
    chmod 0644 /mnt/srv/www/htdocs/helm/KEYS
    curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail \
        'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-${myarch:-amd64}.tar.gz" \
        --output /mnt/srv/www/htdocs/helm/helm-latest-linux-${myarch:-amd64}.tar.gz

    # download_latest_helm.sh: arch embedded now, helm version resolved at runtime
    cat > /mnt/usr/local/bin/download_latest_helm.sh << 'HELMSCRIPT'
#!/bin/bash
curl -k "https://get.helm.sh/helm-$(curl -L --silent --show-error --fail 'https://get.helm.sh/helm-latest-version' 2>&1 | grep '^v[0-9]')-linux-MYARCH.tar.gz" \
    --output /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
curl -k https://raw.githubusercontent.com/helm/helm/main/KEYS --output /srv/www/htdocs/helm/KEYS
chmod 0644 /srv/www/htdocs/helm/KEYS /srv/www/htdocs/helm/helm-latest-linux-MYARCH.tar.gz
HELMSCRIPT
    sed -i "s/MYARCH/${myarch:-amd64}/g" /mnt/usr/local/bin/download_latest_helm.sh

    # install_helm.sh: served by the automation VM, run on client nodes
    cat > /mnt/srv/www/htdocs/helm/install_helm.sh << HELMSCRIPT
#!/bin/bash
[ -d /tmp/helm ] || mkdir /tmp/helm
curl -SsL "http://${AUTOMATION_HOSTNAME}/helm/helm-latest-linux-${myarch:-amd64}.tar.gz" -o /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz
tar xf /tmp/helm/helm-latest-linux-${myarch:-amd64}.tar.gz -C /tmp/helm
cp /tmp/helm/linux-${myarch:-amd64}/helm /usr/local/bin
HELMSCRIPT

    chmod 0755 /mnt/usr/local/bin/download_latest_helm.sh /mnt/srv/www/htdocs/helm/install_helm.sh
}

function configure_sshfs() {
    echo "${_virt_srv:-root@hypervisor}:/var/lib/libvirt/images/sources /srv/www/htdocs/sources fuse.sshfs  noauto,x-systemd.automount,_netdev,reconnect,identityfile=/root/.ssh/id_rsa,allow_other,default_permissions 0 0" >> /mnt/etc/fstab
}

function configure_dns_server() {
    _msg="Configure DNS server" show_nicer_messages
    cat > /mnt/etc/named.conf <<EOF
options {
        directory "/var/lib/named";
        managed-keys-directory "/var/lib/named/dyn/";
        dump-file "/var/log/named_dump.db";
        statistics-file "/var/log/named.stats";
        listen-on port 53 { any; };
        listen-on-v6 { any; };
        allow-query { 127.0.0.1; 0.0.0.0/0; };
        recursion yes;
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
zone "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa" IN {
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

    cat > /mnt/var/lib/named/${_mynetrev}.db <<EOF
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
$(ip -4 --brief a show "${_bridge_name}" primary | awk -F'.' '{print $NF}' | cut -d/ -f1)      IN  PTR     $(hostname -f).

EOF

    cat > /mnt/var/lib/named/${_mydomain}.lan <<EOF
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
$(hostname)         IN  A       $(getent hosts "${HOSTNAME}" | awk '{print $1; exit}')

EOF

    chmod 0644 /mnt/var/lib/named/${_mydomain}.lan /mnt/var/lib/named/${_mynetrev}.db
}

function unmount_image() {
    sync
    guestunmount /mnt
    trap - EXIT
}

# "bridge" (default, today's exact unchanged behavior) puts the automation
# VM directly on _bridge_name; "nat" is the extra, opt-in alternative (see
# lab.cfg.template's own comment) that attaches it to the libvirt NAT'd
# virtual network setup_kvm_node.py's configure_nat_network() already
# defined instead — same --network flag shape virt-install already expects
# for a named libvirt network (network=<name> instead of bridge=<name>).
function vm_network_arg() {
    if [[ "${_network_mode:-bridge}" == "nat" ]]; then
        echo "network=${_nat_network_name:-labnat},mac.address=${_automation_mac}"
    else
        echo "bridge=${_bridge_name},mac.address=${_automation_mac}"
    fi
}

function create_vm() {
    _msg="Create virtual machine" show_nicer_messages
    # lab.cfg's _automation_graphics — default stays "spice" (unchanged behavior),
    # but it's a config knob now, not hardcoded: spice depends on QEMU having been
    # built with spice support, which isn't guaranteed on a minimal host install
    # (confirmed live 2026-08-30: virt-install failed outright with "spice graphics
    # are not supported with this QEMU" on an openSUSE Leap Minimal-VM Cloud host,
    # since libvirt-daemon-qemu's spice UI packages are only a weak zypper
    # Recommends there) — set _automation_graphics=none in lab.cfg on a host like
    # that instead of installing the extra spice packages.
    virt-install --connect ${_qemu_addr} \
        --name "${AUTOMATION_HOSTNAME}" \
        --autostart \
        --vcpus 1 \
        --memory 2048 \
        --osinfo="${_vm_osinfo}" \
        --import \
        --disk "size=${_disk_size:-40},path=/var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2,sparse=no,boot.order=1" \
        --graphics="${_automation_graphics:-spice}" \
        --network "$(vm_network_arg)" \
        --noautoconsole
}

# Only relevant when _network_mode=nat: forward the automation VM's own
# ports in from the KVM host's real IP, reusing the exact same rule-building
# function libs/services.py's PortForwardService calls for lab nodes later
# (libs/portforward.py's apply_forwarded_ports()) — invoked here as a plain
# python3.11 call since this bash script has no importable module context
# of its own. _SLA_DIR resolves this script's own directory regardless of
# the caller's cwd (setup_kvm_node.py's do_it_all() runs this with cwd set
# to this same directory, but this stays correct even if invoked directly).
_SLA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

function configure_nat_port_forwarding() {
    [[ "${_network_mode:-bridge}" != "nat" ]] && return
    _msg="Forward automation VM ports from the KVM host" show_nicer_messages
    python3.11 -c "
import sys
sys.path.insert(0, '${_SLA_DIR}/../libs')
import portforward
portforward.apply_forwarded_ports({'${_myip}': '${_nat_forwarded_ports:-22:22/TCP 80:80/TCP 443:443/TCP}'.split()})
"
}

function wait_for_vm() {
    _msg="Waiting for ${AUTOMATION_HOSTNAME} to come online" show_nicer_messages
    local _count=0
    while ! nc -z -w 2 "${_myip}" 22 &>/dev/null; do
        ((_count++))
        if [[ $_count -gt 60 ]]; then
            echo -e "\033[1;31mERROR\033[0m: Timeout waiting for ${AUTOMATION_HOSTNAME} (${_myip})"
            exit 1
        fi
        sleep 5
    done
    echo "${AUTOMATION_HOSTNAME} is online"
}

function configure_host_dns() {
    _msg="Reconfigure host to use new VM as DNS server" show_nicer_messages
    sed "s/NETCONFIG_DNS_STATIC_SERVERS=.*/NETCONFIG_DNS_STATIC_SERVERS=\"${_myip} ${_mydns}\"/;s/NETCONFIG_DNS_STATIC_SEARCHLIST=.*/NETCONFIG_DNS_STATIC_SEARCHLIST=\"${_mydomain}\"/" \
        -i /etc/sysconfig/network/config
    if grep -i "^search " /etc/resolv.conf &>/dev/null; then
        sed "/search.*/a nameserver ${_myip} " -i /etc/resolv.conf
    else
        sed "1s/^/search ${_mydomain}/" -i /etc/resolv.conf
        sed "1s/^/nameserver ${_myip}/" -i /etc/resolv.conf
    fi
}


# --- Main ---

_msg="Delete VM \"${AUTOMATION_HOSTNAME}\" if it exists" show_nicer_messages
if virsh desc "${AUTOMATION_HOSTNAME}" &>/dev/null; then
    virsh -c ${_qemu_addr} destroy  "${AUTOMATION_HOSTNAME}" 2>/dev/null
    virsh -c ${_qemu_addr} undefine "${AUTOMATION_HOSTNAME}" --remove-all-storage
fi

detect_bridge
configure_bridge
generate_mac
detect_vm_osinfo

configure_image
configure_os
install_packages
configure_ssh
install_lab_scripts
configure_helm
configure_sshfs
configure_dns_server
unmount_image
create_vm
wait_for_vm
configure_nat_port_forwarding
configure_host_dns
