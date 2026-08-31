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
    jq -r ".nodes[\"${_vm_name}\"].kcluster // empty" < "${inputFile}"
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
                $ssh_command "systemctl enable ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service && systemctl start ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service" || _msg="Failed ssh running systemctl enable rke2.service" fail_with_error
                _msg="\t\t- Retrieve node-token" show_nicer_messages
                declare -A token[$clu_name]=`$ssh_command "cat /var/lib/rancher/${clu_type}/server/node-token"` || _msg="Failed ssh creating node-token file" fail_with_error
                RANCHER1_IP=${_vm_name}
        else
                _msg="\t\t- \e[1;91m\"${_vm_name}\"\e[0m is not the 1st node of cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
                $ssh_command "echo 'server: https://${RANCHER1_IP}:9345' >>/etc/rancher/${clu_type}/config.yaml" || _msg="Failed ssh creating config.yaml" fail_with_error
                $ssh_command "echo 'token: ${token[$clu_name]}' >>/etc/rancher/${clu_type}/config.yaml" || _msg="Failed ssh adding token line to config.yaml " fail_with_error
                $ssh_command "systemctl enable ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service && systemctl start ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service" || _msg="Failed ssh enabling and starting rke2.service" fail_with_error
        fi
        ((_lvl--))
}


# Run one or more setup functions on the first server node found in the lab definition.
# Arguments: functions to call in order once a server node is found.
# Usage: on_first_server setup_helm setup_myproduct_repo setup_myproduct
function on_first_server() {
        for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
        do
                load_vm_vars
                ssh_command="ssh  -o StrictHostKeyChecking=accept-new -q  root@${_vm_name}"
                if [[ "${INSTALL_RKE2_TYPE}" == "server" || "${INSTALL_RKE2_TYPE}" == "" ]]
                then
                        echo "# Using node: ${_vm_name}"
                        for _fn in "$@"; do ${_fn}; done
                        return 0
                fi
        done
        echo "ERROR: No server node found in ${inputFile}" >&2
        return 1
}


# Run a setup function on each node that has $_addon in its addons[] list.
# If $_vm_name is already set by the caller, run on that node directly.
# Usage: _addon=uyuni on_addon_nodes setup_uyuni
function on_addon_nodes() {
        _run_fn="${1}"
        _found=0
        if [[ -n "${_vm_name}" ]]
        then
                load_vm_vars
                ssh_command="ssh  -o StrictHostKeyChecking=accept-new  root@${_vm_name}"
                echo "# Using node: ${_vm_name}"
                ${_run_fn}
                return 0
        fi
        for _vm_name in $(jq -r '.nodes | to_entries[].key' < "${inputFile}" | xargs)
        do
                while IFS= read -r _node_addon
                do
                        if [[ "${_node_addon}" == "${_addon}" ]]
                        then
                                load_vm_vars
                                ssh_command="ssh  -o StrictHostKeyChecking=accept-new  root@${_vm_name}"
                                echo "# Using node: ${_vm_name}"
                                ${_run_fn}
                                _found=1
                        fi
                done < <(jq -r ".nodes.\"${_vm_name}\".addons[]" < "${inputFile}" 2>/dev/null)
        done
        if [[ "${_found}" == "0" ]]
        then
                echo "WARNING: No node with addon '${_addon}' found in ${inputFile}"
        fi
}


