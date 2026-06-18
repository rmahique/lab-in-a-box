#!/bin/bash
# Part of lab-in-a-box, this is a simple library that defines functions used by other shell scripts.
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


# set -x -v

# Print _msg with indentation based on _lvl depth
function show_nicer_messages() {
  local _indent=""
  local _i=0
  while (( _i < ${_lvl:-0} )); do _indent+="  "; ((_i++)); done
  echo -e "${_indent}${_msg}"
}

# Print _msg as an error and exit
function fail_with_error() {
  echo -e "\e[1;91mERROR:\e[0m ${_msg}" >&2
  exit 1
}



# Creates a VM image from a separate image and resizes it to the desired size.
function copy_vm_img() {
        _msg="- Copy the image for the new VM \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
	ssh root@${REMOTE_HOST} "cp ${ISO_LOC}/${ISO_IMAGE} ${VM_IMG_LOC}/${_vm_name}.qcow2" || _msg="Failed to copy image for vm  \"${_vm_name}\"" fail_with_error
        _msg="- Resize to ${VM_DSK}G" show_nicer_messages
        ssh root@${REMOTE_HOST} "qemu-img resize -f qcow2 ${VM_IMG_LOC}/${_vm_name}.qcow2 ${VM_DSK}G" || _msg="Failed to resize VM image \"${_vm_name}\" to \"${VM_DSK}G\"" fail_with_error
}


# Check when a host becomes available via ssh
function check_ssh_conn() {
  ((_lvl++))
  _msg="Waiting for \e[1;91m\"${_vm_name}\"\e[0m to come online" show_nicer_messages
  _count=0
  ((_lvl++))
  while true
  do
    ((_count+=1))
    sleep ${_retry_interval:-2}
    if nc -z -w 2 ${_vm_name} ${_tcp_port:-22} &>/dev/null
    then
      _msg="\e[1;91m\"${_vm_name}\"\e[0m is online" show_nicer_messages
      break
    elif [[ "$?" == "127" ]]
    then
      _msg="ERROR - Netcat(nc) not installed" fail_with_error
    fi
    if [[ $_count -gt ${_retry_limit:-100} ]]
    then
      _msg="retry limit ( ${_retry_limit:-100} ) exceeded waiting for \e[1;91m\"${_vm_name}\"\e[0m to boot." fail_with_error
    fi
  done
  ((_lvl--))
  ((_lvl--))
}


# Creates ignition and combustion files used to setup the VM
function prepare_ign_and_cmb() {
        _msg="- Create ignition and combustion files for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
	cp ${LAB_SETUP_PATH}/combustion/{template,$_vm_name}
	cp ${LAB_SETUP_PATH}/ignition/{template,$_vm_name.ign}
	sed "s/TEMPLATE_HN/$_vm_name/g;s#ROOT_PWD_HASH#${ROOT_PWD_HASH}#g;s#ROOT_SSH_KEY#$(cat /root/.ssh/id_rsa.pub)#g" -i ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign

	sed "/#local vars/a mysource=${mysource}\nsourcepath=${sourcepath}\nmydns=${mydns}\nmyip=${myip}\nmymask=${mymask}\nmygw=${mygw}\nSUSE_email=${SUSE_email}\nSUSE_regcode=${SUSE_regcode}\nSUSE_url=${SUSE_url}" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
        sed "s#ROOT_SSH_KEY#$ROOT_SSH_KEY#g" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
}


# Creates cloud-init files used to setup the VM
function prepare_cloud-init() {
        ((_lvl++))
        _msg="- Create cloud-init files for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        ROOT_SSH_KEY=$(cat /root/.ssh/id_rsa.pub)
        ((_lvl++))
        for _type in user-data network-config meta-data
	do
          template_file=${LAB_SETUP_PATH}/cloud-init/template_${_type}
          process_templates >${LAB_SETUP_PATH}/cloud-init/${_vm_name}_${_type}
        done
        ((_lvl--))
        ((_lvl--))
}



