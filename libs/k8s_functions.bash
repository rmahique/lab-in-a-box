#!/bin/bash
# Part of lab-in-a-box, Kubernetes helper functions
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


# List all kcluster names defined in the JSON
function list_kclusters() {
    jq -r '.kclusters | to_entries[].key' < "${inputFile}"
}

# Return the kcluster name for the current $_vm_name
function get_vm_kcluster() {
    jq -r ".nodes[\"${_vm_name}\"].kcluster" < "${inputFile}"
}

# Load kcluster variables for the current $clu_name into the environment
function load_kclu_vars() {
    [[ -z "${clu_name}" ]] && return 1
    if ! jq -e ".kclusters[\"${clu_name}\"]" < "${inputFile}" &>/dev/null; then
        _msg="kcluster \e[1;91m\"${clu_name}\"\e[0m not found in ${inputFile}" fail_with_error
    fi
    while IFS=$'\t' read -r _key _val
    do
        export ${_key}="${_val}"
    done < <(jq -r ".kclusters[\"${clu_name}\"] | to_entries[] | select(.value | scalars) | [.key, .value] | @tsv" < "${inputFile}")
}

# Add the cluster API/service DNS entry for the current $clu_name
function add_kclu_dns() {
    _dns_entry="${clu_name}"
    add_service_dns
}

# Setup K3s
function setup_k3s() {
        ((_lvl++))
        _msg="- Installing K3s on \e[1;91m\"${_vm_name}\"\e[0m for cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages

        prepare_local_as_kubeclient

        $ssh_command "mkdir -p /etc/rancher/k3s" || _msg="Failed ssh creating /etc/rancher/k3s" fail_with_error
        _msg="\t- Running K3s installation script" show_nicer_messages
        if [[ "${token[$clu_name]}" == "" ]]
        then
                _msg="\t\t- This is the 1st node ( \e[1;91m\"${_vm_name}\"\e[0m ) of cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
                $ssh_command "curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=${clu_rel:-stable} sh -s - server --tls-san ${clu_name}.${mydomain}" || _msg="Failed to install K3s server on \"${_vm_name}\"" fail_with_error
                _msg="\t\t- Retrieve node-token" show_nicer_messages
                declare -A token[$clu_name]=`$ssh_command "cat /var/lib/rancher/k3s/server/node-token"` || _msg="Failed to retrieve K3s node-token" fail_with_error
                RANCHER1_IP=${_vm_name}
        else
                _msg="\t\t- \e[1;91m\"${_vm_name}\"\e[0m is not the 1st node of cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
                $ssh_command "curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=${clu_rel:-stable} K3S_URL=https://${RANCHER1_IP}:6443 K3S_TOKEN=${token[$clu_name]} sh -" || _msg="Failed to install K3s agent on \"${_vm_name}\"" fail_with_error
        fi
        ((_lvl--))
}

