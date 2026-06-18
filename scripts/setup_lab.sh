#!/bin/bash
# Part of lab-in-a-box, it will setup a Lab
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

if jq -e '.kclusters' < "${inputFile}" &>/dev/null
then
  _msg="Setup lab \"\e[1;91m${lab_name:-inputFile}\e[0m\" (VMs + Kubernetes clusters)" show_nicer_messages
else
  _msg="Setup lab \"\e[1;91m${lab_name:-inputFile}\e[0m\" (VMs only)" show_nicer_messages
fi


# Register kcluster DNS entries before creating VMs
if jq -e '.kclusters' < "${inputFile}" &>/dev/null
then
  _msg="Add Kubernetes cluster DNS entries" show_nicer_messages
  ((_lvl++))
  list_kclusters | while read clu_name
  do
    load_kclu_vars
    add_kclu_dns
  done
  ((_lvl--))
fi


# Create all VMs
_msg="Creating VMs" show_nicer_messages
((_lvl++))
for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
do
        load_vm_vars
        load_def
        _msg="Node: \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        ssh-keygen -f ~/.ssh/known_hosts -R "${_vm_name}"
        destroy_vm.sh "${inputFile}" "${_vm_name}"
        setup_vm.sh "${inputFile}" "${_vm_name}"
done
((_lvl--))


# Kubernetes cluster setup — only when kclusters are defined
if jq -e '.kclusters' < "${inputFile}" &>/dev/null
then

  # Reboot all nodes to apply initial config
  ((_lvl++))
  for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
  do
        load_vm_vars
        load_def
        _msg="Restart node \e[1;91m${_vm_name}\e[0m" show_nicer_messages
        $ssh_command 'reboot'
  done
  ((_lvl--))

  sleep 5
  _msg="Waiting for nodes to come back online" show_nicer_messages
  ((_lvl++))
  for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
  do
        load_vm_vars
        load_def
        check_ssh_conn
  done
  ((_lvl--))

  # Install Kubernetes on each node
  ((_lvl++))
  for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
  do
        load_vm_vars
        load_def
        if [[ "${clu_name}" == "" ]]
        then
          _msg="\e[1;91mWARNING\e[0m no kcluster defined for \e[1;91m${_vm_name}\e[0m, SKIPPING" show_nicer_messages
          continue
        fi
        load_kclu_vars
        if [[ "${clu_type}" == "" ]]
        then
          _msg="\e[1;91mWARNING\e[0m clu_type is not defined for \e[1;91m${_vm_name}\e[0m, SKIPPING" show_nicer_messages
        else
          _msg="Installing \"\e[1;91m${clu_type}\e[0m\" on node \e[1;91m${_vm_name}\e[0m for cluster \e[1;91m${clu_name}\e[0m" show_nicer_messages
          setup_${clu_type} || _msg="setup_${clu_type} failed on \e[1;91m${_vm_name}\e[0m" fail_with_error
        fi
  done
  ((_lvl--))

  _msg="Wait $((2 + delay_min)) min for cluster/s to stabilise" show_nicer_messages
  sleep $((60 * (2 + delay_min)))

  # Install cluster-level addons (one kcluster at a time, with mgm_node support and dedup)
  _msg="Installing Kubernetes cluster/s addon/s" show_nicer_messages
  ((_lvl++))
  list_kclusters | while read clu_name
  do
    installed_addons=""
    load_kclu_vars

    # Determine which node runs the addon installer
    if [[ "${mgm_node}" == "" ]]
    then
      for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
      do
        if [[ "$(get_vm_kcluster)" == "${clu_name}" ]]
        then
          break
        fi
      done
    else
      _vm_name="${mgm_node}"
    fi

    _cluster_addons="$(jq -r ".kclusters[\"${clu_name}\"].addons // [] | .[]" < "${inputFile}" 2>/dev/null)"
    if [[ "${_cluster_addons}" != "" ]]
    then
      load_vm_vars
      load_def
      _msg="Installing cluster \e[1;91m\"${clu_name}\"\e[0m addon/s from \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
      ((_lvl++))
      while read _addon
      do
        if [[ " ${installed_addons} " != *" ${_addon} "* ]]
        then
          if command -v install_${_addon} &>/dev/null
          then
            _msg="Running addon \e[1;91m\"${_addon}\"\e[0m on \e[1;91m\"${_vm_name}\"\e[0m for cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
            _vm_name=${_vm_name} clu_name=${clu_name} install_${_addon} "${inputFile}"
            installed_addons="${installed_addons} ${_addon}"
          else
            _msg="FAILED! Addon script \e[1;91m\"install_${_addon}\"\e[0m not found" fail_with_error
          fi
        fi
      done <<< "${_cluster_addons}"
      ((_lvl--))
    else
      _msg="No Kubernetes cluster addons for \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
    fi
  done
  ((_lvl--))

fi


# Install per-VM addons (node-level addons from .nodes[vm].addons[])
_msg="Installing VM addons if any" show_nicer_messages
((_lvl++))
for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
do
  load_vm_vars
  load_def
  _vm_addons="$(jq -r ".nodes[\"${_vm_name}\"].addons // [] | .[]" < "${inputFile}" 2>/dev/null)"
  if [[ "${_vm_addons}" != "" ]]
  then
    _msg="Installing VM \"\e[1;91m${_vm_name}\e[0m\" addons" show_nicer_messages
    ((_lvl++))
    while read _addon
    do
      if command -v install_${_addon} &>/dev/null
      then
        _msg="Running addon \e[1;91m\"${_addon}\"\e[0m on \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        _vm_name=${_vm_name} install_${_addon} "${inputFile}"
      else
        _msg="Addon script \e[1;91m\"install_${_addon}\"\e[0m not found" fail_with_error
      fi
    done <<< "${_vm_addons}"
    ((_lvl--))
  else
    _msg="No VM addons for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
  fi
done
((_lvl--))