# check which OS are we in and populate information
function find_OS() {
  # check which OS are we in
  if [[ -f /etc/os-release ]]
  then
    _os="`cat /etc/os-release | sed -n -e 's/^ID="\([-a-zA-Z].*\)"/\1/p'`"
    _version_id="`cat /etc/os-release | sed -n -e 's/^VERSION_ID="\([-a-zA-Z].*\)"/\1/p'`"
    _arch="`arch`"
  fi
}

# Install packages
###################################################################################################
###################################################################################################
###################################################################################################
###################################################################################################
###################################################################################################
###################################################################################################
function install_packages() {

#               eval "cat <<EOF
#$(cat ${template_file} )
#EOF
#"
       _msg="Installing standard packages" show_nicer_messages
          if [[ "${_os}" == "sles" ]]
          then
            BOOT_PACKAGES="vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
          elif [[ "${_os}" == "sle-micro" ]]
          then
            BOOT_PACKAGES="vim-small iptables NetworkManager-cloud-setup wget git"
          elif [[ "${_os}" == "opensuse-leap" ]]
          then
            BOOT_PACKAGES="vim-small apparmor-parser iptables NetworkManager-cloud-setup wget git"
          else
            _msg="ERROR - OS not supported yet" fail_with_error
          fi
}


# Copy the lab materials needed for the install to the hypervisor
function copy_to_hypervisor() {
        _msg="- Copy accross the lab setup materials" show_nicer_messages
	ssh root@${REMOTE_HOST} "[[ -d ${LAB_SETUP_PATH}/ ]] || mkdir -p ${LAB_SETUP_PATH}/" || _msg="failed creating new folder ${LAB_SETUP_PATH}" fail_with_error

        if [[ "$config_method" == "" ]]
        then
          ssh -q root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/{combustion,ignition}" || _msg="" fail_with_error
          rsync -aqv ${LAB_SETUP_PATH}/combustion/${_vm_name} root@${REMOTE_HOST}:${LAB_SETUP_PATH}/combustion/ || _msg="" fail_with_error
          rsync -aqv ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign root@${REMOTE_HOST}:${LAB_SETUP_PATH}/ignition/ || _msg="" fail_with_error
          ssh  -q root@${REMOTE_HOST} "chmod 0644 ${LAB_SETUP_PATH}/ignition/* ${LAB_SETUP_PATH}/combustion/*" || _msg="" fail_with_error
        else
          ssh -q root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/${config_method}" || _msg="" fail_with_error
          rsync -aqv ${LAB_SETUP_PATH}/${config_method}/${_vm_name}* root@${REMOTE_HOST}:${LAB_SETUP_PATH}/${config_method}/ || _msg="" fail_with_error
          ssh  -o StrictHostKeyChecking=accept-new root@${REMOTE_HOST} "cd ${LAB_SETUP_PATH}/${config_method}/; for i in ${_vm_name}*; do echo cp \${i} /tmp/\${i/${_vm_name}_/}; cp \${i} /tmp/\${i/${_vm_name}_/}; done ; rm -f ${VM_IMG_LOC}/${_vm_name}_ci.iso  ;mkisofs -J -l -R -V "cidata" -iso-level 4 -o ${VM_IMG_LOC}/${_vm_name}_ci.iso /tmp/user-data /tmp/meta-data /tmp/network-config" || _msg="" fail_with_error
          
        fi

}