# ─── Traefik on RKE2 ───────────────────────────────────────────────────────────
# Switch the RKE2 bundled ingress from nginx to Traefik and expose extra TCP
# entrypoints, following the SUSE Multi-Linux Manager kubernetes guide:
# the 'ingress-controller: traefik' RKE2 config option plus a HelmChartConfig
# for the packaged rke2-traefik chart (which binds hostPorts 80/443 by default).
# Extra TCP entrypoints are passed as "name:port" arguments, e.g.:
#   setup_traefik_rke2 salt-publish:4505 salt-request:4506 reportdb-pgsql:5432
# Requires: RKE2 with the ingress-controller option (>= v1.30) and
#           $ssh_command pointing at a cluster server node.
function setup_traefik_rke2() {
        _msg="# Configuring RKE2 Traefik ingress (extra TCP ports: ${*:-none})" show_nicer_messages

        # Uninstall the upstream-chart Traefik left by older script versions;
        # it would hold hostPorts 80/443 and block rke2-traefik
        if $ssh_command "helm status traefik -n kube-system" &>/dev/null
        then
                _msg="  Removing upstream-chart Traefik install …" show_nicer_messages
                $ssh_command "helm uninstall traefik -n kube-system"
        fi

        # Write the HelmChartConfig with the extra entrypoints before enabling
        # Traefik, so it starts with the right ports on first deploy
        {
        cat <<'EOF'
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: rke2-traefik
  namespace: kube-system
spec:
  valuesContent: |-
    ingressClass:
      isDefaultClass: true
    ports:
EOF
        for _ep in "$@"
        do
                cat <<EOF
      ${_ep%%:*}:
        port: ${_ep##*:}
        exposedPort: ${_ep##*:}
        protocol: TCP
        hostPort: ${_ep##*:}
        expose:
          default: true
EOF
        done
        } | $ssh_command "cat > /var/lib/rancher/rke2/server/manifests/lab-traefik-config.yaml" \
                || _msg="Failed to write the rke2-traefik HelmChartConfig" fail_with_error

        # Replace nginx with traefik in the RKE2 config
        $ssh_command "
grep -q '^ingress-controller:' /etc/rancher/rke2/config.yaml 2>/dev/null || \
    echo 'ingress-controller: traefik' >> /etc/rancher/rke2/config.yaml
if grep -q 'disable:' /etc/rancher/rke2/config.yaml 2>/dev/null; then
    grep -q 'rke2-ingress-nginx' /etc/rancher/rke2/config.yaml || \
        sed -i '/^disable:/a\\  - rke2-ingress-nginx' /etc/rancher/rke2/config.yaml
else
    echo -e 'disable:\n  - rke2-ingress-nginx' >> /etc/rancher/rke2/config.yaml
fi"

        # The config change only takes effect on rke2-server restart
        if ! $ssh_command "kubectl get ds -n kube-system rke2-traefik" &>/dev/null || \
             $ssh_command "kubectl get ds -n kube-system rke2-ingress-nginx-controller" &>/dev/null
        then
                _msg="  Restarting rke2-server to deploy Traefik …" show_nicer_messages
                $ssh_command "rm -f /var/lib/rancher/rke2/server/manifests/rke2-ingress-nginx.yaml; systemctl restart rke2-server"
                for _i in {1..30}
                do
                        $ssh_command "kubectl get nodes" &>/dev/null && break
                        sleep 10
                done
        fi

        _msg="  Waiting for Traefik to be ready …" show_nicer_messages
        for _i in {1..30}
        do
                $ssh_command "kubectl get ds -n kube-system rke2-traefik" &>/dev/null && break
                sleep 10
        done
        $ssh_command "kubectl rollout status ds/rke2-traefik -n kube-system --timeout=300s" \
                || _msg="Traefik did not become ready — check: kubectl get pods -n kube-system" fail_with_error
}


# Create or update a username/password secret in a namespace.
# Usage: create_basic_auth_secret <namespace> <secret_name> <username> <password>
# Requires: $ssh_command pointing at a cluster server node.
function create_basic_auth_secret() {
        local _ns="$1" _name="$2" _user="$3" _pass="$4"
        $ssh_command "kubectl create secret generic ${_name} \
                -n ${_ns} \
                --from-literal=username='${_user}' \
                --from-literal=password='${_pass}' \
                --dry-run=client -o yaml | kubectl apply -f -"
}


# Raise the Longhorn storage-over-provisioning percentage so thin volumes with
# a nominal size larger than the physical disk can still be scheduled (lab use).
# Usage: set_longhorn_overprovisioning <percentage>
# Requires: $ssh_command pointing at a cluster server node.
function set_longhorn_overprovisioning() {
        _msg="# Setting Longhorn over-provisioning to ${1}%" show_nicer_messages
        $ssh_command "kubectl patch settings.longhorn.io storage-over-provisioning-percentage \
                -n longhorn-system --type=merge -p '{\"value\":\"${1}\"}'" || \
                _msg="  Could not adjust Longhorn over-provisioning (continuing)" show_nicer_messages
}


# ─── Traefik on K3s ────────────────────────────────────────────────────────────
# Expose extra TCP entrypoints on the Traefik ingress bundled with K3s via a
# HelmChartConfig, following the SUSE Multi-Linux Manager kubernetes guide.
# K3s deploys Traefik by default and its ServiceLB publishes the service ports
# on the node, so unlike RKE2 no ingress switch and no hostPorts are needed.
# Extra TCP entrypoints are passed as "name:port" arguments, e.g.:
#   setup_traefik_k3s salt-publish:4505 salt-request:4506 reportdb-pgsql:5432
# Requires: $ssh_command pointing at a cluster server node.
function setup_traefik_k3s() {
        _msg="# Configuring K3s Traefik ingress (extra TCP ports: ${*:-none})" show_nicer_messages

        {
        cat <<'EOF'
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    ports:
EOF
        for _ep in "$@"
        do
                cat <<EOF
      ${_ep%%:*}:
        port: ${_ep##*:}
        exposedPort: ${_ep##*:}
        protocol: TCP
        expose:
          default: true
EOF
        done
        } | $ssh_command "cat > /var/lib/rancher/k3s/server/manifests/lab-traefik-config.yaml" \
                || _msg="Failed to write the traefik HelmChartConfig" fail_with_error

        # The k3s helm controller re-runs the traefik chart job on its own;
        # wait until the service picks up the first extra port, then for the
        # deployment rollout
        if [[ -n "${1}" ]]
        then
                for _i in {1..30}
                do
                        $ssh_command "kubectl get svc traefik -n kube-system -o jsonpath='{.spec.ports[*].port}' 2>/dev/null" \
                                | grep -qw "${1##*:}" && break
                        sleep 10
                done
        fi
        $ssh_command "kubectl rollout status deploy/traefik -n kube-system --timeout=300s" \
                || _msg="Traefik did not become ready — check: kubectl get pods -n kube-system" fail_with_error
}


# Install the CloudNativePG operator (HA PostgreSQL) via its Helm chart.
# Usage: setup_cnpg_operator [<chart_version>]
# Requires: $ssh_command pointing at a cluster server node.
function setup_cnpg_operator() {
        _msg="# Installing the CloudNativePG operator" show_nicer_messages
        $ssh_command "helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null; helm repo update >/dev/null; \
                helm upgrade --install cnpg cnpg/cloudnative-pg \
                --namespace cnpg-system --create-namespace ${1:+--version ${1}}" \
                || _msg="helm install failed for the CloudNativePG operator" fail_with_error
        $ssh_command "kubectl rollout status deploy/cnpg-cloudnative-pg -n cnpg-system --timeout=300s" \
                || _msg="the CloudNativePG operator did not become ready" fail_with_error
}
