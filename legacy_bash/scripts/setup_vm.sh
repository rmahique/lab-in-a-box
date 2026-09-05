#!/bin/bash
# Part of lab-in-a-box, it will setup a VM
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.



_SCHEMA_VERSION="1.0"

if [[ "${1}" == "--help" ]]; then
    cat <<'EOF'
Usage: setup_vm.sh <lab.json> <vm_hostname>

Provisions a single VM: copies the disk image, generates provisioning files
(Ignition+Combustion or cloud-init), registers DNS, and calls virt-install.

Run 'setup_lab.sh --input-definition [json|yaml]' for the full lab definition schema.
EOF
    exit 0
fi

if [[ "${1}" == "--input-definition" || "${1}" == "--schema" ]]; then
    exec setup_lab.sh --input-definition "${2:-json}"
fi

inputFile=${1}
_vm_name=${2}

function usage() {
        echo "Usage:
$0 <configuration file> <vm_name>"

}


if [[ ! ${inputFile} ]]
then
        _msg="ERROR: missing configuration file parameter" show_nicer_messages
        usage
        exit 1
fi
if [[ ! -f ${inputFile} ]]
then
        _msg="ERROR: configuration file \"${inputFile}\" not found or name incorrect" show_nicer_messages
        usage
        exit 1
elif ! jq <"${inputFile}" >/dev/null
then
   _msg="Lab definition not in validated JSON format" show_nicer_messages
   exit 1
fi

if [[ ! ${_vm_name} ]]
then
        _msg="ERROR: Missing \"VM name\" parameter" show_nicer_messages
        usage
        exit 1
fi





_VERSION="__LABVERSION__"
[[ "${1}" == "--version" || "${1}" == "-v" ]] && echo "${0##*/} ${_VERSION}" && exit 0

# load lab_creation defaults
if [[ -f /etc/lab_creation.defaults ]]
then
        . /etc/lab_creation.defaults
elif [[ -f lab_creation.defaults ]]
then
        . lab_creation.defaults
else
        echo "ERROR: Configuration file lab_creation.defaults not found in local path or /etc"
        exit 1
fi

# Load primary functions
. ${_primary_funtions} || exit 1





validate_lab_definition "${inputFile}" "${_vm_name}" || exit 1

# load VM settings
load_vm_vars

IGN_FILE="${_vm_name}.ign"
COM_FILE="${_vm_name}"

# Validate MAC address: check for conflicts on the hypervisor, generate one if needed
check_or_generate_mac

# Define shortcut for the ssh command
ssh_command="ssh  -o StrictHostKeyChecking=accept-new root@${REMOTE_HOST}"


copy_vm_img

if [[ "$config_method" == "" ]]
then
  prepare_ign_and_cmb
else
  prepare_${config_method}
fi


copy_to_hypervisor

add_to_dns

create_vm

clean_ssh_keys





# Wait for it to come online
if check_ssh_conn
then
  reboot_vm
  if check_ssh_conn
  then
    _msg="\t\tVM \e[1;91m\"${_vm_name}\"\e[0m created" show_nicer_messages
  else
    _msg="VM \e[1;91m\"${_vm_name}\"\e[0m failed to come online" fail_with_error
  fi
else
  _msg="VM \e[1;91m\"${_vm_name}\"\e[0m failed to come online" fail_with_error
fi