# Add hostname entry to the DNS server as well as the API DNS entry, TBI
function add_to_dns() {
        ((_lvl++))
        _msg="Add hostname DNS entry \e[1;91m\"${_vm_name}\"\e[0m \e[1;91m\"${myip}\"\e[0m" show_nicer_messages

        # this needs to be properly done, useful when working with different networks
        if [[ ! -f /var/lib/named/${mynet_reverse}.db ]]
        then
          touch /var/lib/named/${mynet_reverse}.db
          _msg="WARNING: Reverse network file not setup, /var/lib/named/${mynet_reverse}.db, please review and make sure it's correct" show_nicer_messages
        fi

        if [[ "${REMOTE_DNS_SERVERS}" != "" ]]
        then
          for _remote_dns_server in ${REMOTE_DNS_SERVERS}
          do
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "grep -qi \"'${_vm_name}.\" /var/lib/named/${mynet_reverse}.db || echo \"${myip//*.}      IN  PTR     ${_vm_name}.\" >>/var/lib/named/${mynet_reverse}.db"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "grep -qi \"^${_vm_name//.*} \" /var/lib/named/${mydomain}.lan || echo \"${_vm_name//.*}         IN  A       ${myip}\" >>/var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} systemctl restart named
          done
        fi
        grep -qi "'${_vm_name}." /var/lib/named/${mynet_reverse}.db || echo "${myip//*.}      IN  PTR     ${_vm_name}." >>/var/lib/named/${mynet_reverse}.db
        grep -qi "^${_vm_name//.*} " /var/lib/named/${mydomain}.lan || echo "${_vm_name//.*}         IN  A       ${myip}" >>/var/lib/named/${mydomain}.lan
        systemctl restart named
        ((_lvl--))
}


# function to add a service DNS giving preference to agent nodes.
function add_service_dns() {
        ((_lvl++))
        _count=0
        ((_lvl++))
        for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
        do
                clu_type_u=` echo ${clu_type} | tr '[:lower:]' '[:upper:]'`
                if [[ $(jq -r ".nodes[\"${_dns}\"][\"INSTALL_${clu_type_u}_TYPE\"]" < ${inputFile} ) == "agent" ]] && [[ $(jq -r ".nodes.\"${_dns}\".kcluster" < "${inputFile}") == "${clu_name}" ]]
                then
                        add_dns_to_named_rr
                        _count=1
                fi
        done


        ((_lvl--))
        if [[ "${_count}" == "0" ]]
        then
        	for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
	        do
                    if [[ $(jq -r ".nodes.\"${_dns}\".kcluster" < "${inputFile}") == "${clu_name}" ]]
                    then
			add_dns_to_named_rr
                    fi
		done
		_msg="DNS ${_dns_entry} added to point to all nodes of the cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
	else
		_msg="DNS ${_dns_entry} added to point to agent nodes of the cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
        fi
        systemctl restart named
        ((_lvl--))
}


# Adds a DNS to Bind for round-robing balancing.
function add_dns_to_named_rr() {
        ((_lvl++))
        _msg="- add DNS entry \e[1;91m\"${_dns_entry}.${mydomain}\"\e[0m" show_nicer_messages
	_myip=$(jq -r ".nodes[\"${_dns}\"][\"myip\"]" < ${inputFile} )

        if [[ "${REMOTE_DNS_SERVERS}" != "" ]] 
        then
          for _remote_dns_server in ${REMOTE_DNS_SERVERS}
          do
            echo "${_remote_dns_server} /${_dns_entry}\tIN A  ${_myip}/d"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} -- "sed \"/${_dns_entry}\tIN A  ${_myip}/d\" -i /var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} -- "echo -e \"${_dns_entry}\tIN A  ${_myip}\" >> /var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} -- systemctl restart named
          done
        fi
	sed "/${_dns_entry}\tIN A  ${_myip}/d" -i /var/lib/named/${mydomain}.lan
	echo -e "${_dns_entry}\tIN A  ${_myip}" >> /var/lib/named/${mydomain}.lan
        ((_lvl--))
}

# Deletes a DNS entry from Bind
function del_from_dns() {
        _msg="- Delete DNS entries for \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        if [[ "${REMOTE_DNS_SERVERS}" != "" ]]        
        then
          for _remote_dns_server in ${REMOTE_DNS_SERVERS}
          do
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "sed \"/${myip//*.}      IN  PTR     ${_vm_name}./d\" -i /var/lib/named/${mynet_reverse}.db"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} "sed \"/${_vm_name//.*}         IN  A       ${myip}/d\" -i /var/lib/named/${mydomain}.lan"
            ssh -o StrictHostKeyChecking=accept-new -q root@${_remote_dns_server} systemctl restart named
          done
        fi


	sed "/${myip//*.}      IN  PTR     ${_vm_name}./d" -i /var/lib/named/${mynet_reverse}.db
	sed "/${_vm_name//.*}         IN  A       ${myip}/d" -i /var/lib/named/${mydomain}.lan
	systemctl restart named
        ((_lvl--))
}


