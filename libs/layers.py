#!/usr/bin/env python3
# Part of lab-in-a-box — addon installation-layer model.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/layers.py — how an addon can be installed, as a dimension separate from
where it's placed (see libs/targets.py for that — placement/topology: which
lab-JSON entity an addon may attach to).

An addon's install_<name> script may declare, via its PLUGIN dict's
"layers" list (see libs/apps.py), one or more of:

    LAYER_OS_NATIVE            installed directly on the host OS (a package
                                or binary, no container involved)
    LAYER_STANDALONE_CONTAINER a container running directly on a host
                                (podman/docker), NOT orchestrated by
                                Kubernetes — e.g. the PXE aux service in
                                services.py
    LAYER_KUBERNETES           deployed as a Kubernetes resource (kubectl/
                                helm) — this is what targets.py's
                                TARGET_CONTAINER already means today

This is purely descriptive/informational for now — it does not gate
validation the way targets.py's PLUGIN["targets"] does (see apps.py's
requirement_issue()). A future increment could add gating once every
addon's "layers" value has actually been reviewed rather than defaulted.
"""

LAYER_OS_NATIVE = "os-native"
LAYER_STANDALONE_CONTAINER = "standalone-container"
LAYER_KUBERNETES = "kubernetes"

ALL_LAYERS = (LAYER_OS_NATIVE, LAYER_STANDALONE_CONTAINER, LAYER_KUBERNETES)
