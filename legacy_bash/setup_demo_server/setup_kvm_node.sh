#!/bin/bash
# Prepares the hypervisor to work as lab_automation node
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.




function show_nicer_messages() {
  tput bold
  echo -e "\n###._ ${_msg} _.###\n"
  tput sgr0
}


# check which OS are we in
if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    _os="${ID}"
    _version_id="${VERSION_ID}"
    _arch="$(uname -m)"
fi

# Fail if not os detected
if [[ "$_os" == "" ]]
then
   echo -e '\033[1;31mERROR\033[0m: OS type not detected'
   exit 1
elif [[ "$_os" == "opensuse-leap" ]]
then
  _pkg_mgr="zypper "
  _pkgs="libvirt podman docker cri-tools minikube-bash-completion kubectl-who-can kubevirt-virtctl kubernetes1.28-client gpgme-devel device-mapper-devel libbtrfs-devel git-core mc bridge-utils tcpdump sensors ftsteutates-sensors netcat-openbsd gptfdisk libvirt-daemon-qemu qemu-tools virt-install libguestfs"
elif [[ "$_os" == "sles" ]]
then
  _pkg_mgr="zypper "
  _register_suse="1"
  _products="PackageHub sle-module-containers sle-module-basesystem sle-module-legacy"
  # NOTE: guestmount (used in setup_lab_automation.sh) requires libguestfs or guestfs-tools.
  # Verify the correct package name and availability in PackageHub before adding it here.
  _pkgs="libvirt podman docker cri-tools minikube-bash-completion kubectl-who-can kubevirt-virtctl kubernetes1.28-client gpgme-devel device-mapper-devel libbtrfs-devel git-core mc bridge-utils tcpdump sensors ftsteutates-sensors netcat-openbsd gptfdisk"
else
  echo -e "\033[1;31mERROR\033[0m: Unsupported OS '${_os}'. This script supports opensuse-leap and sles."
  exit 1
fi
echo "- Installing in ${PRETTY_NAME}"


function do_it_all() {
        if [[ ! -f setup_lab_automation.sh ]]
        then
                echo -e '\033[1;31mERROR\033[0m: Missing script, please download setup_lab_automation.sh script from the GIT repository'
		exit 1
        fi
        if [[ "${_register_suse}" != "" ]]
        then
          _msg='Configure package repositories' show_nicer_messages
          for _product in ${_products}
          do
            SUSEConnect --product ${_product}/${_version_id}/${_arch}
          done
        fi
        _msg='Update all packages and install necessary ones' show_nicer_messages
        ${_pkg_mgr} refresh
        ${_pkg_mgr} update -y
        ${_pkg_mgr} install -y ${_pkgs}

        _msg='Install yq' show_nicer_messages
        python3 -c "
import urllib.request, os, platform
arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), platform.machine())
urllib.request.urlretrieve('https://github.com/mikefarah/yq/releases/latest/download/yq_linux_' + arch, '/usr/local/bin/yq')
os.chmod('/usr/local/bin/yq', 0o755)
print('yq installed to /usr/local/bin/yq')
" || echo -e "\033[1;31mWARNING\033[0m: yq installation failed, some features may not work"

        [[ -d /var/lib/libvirt/images/sources/ ]] || mkdir -p /var/lib/libvirt/images/sources/

        _msg='Download image to be used for the automation VM' show_nicer_messages
        _qcow_basename="${_QCOW_IMAGE##*/}"
        _vm_ver=$(grep -oP '\d+\.\d+' <<< "${_qcow_basename}" | head -1)
        wget -nc -P /var/lib/libvirt/images/sources/ \
            "https://download.opensuse.org/distribution/leap/${_vm_ver}/appliances/${_qcow_basename}"

        cat > /etc/libvirt/storage/pool.xml << 'EOF'
<!--
WARNING: THIS IS AN AUTO-GENERATED FILE. CHANGES TO IT ARE LIKELY TO BE
OVERWRITTEN AND LOST. Changes to this xml configuration should be made using:
  virsh pool-edit pool
or other application using the libvirt API.
-->

<pool type='dir'>
  <name>pool</name>
  <uuid>8bd63226-f3e4-4a14-965f-a75673a1a291</uuid>
  <capacity unit='bytes'>0</capacity>
  <allocation unit='bytes'>0</allocation>
  <available unit='bytes'>0</available>
  <source>
  </source>
  <target>
    <path>/var/lib/libvirt/images/sources</path>
  </target>
</pool>
EOF

	ln -s /etc/libvirt/storage/pool.xml /etc/libvirt/storage/autostart/pool.xml &>/dev/null
        systemctl enable --now libvirtd
        systemctl disable --now firewalld

        _msg='Start setup_lab_automation.sh script to create the automation VM' show_nicer_messages
         bash setup_lab_automation.sh

}

[[ "${_currenttime}" == "" ]] && _currenttime="`date +%s`"
_input="$1"

if [[ -f lab.cfg ]]
then
        _msg="Loading configuration file lab.cfg" show_nicer_messages
	. lab.cfg
else
        _msg="\033[1;31mERROR\033[0m: Missing configuration file lab.cfg" show_nicer_messages
	exit 1
fi

if [[ "${_input}" != "" ]]
then
	if nc -z -w 5 "${_input}" 22 &>/dev/null
	then
                _msg="\n## Setting up ${_input} remotely ##\n" show_nicer_messages
	        ssh-copy-id root@${_input}
		if [[ "$?" != "0" ]]
		then
			echo -e "\033[1;31mERROR\033[0m: we need an SSH key to continue, to generate one please run ssh-keygen -t ed25519 -f ~/id_ed25519_lab -N ''"
			exit 1
		fi
	        ssh root@${_input} "mkdir -p /var/tmp/${0##*/}_${_currenttime}"
	        scp "$0" lab.cfg setup_lab_automation.sh root@${_input}:/var/tmp/${0##*/}_${_currenttime}/
	        ssh root@${_input} "cd /var/tmp/${0##*/}_${_currenttime}/ ; _currenttime=${_currenttime} bash ${0##*/} -y"
	elif [[ "${_input}" == "-y" ]]
	then
		do_it_all
	else
		echo -e "\033[1;31mERROR\033[0m: incorrect parameter \"${_input}\""
	fi
else
        read -p 'Are you sure? (yes/n): ' _response
        if [[ "${_response}" == "yes" ]]
        then
                do_it_all
        else
		echo "

Usage: $0 [-y] [<IP/hostname>]
-y Automatically accept
<IP/hostname> of the host you want to setup

"
                exit 0
        fi

fi


