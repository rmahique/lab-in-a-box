---
name: Bug report
about: Something in lab-in-a-box didn't work as expected
title: ""
labels: bug
---

**What happened**
A clear description of what went wrong.

**What you expected**
What you expected to happen instead.

**Steps to reproduce**
1. Lab JSON used (redact any real credentials/IPs you don't want public)
2. Command run (`setup_lab.py ...`, `install_<addon> ...`, etc.)
3. What broke

**Logs**
The command's own stdout/stderr, plus any relevant `journalctl`/service logs
from the automation VM or affected node.

**Environment**
- lab-in-a-box version/commit: `--version` output or `git log -1 --format=%h`
- Hypervisor OS + version:
- Automation VM OS + version:
- Target VM OS/image (if relevant):

**Additional context**
Anything else that might help — this is genuinely new ground for a bug, not
a bug in an intentionally-vulnerable demo add-on (`install_insecure_app`,
`install_struts_demo` are supposed to be vulnerable).
