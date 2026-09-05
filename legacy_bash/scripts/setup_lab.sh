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
_SCHEMA_VERSION="1.0"
[[ "${1}" == "--version" || "${1}" == "-v" ]] && echo "${0##*/} ${_VERSION}" && exit 0

if [[ "${1}" == "--help" ]]
then
    cat <<'EOF'
Usage: setup_lab.sh [--keep] <lab.json>

Provisions all VMs defined in the lab JSON, sets up Kubernetes clusters, and
installs cluster-level and VM-level addons in order.

Options:
  --keep    Skip VMs that already exist, are running, match the defined IP and
            MAC address, and are accessible via SSH with default credentials.
            Without this flag (default) every VM is destroyed and recreated.

The lab definition JSON must contain:
  nodes      — map of VM hostname → node config (myip, mymac, kcluster, …)
  common     — shared VM settings (ISO_IMAGE, VM_MEM, VM_DSK, VM_CPU, …)
  kclusters  — map of cluster name → cluster config (clu_type, addons, …)
  <addon>    — one section per addon listed in kclusters[x].addons or nodes[x].addons

Run 'install_<addon> --help' for the options accepted by each addon section.
Run 'setup_lab.sh --input-definition [json|yaml]' for the machine-readable schema.
EOF
    exit 0
fi

if [[ "${1}" == "--input-definition" || "${1}" == "--schema" ]]
then
    # Single source of truth for the base lab-definition schema lives in the
    # lab_schema library (lab_schema --base). The web builder imports the same
    # function in-process, so there is exactly one definition to maintain.
    lab_schema --base "${2:-json}"
    exit $?
fi

# Parse optional flags; rewrite $@ so primary_functions.bash sees only the JSON path as $1
_keep=0
_remaining_args=()
for _arg in "$@"; do
    case "${_arg}" in
        --keep) _keep=1 ;;
        *)      _remaining_args+=("${_arg}") ;;
    esac
done
set -- "${_remaining_args[@]}"

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


validate_lab_definition "${inputFile}" || exit 1

lab_name="$(jq -r '.common.lab_name' < "${inputFile}" 2>/dev/null || echo ${inputFile/*\/})"

if jq -e '.kclusters' < "${inputFile}" &>/dev/null
then
  _msg="Setup lab \"\e[1;91m${lab_name:-inputFile}\e[0m\" (VMs + Kubernetes clusters)" show_nicer_messages
else
  _msg="Setup lab \"\e[1;91m${lab_name:-inputFile}\e[0m\" (VMs only)" show_nicer_messages
fi


# ── Phase 1: cluster DNS entries ──────────────────────────────────────────────
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


# ── Phase 2: VM provisioning ──────────────────────────────────────────────────
_msg="Creating VMs" show_nicer_messages
((_lvl++))
for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
do
  load_vm_vars
  load_def
  _msg="Node: \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
  if [[ "${_keep}" == "1" ]] && vm_is_reusable
  then
    _msg="  Skipping \e[1;91m\"${_vm_name}\"\e[0m — existing VM matches definition" show_nicer_messages
  else
    ssh-keygen -f ~/.ssh/known_hosts -R "${_vm_name}"
    destroy_vm.sh "${inputFile}" "${_vm_name}"
    setup_vm.sh "${inputFile}" "${_vm_name}"
  fi
done
((_lvl--))


# ── Phase 3: Kubernetes cluster install ───────────────────────────────────────
if jq -e '.kclusters' < "${inputFile}" &>/dev/null
then

  # Reboot kept cluster nodes to apply initial config
  # (freshly installed nodes are already rebooted by setup_vm.sh)
  _msg="Rebooting kept cluster nodes" show_nicer_messages
  ((_lvl++))
  for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
  do
    load_vm_vars
    load_def
    [[ -z "${clu_name}" ]] && continue
    [[ "${_keep}" != "1" ]] && continue
    _msg="Restart node \e[1;91m${_vm_name}\e[0m (cluster \e[1;91m${clu_name}\e[0m)" show_nicer_messages
    reboot_vm
  done
  ((_lvl--))

  sleep 5
  _msg="Waiting for cluster nodes to come back online" show_nicer_messages
  ((_lvl++))
  for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
  do
    load_vm_vars
    load_def
    [[ -z "${clu_name}" ]] && continue
    [[ "${_keep}" != "1" ]] && continue
    check_ssh_conn
  done
  ((_lvl--))

  # Install Kubernetes — one cluster at a time
  list_kclusters | while read clu_name
  do
    load_kclu_vars
    _msg="Installing \"\e[1;91m${clu_type}\e[0m\" cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
    ((_lvl++))
    for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
    do
      [[ "$(get_vm_kcluster)" != "${clu_name}" ]] && continue
      load_vm_vars
      load_def
      setup_${clu_type} || _msg="setup_${clu_type} failed on \e[1;91m${_vm_name}\e[0m" fail_with_error
    done
    ((_lvl--))

    _msg="Wait $((2 + delay_min)) min for cluster \e[1;91m\"${clu_name}\"\e[0m to stabilise" show_nicer_messages
    sleep $((60 * (2 + delay_min)))

    # Install cluster-level addons
    installed_addons=""

    # Determine which node runs the addon installer
    if [[ "${mgm_node}" == "" ]]
    then
      for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
      do
        [[ "$(get_vm_kcluster)" == "${clu_name}" ]] && break
      done
    else
      _vm_name="${mgm_node}"
    fi

    _cluster_addons="$(jq -r ".kclusters[\"${clu_name}\"].addons // [] | .[]" < "${inputFile}" 2>/dev/null)"
    if [[ "${_cluster_addons}" != "" ]]
    then
      load_vm_vars
      load_def
      _msg="Installing cluster \e[1;91m\"${clu_name}\"\e[0m addon/s ( $(echo "${_cluster_addons}" | xargs) ) from \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
      ((_lvl++))
      for _addon in $(echo "${_cluster_addons}" | xargs)
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
          _msg="Installed addon \e[1;91m\"${_addon}\"\e[0m on cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
        fi
        _msg="No more addons for cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
      done
      ((_lvl--))
    else
      _msg="No Kubernetes cluster addons for \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
    fi
  done  # end per-cluster loop

fi


# ── Phase 4: VM-level addons ──────────────────────────────────────────────────
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
  fi
done

_msg="LAB setup completed" show_nicer_messages
