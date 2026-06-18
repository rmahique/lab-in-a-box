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
if [[ -f /etc/os-release ]]
then
  _os="`cat /etc/os-release | sed -n -e 's/^ID="\([-a-zA-Z].*\)"/\1/p'`"
  _version_id="`cat /etc/os-release | sed -n -e 's/^VERSION_ID="\([-a-zA-Z0-9].*\)"/\1/p'`"
  _arch="`arch`"
fi

# Fail if not os detected
if [[ "$_os" == "" ]]
then
   echo -e '\033ERROR\033[0m: OS type not detected'
   exit 1
elif [[ "$_os" == "opensuse-leap" ]]
then
  _pkg_mgr="zypper "
  _pkgs="libvirt podman docker cri-tools minikube-bash-completion kubectl-who-can kubevirt-virtctl kubernetes1.28-client gpgme-devel device-mapper-devel libbtrfs-devel git-core mc bridge-utils tcpdump sensors ftsteutates-sensors netcat-openbsd gptfdisk libvirt-daemon-qemu qemu-tools virt-install libguestfs"
  zypper install -y https://download.opensuse.org/repositories/utilities/${_version_id}/${_arch}/yq-4.44.6-lp156.42.1.${_arch}.rpm
elif [[ "$_os" == "sles" ]]
then
  _pkg_mgr="zypper "
  _register_suse="1"
  _products="PackageHub sle-module-containers sle-module-basesystem sle-module-legacy"
  _pkgs="libvirt podman docker cri-tools minikube-bash-completion kubectl-who-can kubevirt-virtctl kubernetes1.28-client gpgme-devel device-mapper-devel libbtrfs-devel git-core mc bridge-utils tcpdump sensors ftsteutates-sensors netcat-openbsd gptfdisk"
  zypper install -y https://download.opensuse.org/repositories/utilities/${_version_id}/${_arch}/yq-4.44.6-lp156.42.1.${_arch}.rpm
fi
echo "- Installing in `cat /etc/os-release | sed -n -e 's/^PRETTY_NAME="\([-a-zA-Z0-9].*\)"/\1/p'`"


function do_it_all() {
        if [[ ! -f setup_lab_automation.sh ]]
        then
                echo "\033[1;31mERROR\033[0m: Missing script, please download setup_lab_automation.sh script from the GIT repository"
		exit 1
        fi
        if [[ "${_register_suse}" != "" ]]
        then
          _msg="Configure package repositories" show_nicer_messages
          for _product in ${_products}
          do
            SUSEConnect --product ${_product}/${_version_id}/${_arch}
          done
        fi
        _msg="Update all packages and install necessary ones" show_nicer_messages
        ${_pkg_mgr} refresh
        ${_pkg_mgr} update -y
        ${_pkg_mgr} install -y ${_pkgs}

        [[ -d /var/lib/libvirt/images/sources/ ]] || mkdir -p /var/lib/libvirt/images/sources/

        _msg="Download openSUSE Leap image to be used for the VM" show_nicer_messages
        cd /var/lib/libvirt/images/sources/ && wget -nc https://download.opensuse.org/distribution/leap/15.5/appliances/openSUSE-Leap-15.5-Minimal-VM.${_arch}-kvm-and-xen.qcow2
        cd -

        echo '<!--
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
' >/etc/libvirt/storage/pool.xml

	ln -s /etc/libvirt/storage/pool.xml /etc/libvirt/storage/autostart/pool.xml &>/dev/null
        systemctl enable --now libvirtd
        systemctl disable --now firewalld

        _msg="Start setup_lab_automation.sh script to create the automation VM" show_nicer_messages
         bash setup_lab_automation.sh

}

[[ "${_currenttime}" == "" ]] && _currenttime="`date +%s`"
_input="$1"

if [[ -f lab.cfg ]]
then
        _msg="Loading configuration file lab.cfg" show_nicer_messages
	. lab.cfg
else
        _msg="\033ERROR\033[0m: Missing configuratoin file lab.cfg" show_nicer_messages
	exit 1
fi

if [[ "${_input}" != "" ]]
then
	if  ping -c 1 -q "${_input}" &>/dev/null
	then
                _msg="\n## Setting up ${_input} remotely ##\n" show_nicer_messages
	        ssh-copy-id root@${_input}
		if [[ "$?" != "0" ]]
		then
			echo "E\033[1;31mRROR\033[0m: we need an SSH key to continue, to generate one please run ssh-keygen -b 16384 -t rsa -a 100 -f ~/id_rsa_TESTTDELETEME -N ''"
			exit 1
		fi
	        ssh root@${_input} "mkdir /var/tmp/$0_${_currenttime}"
	        scp $0 lab.cfg setup_lab_automation.sh root@${_input}:/var/tmp/${0//*\/}_${_currenttime}/
	        ssh root@${_input} "cd /var/tmp/${0//*\/}_${_currenttime}/ ; _currenttime=${_currenttime} bash $0 -y"
	elif [[ "${_input}" == "-y" ]]
	then
		do_it_all
	else
		echo "E\033[1;31mRROR\033[0m: incorrect parameter \"${_input}\""
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


