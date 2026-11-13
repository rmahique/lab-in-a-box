#!/bin/bash
# Part of lab-in-a-box: prints the .lab-versions list of a lab-in-a-box git checkout.
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.
#
# Usage: write_lab_versions.sh SOURCE_TREE > DEST/.lab-versions
#
# Prints "<hash> <path>" for every tracked file of the git checkout SOURCE_TREE and every directory holding one:
# the last commit that changed it. A tree copied out of git (a staged automation node tree, an RPM/DEB package)
# carries this list so install_automation_node_scripts.sh's lab_version() can stamp versions without .git. It needs
# the checkout's full history: in a shallow clone every path gets the newest commit present.
set -euo pipefail

_src="${1:?usage: write_lab_versions.sh SOURCE_TREE}"
git -C "${_src}" rev-parse --is-inside-work-tree >/dev/null
cd "${_src}"
# One "git log -1" per path: a single --name-only walk cannot reproduce lab_version()'s value, because such a walk lists
# no paths for a merge commit and a merge that resolves a conflict is the last commit to change that file.
git ls-files | awk '{
      print
      n = split($0, part, "/"); dir = part[1]
      for (i = 1; i < n; i++) { if (i > 1) dir = dir "/" part[i]; print dir }
  }' | sort -u | while IFS= read -r _path; do
      printf '%s %s\n' "$(git log -1 --format=%h -- "${_path}")" "${_path}"
  done
