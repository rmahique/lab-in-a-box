#!/bin/bash
# Part of lab-in-a-box, contains basic commands and functions to save space on your shell scripts
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.



inputFile=${1}


typeset usage 2>&1 | grep -q 'function' || function usage() { echo -e "Usage:\n$0 <configuration file>"; }


if [[ ! ${inputFile} ]]
then
        echo "ERROR: Missing parameter"
        usage
        exit 1
fi
if [[ ! -f "${inputFile}" ]]
then
   echo "ERROR: Lab definition file (${inputFile}) doesn't exists"
   usage
   exit 1
elif ! jq <"${inputFile}" >/dev/null
then
   echo "ERROR: Lab definition not in validated JSON format"
   exit 1
fi





# load lab_creation config
if [[ -f /etc/lab_creation.cfg ]]
then
        . /etc/lab_creation.cfg
elif [[ -f lab_creation.cfg ]]
then
        . lab_creation.cfg
else
        echo "ERROR: Configuration file lab_creation.cfg not found in local path or /etc"
        exit 1
fi

if [[ ! -f ${_lib_path}/lab_creation.bash ]]
then
        echo "ERROR: Library \"${_lib_path}/lab_creation.bash\" not found"
        exit 1
else
        . ${_lib_path}/lab_creation.bash
fi





function load_def(){
        ssh_command="ssh  -o StrictHostKeyChecking=accept-new -q  root@${_vm_name}"
}




