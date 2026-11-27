#!/usr/bin/env python3
"""
libs/placement.py: where a lab runs an add-on that installs either on a kcluster or, with podman, on a node.

The lab places such an add-on by where it lists it. Listed in a kcluster's addons[], it is a Kubernetes workload on
that kcluster; listed in a node's own addons[], it is a podman container on that node. <addon>_deployment, when the
lab sets it, overrides that everywhere the add-on is listed.

deployment() answers for the run in progress, which is how an add-on picks its own mode. location() answers for
the lab as a whole, which is how another add-on finds where this one runs.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import os

import apps

PODMAN = "podman"
KUBERNETES = "kubernetes"
DEPLOYMENTS = (PODMAN, KUBERNETES)


def _listing(section: dict, addon: str) -> list:
    """The names of the entries of `section` (kclusters or nodes) whose own addons[] list `addon`, in lab order."""
    return [name for name, entry in (section or {}).items()
            if any(apps.addon_entry_name(e) == addon for e in (entry or {}).get("addons") or [])]


def kclusters_listing(definition: dict, addon: str) -> list:
    """The kclusters that list `addon` in their addons[]."""
    return _listing(definition.get("kclusters"), addon)


def nodes_listing(definition: dict, addon: str) -> list:
    """The nodes that list `addon` in their own addons[]."""
    return _listing(definition.get("nodes"), addon)


def asked(addon: str, cfg: dict) -> str:
    """<addon>_deployment as the lab wrote it, lower-cased; "" when the lab leaves it to the placement."""
    return str((cfg or {}).get("{}_deployment".format(addon)) or "").strip().lower()


def deployment(definition: dict, addon: str, cfg: dict) -> str:
    """
    The mode this run installs `addon` in: <addon>_deployment when the lab sets it, else "kubernetes" when
    setup_lab.py runs it for a kcluster (it sets $clu_name then) or, run by hand, when a kcluster lists it, else
    "podman".
    """
    mode = asked(addon, cfg)
    if mode:
        return mode
    if os.environ.get("clu_name") or kclusters_listing(definition, addon):
        return KUBERNETES
    return PODMAN


def location(definition: dict, addon: str) -> tuple:
    """
    Where the lab runs `addon`, as another add-on has to reach it: ("kubernetes", kcluster), ("podman", node), or
    ("", "") when the lab does not install it. A lab that lists it in both places is answered with the kcluster,
    unless <addon>_deployment says podman.
    """
    kclusters = kclusters_listing(definition, addon)
    nodes = nodes_listing(definition, addon)
    mode = asked(addon, definition.get(addon) or {})
    if mode == PODMAN or (not mode and not kclusters):
        return (PODMAN, nodes[0]) if nodes else ("", "")
    return (KUBERNETES, kclusters[0]) if kclusters else ("", "")


def validate(v, addon: str) -> None:
    """A preflight [ERROR] on `v` when <addon>_deployment names no mode this module knows."""
    mode = asked(addon, v.definition.get(addon) or {})
    if mode and mode not in DEPLOYMENTS:
        v.errors.append("[ERROR] {a}.{a}_deployment '{m}' is invalid: use one of: {o}".format(
            a=addon, m=mode, o=", ".join(DEPLOYMENTS)))


def node_url(node: str, port, path: str = "") -> str:
    """The URL a podman-mode service answers on: the node's own name, which the lab DNS already resolves."""
    return "http://{}:{}{}".format(node, port, path)