# Setup RKE2
function setup_rke2() {
        ((_lvl++))
        _msg="- Installing RKE2 on \e[1;91m\"${_vm_name}\"\e[0m for cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages

        prepare_local_as_kubeclient

        $ssh_command "echo 'W2tleWZpbGVdCnVubWFuYWdlZC1kZXZpY2VzPWludGVyZmFjZS1uYW1lOmNhbGkqO2ludGVyZmFjZS1uYW1lOmZsYW5uZWwq' | base64 -d > /etc/NetworkManager/conf.d/rke2-canal.conf ; chmod 0420 /etc/NetworkManager/conf.d/rke2-canal.conf" || _msg="Failed ssh creating rke2-canal.conf file: \"$ssh_command\"" fail_with_error
        $ssh_command "echo 'bmV0LmlwdjQuY29uZi5hbGwuZm9yd2FyZGluZz0xCm5ldC5pcHY2LmNvbmYuYWxsLmZvcndhcmRpbmc9MQ==' | base64 -d > /etc/sysctl.d/90-rke2.conf ; chmod 0420 /etc/sysctl.d/90-rke2.conf" || _msg="" fail_with_error
        $ssh_command "echo 'ZXhwb3J0IFBBVEg9JFBBVEg6L29wdC9ya2UyL2JpbjovdmFyL2xpYi9yYW5jaGVyL3JrZTIvYmluLwpleHBvcnQgS1VCRUNPTkZJRz0vZXRjL3JhbmNoZXIvcmtlMi9ya2UyLnlhbWwKCg==' | base64 -d > /etc/profile.d/rke2.sh  ; chmod 0420 /etc/profile.d/rke2.sh" || _msg="Failed ssh creating rke2.sh" fail_with_error
        $ssh_command "mkdir -p /var/lib/rancher/${clu_type} /etc/rancher/${clu_type}" || _msg="Failed ssh creating folders" fail_with_error
        _msg="\t- Running RKE2 installation script" show_nicer_messages
        $ssh_command "curl -sfL https://get.${clu_type}.io | INSTALL_RKE2_TYPE=${INSTALL_RKE2_TYPE:-server}  INSTALL_RKE2_METHOD=${INSTALL_RKE2_METHOD} INSTALL_RKE2_CHANNEL=${clu_rel:-stable} sh -"
        if [[ "$?" != "0" ]]
        then
          msg="Failed to reach https://get.${clu_type}.io or execut install script\ncommand: $ssh_command \"curl -sfL https://get.${clu_type}.io | INSTALL_RKE2_TYPE=${INSTALL_RKE2_TYPE:-server}  INSTALL_RKE2_METHOD=${INSTALL_RKE2_METHOD} INSTALL_RKE2_CHANNEL=${clu_rel:-stable} sh -\"" fail_with_error
        fi
        _msg="\t- Configuring the RKE2 node" show_nicer_messages
        [[ -d "${clu_name}" ]] || mkdir "${clu_name}" || _msg="mkdir \"${clu_name}\"" fail_with_error
        [[ -f "${clu_name}/config-server.yaml" ]] || cat <<EOF>"${clu_name}/config-server.yaml"
write-kubeconfig-mode: "0600"
tls-san:
  - "${mydomain}"
  - "${clu_name}.${mydomain}"
EOF
        [[ -f "${clu_name}/config-agent.yaml" ]] || cat <<EOF>"${clu_name}/config-agent.yaml"
write-kubeconfig-mode: "0600"
tls-san:
  - "${mydomain}"
  - "${clu_name}.${mydomain}"
EOF
        rsync -a ${clu_name}/config-${INSTALL_RKE2_TYPE:-server}.yaml root@${_vm_name}:/etc/rancher/${clu_type}/config.yaml || _msg="rsync -a ${clu_name}/config-${INSTALL_RKE2_TYPE:-server}.yaml root@${_vm_name}:/etc/rancher/${clu_type}/config.yaml" fail_with_error
        if [[ "${token[$clu_name]}" == "" ]]
        then
                _msg="\t\t- This is the 1st node ( \e[1;91m\"${_vm_name}\"\e[0m ) of cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
                _msg="\t\t- Enable and start ${clu_type}-server for cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
                $ssh_command "systemctl enable --now ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service" || _msg="Failed ssh running systemctl enable rke2.service" fail_with_error
                _msg="\t\t- Retrieve node-token" show_nicer_messages
                declare -A token[$clu_name]=`$ssh_command "cat /var/lib/rancher/${clu_type}/server/node-token"` || _msg="Failed ssh creating node-token file" fail_with_error
                RANCHER1_IP=${_vm_name}
        else
                _msg="\t\t- \e[1;91m\"${_vm_name}\"\e[0m is not the 1st node of cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
                $ssh_command "echo 'server: https://${RANCHER1_IP}:9345' >>/etc/rancher/${clu_type}/config.yaml" || _msg="Failed ssh creating config.yaml" fail_with_error
                $ssh_command "echo 'token: ${token[$clu_name]}' >>/etc/rancher/${clu_type}/config.yaml" || _msg="Failed ssh adding token line to config.yaml " fail_with_error
                $ssh_command "systemctl enable --now ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service" || _msg="Failed ssh enabling and starting rke2.service" fail_with_error
        fi
        ((_lvl--))
}
