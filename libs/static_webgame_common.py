#!/usr/bin/env python3
# Part of lab-in-a-box — shared Kubernetes-manifest shape for self-hosting a real, pre-built
# static/HTML5 game, reused by install_supertux_classic, install_skynet_simulator, and
# install_open_saber.
# Author/s: Raul Mahiques
# License: GPLv3
"""
Each of these games is genuinely self-hosted — its own real, pre-built web export, fetched at
deploy time by an initContainer into a shared emptyDir, then served by a plain nginx container —
not a link out to itch.io. Only games whose license explicitly permits redistribution are wired up
this way (confirmed per-game, live, before writing that game's own addon — see each addon's own
header comment for its specific license confirmation).

deploy_podman() is the same shape on a single node: the game's own fetch script runs once in a throwaway container
into a host directory, and a long-running nginx container serves that directory on a host port.
"""

import json
import shlex

import placement
import podman_service as ps
from lab_creation import ssh_run

NGINX_IMAGE = "docker.io/library/nginx:stable"
DATA_ROOT = "/var/lib"
# How long a fresh nginx container gets to answer.
START_WAIT = 120

MANIFEST_TEMPLATE = """---
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {ns}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      initContainers:
      - name: fetch
        image: {init_image}
        command: {init_command_json}
        volumeMounts:
        - name: web
          mountPath: /shared
      containers:
      - name: nginx
        image: nginx:stable
        ports:
        - containerPort: 80
        volumeMounts:
        - name: web
          mountPath: /usr/share/nginx/html
      volumes:
      - name: web
        emptyDir: {{}}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {ns}
spec:
  ports:
    - port: 80
      name: web
  selector:
    app: {name}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  namespace: {ns}
spec:
  rules:
    - host: {host}
      http:
        paths:
          - backend:
              service:
                name: {name}
                port:
                  name: web
            path: /
            pathType: Prefix
"""


def render_static_webgame(name, ns, host, script, args=None, init_image="alpine:3"):
    """
    Render a Namespace+Deployment(initContainer fetch + nginx)+Service+Ingress manifest that
    self-hosts a real, pre-built static web game.

    name       : Kubernetes object name (also the app= label/selector value)
    ns         : namespace
    host       : ingress hostname
    script     : shell script (a single string, may contain newlines) run via `sh -c script` in
                 the initContainer to populate /shared with the game's own files (download +
                 extract) — game-specific, written in each addon's own script so its exact fetch
                 logic stays visible and reviewable there.
    args       : optional list of extra positional args (e.g. a download URL) passed after the
                 script — becomes $1, $2, ... inside it, exactly like `sh -c script _ arg1 arg2`.
                 Passed as a single, correctly-parsed argv list (no shell re-parsing / no
                 double-quoting risk — this is NOT a second layer of `sh -c`).
    init_image : image the initContainer runs in (default alpine:3 — small, has apk for
                 installing curl/unzip/tar on demand)
    """
    command = ["sh", "-c", script] + (["_"] + list(args) if args else [])
    return MANIFEST_TEMPLATE.format(
        name=name, ns=ns, host=host, init_image=init_image, init_command_json=json.dumps(command),
    )


def qualified(image: str) -> str:
    """`image` with Docker Hub's registry spelled out when it names none, so podman never asks which one is meant."""
    first = image.split("/")[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return image
    return "docker.io/{}{}".format("" if "/" in image else "library/", image)


def fetch_command(web_dir: str, script: str, args=None, init_image: str = "alpine:3") -> str:
    """The podman run that fills `web_dir` with the game's files, running `script` as render_static_webgame()'s
    initContainer does, with `web_dir` at /shared."""
    command = ["sh", "-c", script] + (["_"] + list(args) if args else [])
    return "podman run --rm -v {}:/shared:Z {} {}".format(
        shlex.quote(web_dir), shlex.quote(qualified(init_image)), " ".join(shlex.quote(c) for c in command))


def deploy_podman(hostname: str, name: str, script: str, port: int, args=None, init_image: str = "alpine:3") -> str:
    """
    Self-host the game `name` on `hostname` with podman: fetch its files into /var/lib/<name>/html with `script`
    (see render_static_webgame()), then serve them with nginx on `port`. Returns the URL it answers on.
    """
    web_dir = "{}/{}/html".format(DATA_ROOT, name)
    ssh_run(hostname, "mkdir -p {}".format(shlex.quote(web_dir)))
    ssh_run(hostname, fetch_command(web_dir, script, args, init_image))
    ps.deploy(hostname, name, NGINX_IMAGE, description="{} (lab-in-a-box)".format(name),
              ports=["{}:80".format(port)], volumes=["{}:/usr/share/nginx/html:ro,Z".format(web_dir)])
    ps.wait_for_http(hostname, "http://127.0.0.1:{}/".format(port), timeout=START_WAIT, what=name)
    return placement.node_url(hostname, port, "/")
