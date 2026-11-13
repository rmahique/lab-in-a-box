#!/bin/bash
# Tests-paths: scripts/upgrade_lab_in_a_box
# scripts/upgrade_lab_in_a_box: the git path against a real local repository whose install_automation_node_scripts.sh
# only records what it deployed, and the package path with rpm, curl and zypper stubbed. Independent container — see
# tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_script="${PWD}/scripts/upgrade_lab_in_a_box"

_work="$(mktemp -d)"
trap 'rm -rf "${_work}"' EXIT
failed=0
check() {
    if ! eval "$2"; then
        echo "FAIL: $1"
        failed=$((failed + 1))
    fi
}

# ── a repository with main and dev, each deploy writing its branch and commit to ${_work}/deployed ──
_up="${_work}/upstream"
git init -q -b main "${_up}"
git -C "${_up}" config user.email t@t; git -C "${_up}" config user.name t
cat > "${_up}/install_automation_node_scripts.sh" <<EOF
#!/bin/bash
echo "\$(git rev-parse --abbrev-ref HEAD) \$(git rev-parse --short HEAD) \$(cat content)" > "${_work}/deployed"
EOF
echo "main-1" > "${_up}/content"
git -C "${_up}" add -A; git -C "${_up}" commit -qm "main 1"
git -C "${_up}" checkout -qb dev; echo "dev-1" > "${_up}/content"; git -C "${_up}" commit -qam "dev 1"
git -C "${_up}" checkout -q main
_repo="file://${_up}"
export LAB_UPGRADE_SRC="${_work}/src"

out=$("${_script}" --repo "${_repo}" 2>&1); rc=$?
check "git: a first run clones and deploys main by default" '[[ ${rc} -eq 0 && $(cat "${_work}/deployed") == "main "*" main-1" ]]'
check "git: it reports the commit it deployed" '[[ ${out} == *"upgraded to $(git -C "${_up}" rev-parse --short main) of main"* ]]'

echo "main-2" > "${_up}/content"; git -C "${_up}" commit -qam "main 2"
"${_script}" --repo "${_repo}" >/dev/null 2>&1
check "git: a later run deploys the branch's new commit" '[[ $(cat "${_work}/deployed") == *" main-2" ]]'

echo "stray" > "${_work}/src/content"
"${_script}" --repo "${_repo}" --branch dev >/dev/null 2>&1
check "git: --branch deploys that branch, local changes in the checkout discarded" \
    '[[ $(cat "${_work}/deployed") == "dev "*" dev-1" ]]'

_up2="${_work}/other"
git clone -q "${_up}" "${_up2}"
git -C "${_up2}" checkout -q main
git -C "${_up2}" config user.email t@t; git -C "${_up2}" config user.name t
echo "other-1" > "${_up2}/content"; git -C "${_up2}" commit -qam "other"
"${_script}" --repo "file://${_up2}" >/dev/null 2>&1
check "git: another --repo replaces the checkout" \
    '[[ $(cat "${_work}/deployed") == *" other-1" && $(git -C "${_work}/src" remote get-url origin) == "file://${_up2}" ]]'

"${_script}" --repo "${_repo}" --branch nope >/dev/null 2>&1; rc=$?
check "git: a branch that does not exist fails" '[[ ${rc} -ne 0 ]]'

"${_script}" --bogus >/dev/null 2>&1; rc=$?
check "an unknown option fails" '[[ ${rc} -ne 0 ]]'

# ── the package path: rpm says lab-in-a-box 1.0.0 is installed, the latest release is 1.1.0 ──
_bin="${_work}/bin"
mkdir -p "${_bin}"
cat > "${_bin}/rpm" <<'EOF'
#!/bin/bash
[[ "$*" == *"--qf"* ]] && { printf '%s' "${FAKE_RPM_VERSION}"; exit 0; }
exit 0
EOF
cat > "${_bin}/curl" <<'EOF'
#!/bin/bash
echo "$*" >> "${FAKE_LOG}"
if [[ "$*" == *"/releases/latest"* ]]; then
    echo '{"tag_name": "1.1.0", "assets": [
        {"name": "lab-in-a-box-1.1.0.noarch.rpm", "browser_download_url": "https://dl.example/lab-in-a-box-1.1.0.noarch.rpm"},
        {"name": "lab-in-a-box_1.1.0_all.deb", "browser_download_url": "https://dl.example/lab-in-a-box_1.1.0_all.deb"}]}'
    exit 0
fi
while (( $# )); do [[ $1 == -o ]] && { echo rpm > "$2"; }; shift; done
EOF
cat > "${_bin}/zypper" <<'EOF'
#!/bin/bash
echo "zypper $*" >> "${FAKE_LOG}"
EOF
chmod +x "${_bin}"/*
export FAKE_LOG="${_work}/pkg.log" LAB_UPGRADE_API="https://api.example"

: > "${FAKE_LOG}"
out=$(PATH="${_bin}:${PATH}" FAKE_RPM_VERSION=1.0.0 "${_script}" 2>&1); rc=$?
check "package: rpm install upgrades through zypper, not git" \
    '[[ ${rc} -eq 0 ]] && grep -q "zypper --non-interactive install --allow-unsigned-rpm .*/lab-in-a-box-1.1.0.noarch.rpm" "${FAKE_LOG}"'
check "package: the latest release of the official repository is asked for" \
    'grep -q "https://api.example/repos/SUSE-Technical-Marketing/lab-in-a-box/releases/latest" "${FAKE_LOG}"'
check "package: the .rpm asset is downloaded" 'grep -q -- "-o .*lab-in-a-box-1.1.0.noarch.rpm https://dl.example/lab-in-a-box-1.1.0.noarch.rpm" "${FAKE_LOG}"'

: > "${FAKE_LOG}"
out=$(PATH="${_bin}:${PATH}" FAKE_RPM_VERSION=1.1.0 "${_script}" 2>&1); rc=$?
check "package: already on the latest release installs nothing" \
    '[[ ${rc} -eq 0 && ${out} == *"already the latest release"* ]] && ! grep -q zypper "${FAKE_LOG}"'

: > "${FAKE_LOG}"
out=$(PATH="${_bin}:${PATH}" FAKE_RPM_VERSION=1.0.0 "${_script}" --repo https://github.com/someone/fork.git --branch dev 2>&1)
check "package: --repo picks that GitHub repository's releases" \
    'grep -q "https://api.example/repos/someone/fork/releases/latest" "${FAKE_LOG}"'
check "package: --branch is reported as not applying" '[[ ${out} == *"--branch does not apply"* ]]'

out=$(PATH="${_bin}:${PATH}" FAKE_RPM_VERSION=1.0.0 "${_script}" --repo "${_repo}" 2>&1); rc=$?
check "package: a repository that is not on GitHub fails" '[[ ${rc} -ne 0 && ${out} == *"GitHub repository URL"* ]]'

if (( failed )); then
    echo "${failed} upgrade_lab_in_a_box check(s) failed"
    exit 1
fi
echo "all upgrade_lab_in_a_box checks passed"
