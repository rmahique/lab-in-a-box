"""
Lab In A Box wordmark — rendered headless via Blender's Python API, sharing
generate_logo.py's materials/lighting (see logo_common.py) so the icon and
the wordmark read as one visual identity rather than two unrelated assets.

Concept: "LAB IN A" in the same cool glass-teal as the icon's ordinary VM
cubes, "BOX" picked out in the same warm amber as the icon's automation VM —
the one hero element in each asset marks the same idea (the box itself /
the thing orchestrating what's inside it), not an arbitrary color split.
Extruded + beveled 3D text (not flat/vector type) to match the icon's own
dimensional, product-rendered feel — kept to a shallow extrude and a modest
camera tilt so it stays legible at small sizes instead of reading as a
gimmicky 3D-text render.

Font: IBM Plex Mono Bold (media/fonts/, SIL OFL — see media/fonts/OFL.txt).
Monospace was a deliberate pick, not a default: this is a CLI toolkit, and
the even letter-spacing plus technical/geometric feel of a mono face fits
that better than a humanist display face would.

Regenerate with (same headless Mesa requirement as generate_logo.py):

    cd media
    LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe MESA_GL_VERSION_OVERRIDE=3.3 \\
      xvfb-run -a --server-args="-screen 0 2400x560x24" \\
      blender --background --python generate_text_logo.py

Set LOGO_DRAFT=1 for a fast low-sample/low-res preview.
Output: media/logo-text.png (edit OUT_PATH below to change).
"""
import math
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logo_common import (  # noqa: E402
    AMBER, TEAL, clean_scene, glow_material, setup_render,
    setup_ambient_world, three_point_lighting, aim_camera_at, enable_dof,
)

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo-text.png")
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "IBMPlexMono-Bold.ttf")
DRAFT = os.environ.get("LOGO_DRAFT") == "1"

clean_scene()
scene = bpy.context.scene
setup_render(scene, DRAFT, resolution_x=2400, resolution_y=560, samples_final=224)
setup_ambient_world()

font = bpy.data.fonts.load(FONT_PATH)


def make_word(body, material, gap_before, x_cursor):
    """Add one text block, return (object, new_x_cursor) — align_x='LEFT' plus
    cumulative positioning is simpler and more predictable here than trying
    to pre-compute string widths from font metrics by hand."""
    bpy.ops.object.text_add(location=(x_cursor + gap_before, 0, 0))
    obj = bpy.context.active_object
    # A freshly-added text object lies flat in the XY plane (like text lying
    # on a table, facing world +Z) — the camera below looks along -Y toward
    # the origin, so without this rotation it sees the text edge-on (mostly
    # the thin extrusion depth, with letters reduced to sliver cross-sections)
    # rather than face-on.
    obj.rotation_euler = (math.radians(90), 0, 0)
    obj.data.font = font
    obj.data.body = body
    obj.data.align_x = "LEFT"
    obj.data.align_y = "CENTER"
    obj.data.size = 1.0
    obj.data.extrude = 0.055
    obj.data.bevel_depth = 0.012
    obj.data.bevel_resolution = 3
    obj.data.materials.append(material)
    bpy.context.view_layer.update()  # dimensions need current evaluated geometry
    return obj, x_cursor + gap_before + obj.dimensions.x


body_mat = glow_material("WordmarkBody", TEAL, emit_strength=2.2, roughness=0.25, metallic=0.2)
box_mat = glow_material("WordmarkBox", AMBER, emit_strength=3.4, roughness=0.15, metallic=0.2)

cursor = 0.0
lab_obj, cursor = make_word("LAB IN A", body_mat, gap_before=0.0, x_cursor=cursor)
# A trailing space in the first body string wouldn't reliably reserve width —
# Blender's text `dimensions` come from actual glyph curve geometry, and a
# space has none — so the gap between words is an explicit number instead.
box_obj, cursor = make_word("BOX", box_mat, gap_before=0.55, x_cursor=cursor)

total_width = cursor
# Recenter the whole wordmark on the origin (both text blocks used align_x
# ='LEFT' against a running cursor, so as-built they start at x=0 — shift
# everything left by half the total width instead of fighting per-object
# alignment math).
for obj in (lab_obj, box_obj):
    obj.location.x -= total_width / 2.0

# ── Camera — near-front for legibility, with a shallow tilt so it still
# reads as a 3D render rather than flat vector type. Distance is derived
# from the actual rendered width (not a hardcoded guess) — a wordmark's
# length changes with font/kerning, and a fixed lens+distance pair that
# happened to fit "LAB IN A BOX" would silently clip a longer relabel.
# lens=40mm (wide-ish) horizontal FOV in Blender's default 36mm-sensor
# convention: 2*atan(36/(2*40)) ≈ 47°; half_fov's tangent below is that
# angle's tan(), used directly rather than re-deriving it from `lens` so a
# future lens change doesn't silently desync the distance math from it.
lens_mm = 40
half_fov_tan = 0.435  # tan(23.5°) — matches lens_mm above; keep both in sync
margin = 1.35  # frame the text at ~74% of the available width, not edge-to-edge
cam_distance = (total_width / 2.0 * margin) / half_fov_tan
cam_location = (total_width * 0.03, -cam_distance, cam_distance * 0.09)
bpy.ops.object.camera_add(location=cam_location)
camera = bpy.context.active_object
scene.camera = camera
aim_camera_at(camera, target_location=(0, 0, 0))
camera.data.lens = lens_mm

focus_dist = math.sqrt(sum(c ** 2 for c in cam_location))
enable_dof(camera, focus_distance=focus_dist, fstop=5.6)

# ── Lighting — same rig style as the icon ─────────────────────────────────
three_point_lighting(
    key_pos=(3, -7, 4), fill_pos=(-4, -3, -1), rim_pos=(1, 4, -2),
)

# ── Render ───────────────────────────────────────────────────────────────
scene.render.filepath = OUT_PATH
bpy.ops.render.render(write_still=True)
print("RENDERED:", OUT_PATH)
