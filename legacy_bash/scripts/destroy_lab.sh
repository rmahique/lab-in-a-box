#!/bin/bash
# Part of lab-in-a-box, it will destroy all VMs defined in a lab JSON file
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


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

# Load primary functions (also validates inputFile, loads lab_creation.cfg and the main lib)
. ${_primary_funtions} || exit 1


lab_name="$(jq -r '.common.lab_name' < "${inputFile}" 2>/dev/null)"

_msg="Destroy lab \"\e[1;91m${lab_name}\e[0m\"" show_nicer_messages
((_lvl++))
for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
do
        _msg="Node: \e[1;91m${_vm_name}\e[0m" show_nicer_messages
        ssh-keygen -f ~/.ssh/known_hosts -R "${_vm_name}"
        destroy_vm.sh "${inputFile}" "${_vm_name}"
done
((_lvl--))