# Creates a VM on a KVM hypervisor
function create_vm() {
        ((_lvl++))
        _msg="Create virtual machine \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        ((_lvl++))
        if [[ "${extra_fs}" != "" ]]
        then
          for _fs in ${extra_fs}
          do
            _filesystems="${_filesystems} --filesystem ${fs}"
          done
        else
          _msg="${_vm_name} has no extra volumes" show_nicer_messages
        fi
        if [[ "${extra_dsk}" != "" ]]
        then    
          for _dsk in ${extra_dsk}
          do
            if [[ "${_dsk}" =~ "UUID" ]]
            then
              _dsk="${_dsk//,*}"
              _dsk=`$ssh_command "lsblk -o UUID,PATH | grep ${_dsk//UUID=} | cut -d' ' -f2"`
            fi
            _disks="${_disks} --disk path=${_dsk//,*}"
          done
        else
          _msg="\e[1;91m\"${_vm_name}\"\e[0m has no extra disks" show_nicer_messages
        fi
        _msg="_disks:  ${_disks}" show_nicer_messages
        ((_lvl--))

        # If not config method specified we use ignition+combustion files
        if [[ "$config_method" == "" ]]
        then
          virt-install --connect ${VIRT_SRV} \
	       --name  ${_vm_name} \
               --autostart \
               --boot ${VM_BOOT:-uefi} \
	       --vcpus ${VM_CPU}  \
	       --memory ${VM_MEM} \
	       --os-variant=${VM_OSVARIANT:-slem5.4} \
	       --import \
	       --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,boot.order=1 \
               --import ${_filesystems} ${_disks} \
	       --graphics spice,listen=0.0.0.0 \
	       --network "${NETWORK}" \
	       --noautoconsole \
	       --qemu-commandline="-fw_cfg name=opt/com.coreos/config,file=${LAB_SETUP_PATH}/ignition/${IGN_FILE} -fw_cfg name=opt/org.opensuse.combustion/script,file=${LAB_SETUP_PATH}/combustion/${COM_FILE}"
          [[ "$?" != "0" ]] && _msg="virt-install failed for \e[1;91m\"${_vm_name}\"\e[0m" fail_with_error
        elif [[ "$config_method" == "iso-cloud-init" ]]
        then
          if [[ "$vcluster" == "harvester" ]]
          then
            _boot_params="harvester.install.config_url=http://10.100.0.10/harvester/config-create.yaml"
          fi

        elif [[ "$config_method" == "cloud-init" ]]
        then
          virt-install  --connect ${VIRT_SRV} \
               --name  ${_vm_name} \
               --import \
               --autostart \
               --boot ${VM_BOOT:-uefi} \
               --vcpus ${VM_CPU}  \
               --memory ${VM_MEM} \
               --os-variant=${VM_OSVARIANT:-slem5.4} \
               --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,boot.order=1 \
               --import ${_filesystems} ${_disks} \
               --graphics spice,listen=0.0.0.0 \
               --network "${NETWORK}" \
               --noautoconsole \
               --disk ${VM_IMG_LOC}/${_vm_name}_ci.iso,device=cdrom
          [[ "$?" != "0" ]] && _msg="virt-install for cloud-init failed for \e[1;91m\"${_vm_name}\"\e[0m" fail_with_error
          _msg="  - Waiting 3 minutes" show_nicer_messages
          sleep 180

          if [[ "$salt_states" != "" ]]
          then
            setup_salt
            _msg="  - applying salt states" show_nicer_messages
            for _salt_state in ${salt_states}
            do
              salt-ssh -i -v --update-roster  ${_vm_name} state.apply ${_salt_state}
            done
          fi

         _msg="  - eject media" show_nicer_messages
          virsh --connect ${VIRT_SRV} change-media ${_vm_name} --eject ${VM_IMG_LOC}${_vm_name}_ci.iso

         _msg="- reboot node" show_nicer_messages
          virsh --connect ${VIRT_SRV} reboot ${_vm_name}
        fi
        ((_lvl--))
}

