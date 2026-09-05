"""
Lab In A Box icon — rendered headless via Blender's Python API.

Concept: nested virtualization, made literal. A beveled glass cube (the
physical host / hypervisor "box") contains several smaller glowing cubes
(the VMs/labs running inside it), one of them brighter and warmer than the
rest (the automation VM orchestrating everything). Transparent background
so it drops cleanly onto light or dark surfaces (README, favicon, etc.).

Second pass (2026-08-31) over the original render: the first version used a
bare Emission shader for the glow cubes (no light response at all — flat
color with a blurred edge, ignoring every light and the AO pass), and had
neither bevels nor ambient occlusion nor depth of field, so nothing in frame
had a genuine specular highlight or contact shadow to read as 3D. This
version keeps the glass material's own recipe (Principled BSDF +
Transmission — a dedicated Glass BSDF node was tried and looked visually
correct in the node graph but rendered as a near-opaque dark cube; see
logo_common.glass_material's docstring) but adds, via logo_common.py: real
Principled-BSDF glow cubes (built-in Emission input, so they catch specular
light instead of ignoring it), bevels on every hard edge, ambient occlusion,
a dim ambient world for the glass to actually reflect something, and depth
of field focused on the hero cube — same composition, genuinely more
dimensional render.

Regenerate with (needs Mesa's software GL — this was built on a headless
VM with no GPU):

    zypper install -y blender xvfb-run xorg-x11-server-Xvfb   # if missing
    cd media
    LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe MESA_GL_VERSION_OVERRIDE=3.3 \\
      xvfb-run -a --server-args="-screen 0 1600x1600x24" \\
      blender --background --python generate_logo.py

Set LOGO_DRAFT=1 in the environment for a fast low-sample/low-res preview
while iterating on composition — full quality omits it.
Output: media/logo.png (edit OUT_PATH below to change).
"""
import math
import os
import random
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logo_common import (  # noqa: E402
    AMBER, TEAL, GLASS_TINT, clean_scene, glow_material, glass_material,
    emission_material, add_bevel, setup_render, setup_ambient_world,
    three_point_lighting, aim_camera_at, enable_dof,
)

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
DRAFT = os.environ.get("LOGO_DRAFT") == "1"

random.seed(7)

clean_scene()
scene = bpy.context.scene
setup_render(scene, DRAFT, resolution_x=1600)
setup_ambient_world()

# ── Outer glass box (the physical host), plus a thin glowing wireframe cage
# on top of it for edge definition the glass alone won't reliably show at
# small (128px favicon) sizes ─────────────────────────────────────────────
bpy.ops.mesh.primitive_cube_add(size=2.6, location=(0, 0, 0))
outer = bpy.context.active_object
outer.name = "HostBox"
outer.data.materials.append(glass_material("GlassHost", GLASS_TINT))
add_bevel(outer, width=0.05, segments=4)

bpy.ops.mesh.primitive_cube_add(size=2.6, location=(0, 0, 0))
cage = bpy.context.active_object
cage.name = "HostCage"
wf = cage.modifiers.new("wf", type="WIREFRAME")
wf.thickness = 0.018
cage.data.materials.append(emission_material("CageGlow", GLASS_TINT, strength=3.0))

# ── Inner VM cubes — staggered, off-center, varied depth/size so the
# composition doesn't read as a symmetric cross when viewed head-on ──────
positions_sizes = [
    ((-0.85, -0.78, -0.78), 0.42),
    ((0.80, -0.30, -0.88), 0.32),
    ((-0.50, 0.85, -0.75), 0.36),
    ((0.85, 0.85, 0.60), 0.46),
    ((-0.90, 0.75, 0.80), 0.30),
]

vm_objects = []
for i, (pos, size) in enumerate(positions_sizes):
    bpy.ops.mesh.primitive_cube_add(size=size, location=pos)
    cube = bpy.context.active_object
    cube.rotation_euler = (
        random.uniform(-0.2, 0.2),
        random.uniform(-0.2, 0.2),
        random.uniform(0, math.pi / 2),
    )
    cube.name = f"VM_{i}"
    cube.data.materials.append(glow_material(f"VMGlow_{i}", TEAL, emit_strength=2.2))
    add_bevel(cube, width=size * 0.09, segments=2)
    vm_objects.append(cube)

# The automation VM: near-center hero position, clearly separated from the
# other cubes (which are now pushed out toward the corners), brighter/warmer
bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0.05, -0.05, 0.05))
automation = bpy.context.active_object
automation.name = "AutomationVM"
automation.rotation_euler = (0.1, 0.18, 0.4)
automation.data.materials.append(glow_material("AutomationGlow", AMBER, emit_strength=3.2, roughness=0.12))
add_bevel(automation, width=0.045, segments=3)
vm_objects.append(automation)

# ── Camera — an asymmetric 3/4 angle (NOT down the body diagonal, which
# makes a cube look like a symmetric bullseye of radiating edges), pulled
# back a bit further for breathing room around the box in frame ──────────
cam_location = (8.2, -3.4, 2.5)
bpy.ops.object.camera_add(location=cam_location)
camera = bpy.context.active_object
scene.camera = camera
aim_camera_at(camera)
camera.data.lens = 58

focus_dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(cam_location, automation.location)))
enable_dof(camera, focus_distance=focus_dist, fstop=3.5)

# ── Lighting ─────────────────────────────────────────────────────────────
three_point_lighting(
    key_pos=(5, -4, 6), fill_pos=(-5, 3, -2), rim_pos=(-2, -6, -3),
)

# ── Render ───────────────────────────────────────────────────────────────
scene.render.filepath = OUT_PATH
bpy.ops.render.render(write_still=True)
print("RENDERED:", OUT_PATH)
