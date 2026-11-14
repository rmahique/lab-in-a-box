#!/bin/bash
# setup_lab_automation.sh with _automation_node=container: runs the real script with podman, systemctl, virsh, curl and
# the network commands replaced by stubs that log their arguments, then checks the image build, the unit, the network
# script, the container's persistent volumes, the inputs put into the node (settings, automation-node-lib.sh, the
# lab-in-a-box tree without .git or lab.cfg, .lab-versions) and that the node runs "configure" from them.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_repo=$PWD
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

_t=$(mktemp -d)
_bin="${_t}/bin" _log="${_t}/calls.log"
mkdir -p "${_bin}" "${_t}/lab" "${_t}/node"
export STUB_LOG="${_log}" STUB_DIR="${_t}"

# podman exec runs inside ${STUB_DIR}/node: "tar -C / ..." extracts there, "rm -rf PATH" removes there.
cat > "${_bin}/podman" <<'EOF'
#!/bin/bash
echo "podman $*" >> "${STUB_LOG}"
case "$1" in
    inspect) echo 4242 ;;
    exec)
        shift; [[ "$1" == -i ]] && shift; shift
        case "$1 $2 $3" in
            "tar -C /") shift 3; tar -C "${STUB_DIR}/node" "$@" ;;
            "rm -rf "*) rm -rf "${STUB_DIR}/node$3" ;;
            "cat /root/.ssh/id_rsa.pub ") echo "ssh-rsa CONTAINERKEY root@automation" ;;
        esac ;;
esac
exit 0
EOF
# git clone (setup tree outside git) makes a one-commit repository instead of downloading.
cat > "${_bin}/git" <<EOF
#!/bin/bash
if [[ "\$1" == clone ]]; then
    d=\${*: -1}; mkdir -p "\$d"; echo cloned > "\$d/CLONED"
    $(command -v git) -C "\$d" init -q && $(command -v git) -C "\$d" add CLONED \\
        && $(command -v git) -C "\$d" -c user.name=t -c user.email=t@t commit -qm c
    exit
fi
exec $(command -v git) "\$@"
EOF
cat > "${_bin}/ip" <<'EOF'
#!/bin/bash
echo "ip $*" >> "${STUB_LOG}"
[[ "$*" == "-4 -br addr show br0" ]] && echo "br0              UP             192.0.2.2/24 fe80::1/64"
exit 0
EOF
for _c in systemctl virsh nc tput ssh-keygen hostname curl; do
    printf '#!/bin/bash\necho "%s $*" >> "${STUB_LOG}"\n[[ "%s $*" == "virsh "*" desc "* ]] && exit 1\nexit 0\n' "${_c}" "${_c}" > "${_bin}/${_c}"
