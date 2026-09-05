# Security Policy

## Scope

lab-in-a-box automates infrastructure that ends up with root SSH access to
every VM it creates, and manages secrets (root password hashes, SSH keys,
admin credentials for the software it installs). Vulnerabilities in the
project's **own** code — the orchestration scripts and libraries under
`scripts/`/`libs/`, the web UI, credential handling, or the generated
provisioning files — are in scope.

**Not in scope:** `install_insecure_app` and `install_struts_demo` deploy
*intentionally* vulnerable applications, on purpose, for security training
labs. Their vulnerabilities are the point — please don't report them.

## Reporting a vulnerability

Please **do not** open a public issue for a security vulnerability.

Instead, use GitHub's private reporting flow: go to the
[Security tab](../../security/advisories/new) of this repository and open a
new draft security advisory. This reaches the maintainer directly without
disclosing the issue publicly first.

Include, if possible:
- The affected script(s)/file(s) and version (`--version` output, or commit
  hash)
- Steps to reproduce, or a proof of concept
- The potential impact (what an attacker could do, and under what
  preconditions — e.g. does it require existing SSH access to a lab VM, or
  is it reachable from the automation VM's network-facing services)

## Response

This is a small, actively-maintained project without a formal SLA. Expect
an initial response within a few days. Once a fix is available, it will be
released and the advisory will be published with credit to the reporter
(unless you'd prefer to stay anonymous — say so in your report).
