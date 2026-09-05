#!/bin/bash
# Part of lab-in-a-box, prepares the demo server scripts
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.





# check which OS are we in
[[ -f /etc/os-release ]] && source /etc/os-release && _os="${ID}"

# Fail if not os detected
if [[ "$_os" == "" ]]
then
   echo -e '\033[1;31mERROR\033[0m: OS type not detected'
   exit 1
elif [[ "$_os" == "opensuse-leap" ]]
then
  echo '- Installing in openSUSE Leap'
  _pkg_mgr="zypper "
elif [[ "$_os" == "sles" ]]
then
  echo '- Installing in SLES'
  _pkg_mgr="zypper "
else
  echo -e "\033[1;31mERROR\033[0m: Unsupported OS '${_os}'. This script supports opensuse-leap and sles."
  exit 1
fi

if ! type "git" &>/dev/null
then
  $_pkg_mgr install -y git || { echo -e '\033[1;31mERROR\033[0m: GIT command is not pressent and we couldn'\''t install it, please install it before proceed' ; exit 1 ; }
fi

# setup_kvm_node.py is pinned to python3.11 explicitly, same as the rest of
# this project's Python code — refuse to point the user at a script that
# can't run rather than failing later, less clearly.
if ! command -v python3.11 &>/dev/null
then
  echo -e '\033[1;31mERROR\033[0m: python3.11 is required (this project'\''s Python code is pinned to it) but was not found on PATH.'
  exit 1
fi

if [[ -d /var/tmp/setup_demo_server/.git ]]; then
    git -C /var/tmp/setup_demo_server pull --ff-only
else
    git clone https://github.com/SUSE-Technical-Marketing/lab-in-a-box.git /var/tmp/setup_demo_server
fi

if cd /var/tmp/setup_demo_server/setup_demo_server/
then
	chmod 0755 setup_kvm_node.py setup_lab_automation.sh

	if [[ ! -f lab.cfg ]]; then
	    [[ -f lab.cfg.template ]] || { echo -e '\033[1;31mERROR\033[0m: lab.cfg.template missing'; exit 1; }
	    cp lab.cfg.template lab.cfg
	fi
	echo -e '
\033[0;32;42m####################################################################\033[0m

\033[1;31mPlease edit the lab configuration file\033[0m:

\033[1;32mcd /var/tmp/setup_demo_server/setup_demo_server/ ; vi lab.cfg\033[0m

in this folder according to your settings.

Afterwards please run setup_kvm_node.py to configure your LAB KVM server:

\033[1;32m./setup_kvm_node.py <node_ip>\033[0m

\033[1;30m- node_ip\033[0m: is the IP of the server you want to use for your lab


You can also setup your current machine as the lab server by running the same command without any parameter:

\033[1;32m./setup_kvm_node.py \033[0m


\033[0;32;42m####################################################################\033[0m
'
else
	echo -e '\033[1;31mERROR\033[0m: Cloning the repository failed, this is the command used: \"\033[1;32mgit clone https://github.com/SUSE-Technical-Marketing/lab-in-a-box.git /var/tmp/setup_demo_server/\033[0m\"'
	exit 1
fi