done
chmod 0755 "${_bin}"/*
mkdir -p /root/.ssh && : > /root/.ssh/id_rsa && echo "ssh-ed25519 HOSTKEY root@host" > /root/.ssh/id_rsa.pub

cat > "${_t}/lab/lab.cfg" <<'EOF'
_automation_node="container"
_network_mode="bridge"
_bridge_name="br0"
_bridge_nic=""
_myip="192.0.2.10"
_mynet="192.0.2.0/24"
_mygw="192.0.2.1"
_mydns="192.0.2.53"
_mynetrev="2.0.192"
_mydomain="mydemo.lab"
AUTOMATION_HOSTNAME="automation.mydemo.lab"
MYREG="registry.mydemo.lab"
ROOT_SSH_PUB_KEY="ssh-ed25519 OPERATORKEY op@laptop"
root_pwd="s3cret-pw"
_qemu_addr="qemu:///system"
_virt_srv="root@192.0.2.2"
EOF

# Runs setup_lab_automation.sh from the setup tree $1 (default: this repository).
run_setup() {
    : > "${_log}"
    (cd "${_t}/lab" && PATH="${_bin}:${PATH}" LAB_KUBECTL_INSTALL="echo kubectl" LAB_PYTHON=python3.11 \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
        bash "${1:-${_repo}}/setup_demo_server/setup_lab_automation.sh" > "${_t}/run.log" 2>&1)
    # configure_host_dns, the last step, edits this container's own /etc/resolv.conf, which podman keeps read-only,
    # so the exit status is not checked: every step must run and none may report an ERROR.
    if grep -q "ERROR" "${_t}/run.log" || ! grep -q "Reconfigure host to use new VM as DNS server" "${_t}/run.log"; then
        tail -30 "${_t}/run.log"; fail "setup_lab_automation.sh failed in container mode"
    fi
}

run_setup
_node="${_t}/node/etc/lab_creation/node"

grep -q "^podman build .*--build-arg KUBECTL_INSTALL=echo kubectl -t localhost/lab-automation-node:latest -f ${_repo}/setup_demo_server/automation-node.Containerfile" "${_log}" \
    || fail "the image is not built from automation-node.Containerfile with KUBECTL_INSTALL"
grep -q "^podman create --name automation.mydemo.lab --hostname automation.mydemo.lab --privileged --network none --systemd always -v .* localhost/lab-automation-node:latest" "${_log}" \
    || fail "the container is not created privileged, without a network, with systemd"
for _v in etc-lab-creation:/etc/lab_creation etc-lab-builder:/etc/lab-builder etc-lab-mcp:/etc/lab-mcp etc-ssh:/etc/ssh \
          named:/var/lib/named root:/root provisioning:/srv/www/htdocs/lab_creation helm:/srv/www/htdocs/helm; do
    grep "^podman create " "${_log}" | grep -q -- "-v lab-automation-${_v} " || fail "the container lacks the persistent volume lab-automation-${_v}"
done
grep -q "^systemctl enable --now lab-automation.service" "${_log}" || fail "lab-automation.service is not enabled"
grep -q "^virt-install\|^virsh .*undefine" "${_log}" && fail "container mode touched a VM"

_unit=/etc/systemd/system/lab-automation.service
grep -q "^ExecStart=/usr/bin/podman start automation.mydemo.lab$" "${_unit}" || fail "the unit does not start the container"
grep -q "^ExecStart=/usr/local/sbin/lab-automation-net automation.mydemo.lab br0 192.0.2.10/24 52:54:00:00:02:0a 192.0.2.1$" "${_unit}" \
    || fail "the unit does not connect the container to br0 with its IP, MAC and gateway: $(grep lab-automation-net "${_unit}")"
_net=/usr/local/sbin/lab-automation-net
bash -n "${_net}" || fail "lab-automation-net has a syntax error"
grep -q 'ip link set labauto0 master "\$2" up' "${_net}" || fail "lab-automation-net does not attach the veth to the bridge"
grep -q 'ip route add default via "\$5"' "${_net}" || fail "lab-automation-net does not set the default route"

# The inputs in the node.
[[ -x "${_node}/automation-node-lib.sh" ]] || fail "automation-node-lib.sh is not put into the node"
[[ "$(stat -c %a "${_node}/settings" 2>/dev/null)" == 600 ]] || fail "the node settings are missing or not mode 0600"
(
    # shellcheck source=/dev/null
    . "${_node}/settings"
    [[ "${_node_kind}" == container && "${_python_bin}" == python3.13 && "${_host_ip}" == 192.0.2.2 \
       && "${ROOT_PWD_HASH}" == '$6$'* && "${HOST_SSH_PUB_KEY}" == "ssh-ed25519 HOSTKEY root@host" ]]
) || fail "the node settings lack the node kind, Python, the hypervisor's address and keys, or the password hash"
grep -q "s3cret-pw" "${_node}/settings" && fail "the node settings contain the plaintext root password"
grep -q "s3cret-pw" "${_log}" && fail "the root password appears on a command line"
[[ -f "${_node}/lab-in-a-box/install_automation_node_scripts.sh" ]] || fail "the lab-in-a-box tree is not put into the node"
[[ -e "${_node}/lab-in-a-box/.git" ]] && fail "the lab-in-a-box tree in the node has .git"
find "${_node}/lab-in-a-box" -name lab.cfg | grep -q . && fail "lab.cfg was copied into the node"
grep -q " scripts/setup_lab.py$" "${_node}/lab-in-a-box/.lab-versions" && grep -q " webui$" "${_node}/lab-in-a-box/.lab-versions" \
    || fail ".lab-versions lacks file or directory versions"
[[ "$(awk '$2 == "scripts/setup_lab.py" {print $1}' "${_node}/lab-in-a-box/.lab-versions")" \
   == "$(git -c safe.directory='*' log -1 --format=%h -- scripts/setup_lab.py)" ]] || fail ".lab-versions does not match git log"
grep -q "^podman exec automation.mydemo.lab bash /etc/lab_creation/node/automation-node-lib.sh configure$" "${_log}" \
    || fail "the node is not configured with automation-node-lib.sh configure"
grep -q "^podman exec automation.mydemo.lab rm -rf /etc/lab_creation/node/lab-in-a-box$" "${_log}" \
    || fail "the old lab tree is not removed before the new one is copied in"
grep -q "CONTAINERKEY" /root/.ssh/authorized_keys || fail "the container's key is not authorized on the host"

# A second run, from a setup tree that is not a git checkout (setup_kvm_node.py TARGET copies only
# setup_demo_server/ and libs/): the host clones lab-in-a-box; nothing is removed or authorized twice.
_plain="${_t}/plain"
mkdir -p "${_plain}"
cp -a "${_repo}/setup_demo_server" "${_repo}/libs" "${_plain}/"
run_setup "${_plain}"
[[ -f "${_node}/lab-in-a-box/CLONED" && ! -f "${_node}/lab-in-a-box/install_automation_node_scripts.sh" ]] \
    || fail "a setup tree outside git does not put a clone of lab-in-a-box into the node"
grep -q "^podman volume rm\|^podman rm .*-v\|--volumes" "${_log}" && fail "re-running setup removed a persistent volume"
[[ "$(grep -c CONTAINERKEY /root/.ssh/authorized_keys)" == 1 ]] || fail "re-running setup authorized the container's key on the host twice"

# A container on the host's network (_automation_network=host): no bridge, no veth, no sshd; the host gets wrappers.
echo '_automation_network="host"' >> "${_t}/lab/lab.cfg"
echo "not a wrapper" > /usr/local/bin/check_addon.py
run_setup
grep -q "^podman create .* --network host --uts private -v /var/lib/lab-in-a-box/labs:/var/lib/lab-in-a-box/labs --systemd always " "${_log}" \
    || fail "a host-network container is not created on the host's network with its own hostname and the labs directory"
[[ -d /var/lib/lab-in-a-box/labs ]] || fail "the shared labs directory is not created"
grep -q "lab-automation-net" "${_unit}" && fail "a host-network container's unit still connects it to a bridge"
[[ -e /usr/local/sbin/lab-automation-net ]] && fail "lab-automation-net is left behind for a host-network container"
grep -q "^ExecStart=/usr/bin/podman start automation.mydemo.lab$" "${_unit}" && grep -q "^ExecStop=/usr/bin/podman stop" "${_unit}" \
    || fail "a host-network container's unit does not start and stop it"
grep -q "Using bridge" "${_t}/run.log" && fail "a host-network container still looks for a bridge"
(
    # shellcheck source=/dev/null
    . "${_node}/settings"
    [[ "${_node_network}" == host && "${_host_ip}" == 192.0.2.10 ]]
) || fail "the node settings do not tell a host-network container it is one, at the host's own address"
grep -q 'Device "" does not exist' "${_t}/run.log" && fail "a host-network container still asks for a bridge's address"
_w=/usr/local/bin/setup_lab.py
[[ -x "${_w}" ]] && grep -qx "# lab-in-a-box host wrapper" "${_w}" \
    && grep -q '^exec podman exec -i "${_tty\[@\]}" -w "${_wd}" automation.mydemo.lab setup_lab.py "\$@"$' "${_w}" \
    || fail "the host has no setup_lab.py wrapper running it in the container"
[[ "$(cat /usr/local/bin/check_addon.py)" == "not a wrapper" ]] || fail "a file that is not a wrapper was overwritten"
# The wrapper: a file outside the labs directory is refused; inside it, the command runs there in the container.
echo '{}' > "${_t}/outside.json"
PATH="${_bin}:${PATH}" bash "${_w}" "${_t}/outside.json" > "${_t}/w.out" 2>&1 && fail "a wrapper accepted a file outside the labs directory"
grep -q "outside /var/lib/lab-in-a-box/labs" "${_t}/w.out" || fail "a wrapper does not say why it refused a file"
mkdir -p /var/lib/lab-in-a-box/labs/x && echo '{}' > /var/lib/lab-in-a-box/labs/x/lab.json
: > "${_log}"
(cd /var/lib/lab-in-a-box/labs/x && PATH="${_bin}:${PATH}" bash "${_w}" lab.json --debug)
grep -q "^podman exec -i -w /var/lib/lab-in-a-box/labs/x automation.mydemo.lab setup_lab.py lab.json --debug$" "${_log}" \
    || fail "a wrapper does not run the command in the container, in the same directory: $(cat "${_log}")"
# Removing the container removes the wrappers, and only them.
( . "${_repo}/setup_demo_server/automation-node-lib.sh"
  _HOST_WRAPPED="setup_lab.py check_addon.py" _WRAPPER_MARK="# lab-in-a-box host wrapper" AUTOMATION_HOSTNAME=automation.mydemo.lab
  eval "$(sed -n '/^function remove_automation_container/,/^}/p' "${_repo}/setup_demo_server/setup_lab_automation.sh")"
  PATH="${_bin}:${PATH}" remove_automation_container )
[[ -e /usr/local/bin/setup_lab.py ]] && fail "removing the container leaves its wrappers"
[[ -e /usr/local/bin/check_addon.py ]] || fail "removing the container removed a file that is not a wrapper"

exit "${_fail}"