# Deletes a VM from a KVM hypervisor
function delete_vm() {
        ((_lvl++))
        _msg="- Delete VM \e[1;91m\"${_vm_name}\"\e[0m" show_nicer_messages
        virsh -c ${VIRT_SRV} undefine --nvram "${_vm_name}"
	virsh -c ${VIRT_SRV} destroy  "${_vm_name}" 2>/dev/null
	virsh -c ${VIRT_SRV} undefine "${_vm_name}" --nvram --remove-all-storage
        ((_lvl--))
}

# Removes the VM ssh key from the known hosts to avoid warnings.
function clean_ssh_keys() {
	# Cleaup SSH keys
	ssh-keygen -f "${HOME}/.ssh/known_hosts" -R "${myip}"
}


# creates a config directory for operating kubernetes, TBI
function prepare_local_as_kubeclient() {
	# setup as client
	[ -d ~/.kube ] || mkdir -p ~/.kube
}





# Load VM variables
function load_vm_vars() {
        # grab the local device
        _default_dev="`nmcli -t -f GENERAL.DEVICE device show |grep -m 1 -v ' lo \| br-.*\| docker[0-9]* '`"
        _default_dev=${_default_dev//*:}
        for _key in $(jq -r '.common | to_entries[].key ' < ${inputFile} )
        do
              export ${_key}="$(jq -r .common[\"${_key}\"] < ${inputFile} )"
        done
        for _key in $(jq -r ".nodes[\"${_vm_name}\"] | to_entries[].key" < ${inputFile} )
        do
              # if kcluster is defined it means it's part of a kubernetes cluster
              if [[ "${_key}" == "kcluster" ]]
              then
                export clu_name=$(get_vm_kcluster)
              else
                export ${_key}="$(jq -r .nodes[\"${_vm_name}\"][\"${_key}\"] < ${inputFile} )"
              fi
        done
        # make live easier with some autogenerated defauls
        # many assumptions.
        if [[ "${mydns}" == "" ]]
        then
           mydns=`nmcli -t -f IP4.DNS device show ${_default_dev} |egrep -m 1 -o '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'`
        fi
        if [[ "${mygw}" == "" ]]
        then
           mygw=`ip route list to default |egrep -m 1 -o '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}'`
	fi
	if [[ "${mynet_reverse}" == "" ]]
        then
           mynet_reverse=`echo "${myip}" |sed 's/\([0-9]*\)\.\([0-9]*\)\.\([0-9]*\)\.\([0-9]*\)/\3.\2.\1/'`
        fi
        if [[ "${mymask}" == "" ]]
        then
          mymask=$(ipcalc -p "`ip -o -f inet addr show ${_default_dev} | egrep -o '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\/[0-9]{1,2}'`"|cut -d= -f2)
        fi
        if [[ "${mydomain}" == "" ]]
        then
          mydomain="$(hostname | sed "s/`hostname -s`\.//")"
        fi
}



# Setup SALT
function setup_salt() {
   [ -d ${HOME}/salt-ssh/states ] || mkdir -p ${HOME}/salt-ssh/states
   cat >${HOME}/salt-ssh/roster <<-EOF
managed:
  host: ${_vm_name}
  user: root
  sudo: False
  priv: ${HOME}/.ssh/id_rsa
EOF

  for _state in ${salt_states}
  do
    template_file=${LAB_SETUP_PATH}/salt-ssh/${_state}
    process_templates >${HOME}/salt-ssh/states/${_state}
  done

}


# Setup Helm
function setup_helm() {
	# add helm
        _msg="Setup Helm on cluster \e[1;91m\"${clu_name}\"\e[0m" show_nicer_messages
        if [[ "$online" == "1" ]]
	then
	        $ssh_command "curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash" || _msg="$ssh_command \"curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash\"" fail_with_error
	else
		$ssh_command 'curl http://automation/helm/install_helm.sh | bash -' || _msg="$ssh_command 'curl http://automation/helm/install_helm.sh | bash -'" fail_with_error
	fi
}


function helm_repo_add() {
        _msg="Adding helm repository \"${_repo_name}\"" show_nicer_messages
	$ssh_command "helm repo add ${_repo_name} ${_repo_url}" || _msg="Failed adding a helm repo ${_repo_name} ${_repo_url}" fail_with_error
        $ssh_command "helm repo update" || _msg="Failed updating helm repos" fail_with_error
}



# Generic load Vars function

function _load_vars() {
	if jq -r ".${_section} | to_entries[].key " < ${inputFile} &>/dev/null
	then
		for _key in $(jq -r ".${_section} | to_entries[].key " < ${inputFile} )
	        do  
	            value=$(jq -r ".${_section}[\"${_key}\"]" < ${inputFile} )
	            export ${_key}="${value}"
	        done
	else
		_msg="No variables defined for ${_section}" show_nicer_messages
	fi
}


function kk() { 
  if jq -r ".${1} | to_entries[].key " < ${inputFile} &>/dev/null
  then
    for _key in $(jq -r ".${1} | to_entries[].key " < ${inputFile} )
    do
      keys="${keys} \"${_key}\""
    done
  else
    _msg="No variables defined for ${_section}" show_nicer_messages
  fi
}



function ko() {
  _counter=$2
  _itemlist=$1
  _mypath=''
#  echo "D: START  -_itemlist=\"$_itemlist\" ; _counter=$_counter"
  for i in $1
  do
#    echo "D: i=$i"
    if [[ "$i" == "any" ]]; then
#      echo "D: i is any"
      _itemlist=${_itemlist/ $i}
      keys=''
#      echo "D1: _mypath=${_mypath}"
      kk ${_mypath}
      for e in $keys
      do
#        echo "D: e=$e ; _itemlist=\"${_itemlist}\" ; $_itemlist/ any/$e=\"${_itemlist/ any/$e}\"; _counter=${_counter}"
        ko "${_itemlist} ${e}" ${2}
        _itemlist=${_itemlist/ $e}
        ((_counter-=1))
      done
      break
    else
      if [[ "${_counter}" -eq 1  ]]
      then
#        echo "D: _counter is 1 ## _mypath: ${_mypath} ; e: ${e}"
        value=$(jq -r ".${_mypath}[\"${i}\"]" < ${inputFile} )
        echo "${_mypath}=$value"
      else
#        echo "D: _counter is $_counter"
        _mypath="${_mypath}[\"${i}\"]"
        ((_counter-=1))
      fi
    fi
  done
}


function load_iter_vars() {
        # We load the common settings
        for _key in $(jq -r '.common | to_entries[].key ' < ${inputFile} )
        do
              export ${_key}="$(jq -r .common[\"${_key}\"] < ${inputFile} )"
        done
        # we interate according to the input
        # number of arguments passed $#
        
        for _i in $@
        do
           if [[ "${_i}" == "*" ]]
           then
             ddd
           else
             uuu
           fi
        done 
        for _key in $(jq -r ".kclusters[\"${clu_name}\"] | to_entries[].key" < ${inputFile} )
        do
              export ${_key}="$(jq -r .kclusters[\"${clu_name}\"][\"${_key}\"] < ${inputFile} )"
        done
}




# Load rancher related variables.
function load_rancher_vars() {
        _section="rancher"
        _load_vars
}


# Load Jenkins related variables.
function load_jenkins_vars() {
       _section="jenkins"
       _load_vars
}

# Load ArgoCD related variables.
function load_argocd_vars() {
       _section="argocd"
       _load_vars
}

 
  # Load Longhorn related variables.
function load_lh_vars() {
       _section="longhorn"
       _load_vars
}

# Load NeuVector related variables.
function load_nv_vars() {
       _section="neuvector"
       _load_vars
}




# Inspired from https://stackoverflow.com/questions/2914220/bash-templating-how-to-build-configuration-files-from-templates-with-bash#11050943
function process_templates() {
       eval "cat <<EOF
$(cat ${template_file} )
EOF
"

}

# Check if $_needle exists in the space-separated list $_haystack
function check_exists() {
    [[ " ${_haystack} " == *" ${_needle} "* ]]
}

. ${_lib_path}/k8s_functions.bash



