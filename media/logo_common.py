"""
Shared Blender helpers for lab-in-a-box's media/generate_*.py scripts — the
material/lighting/render building blocks both the icon (generate_logo.py) and
the wordmark (generate_text_logo.py) use, so the two assets share one visual
language instead of drifting apart. Pinned to what actually exists in the
Blender 2.82 headless build these scripts run under (no Eevee Next / Principled
v2 assumptions) — every node/attribute here was confirmed live against that
exact version before use, not guessed from newer docs.

Import, don't copy: any future logo variant (a favicon crop, a second color
theme) should pull from here rather than re-implementing these.
"""
import bpy

# ── Palette — the one place both scripts get their colors from ─────────────
TEAL = (0.05, 0.85, 0.78)        # ordinary VM cubes / body color
AMBER = (1.0, 0.5, 0.05)         # the automation VM / hero accent
GLASS_TINT = (0.55, 0.75, 0.95)  # the outer host box


def clean_scene():
    """Wipe the default scene (cube/camera/light) — every generator starts here."""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


def glow_material(name, color, emit_strength=3.0, roughness=0.2, metallic=0.15, alpha=1.0):
    """
    A solid object that both glows (bloom-triggering Emission) AND responds to
    real light (Base Color/Roughness/Metallic on the same Principled BSDF) —
    confirmed live that this Blender's Principled BSDF has a built-in
    Emission input, unlike relying on a separate flat Emission shader (the
    original logo's approach): that made the inner cubes look like flat
    color swatches with a blurred edge, not solid lit objects, since a bare
    Emission shader has no specular response and ignores AO/shadows entirely.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.blend_method = "BLEND" if alpha < 1.0 else "OPAQUE"
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*[c * 0.6 for c in color], 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Emission"].default_value = (*[c * emit_strength for c in color], 1.0)
    bsdf.inputs["Alpha"].default_value = alpha
    return mat


def glass_material(name, tint, roughness=0.04, ior=1.42, alpha=0.28):
    """
    Principled BSDF with Transmission — confirmed live to actually render as
    translucent glass in this Blender's Eevee. A dedicated Glass BSDF node
    was tried first (the node graph looked correct — Color/Roughness/IOR all
    wired up as expected) but rendered as a near-opaque dark cube instead of
    glass; not root-caused further (Eevee's per-material vs. scene-wide
    screen-space-refraction settings are one likely culprit, unconfirmed) —
    reverted to this known-working recipe rather than spend more of this
    session's time on it. Confirmed by direct render comparison, not assumed
    from the node graph alone.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.blend_method = "BLEND"
    mat.show_transparent_back = False
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*tint, 1.0)
    bsdf.inputs["Transmission"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["IOR"].default_value = ior
    bsdf.inputs["Alpha"].default_value = alpha
    return mat


def emission_material(name, color, strength):
    """Pure emission, no light response — for thin accents (wireframe cages)
    where a real BSDF's specular highlights would just add noise."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    emit = nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = (*color, 1.0)
    emit.inputs["Strength"].default_value = strength
    mat.node_tree.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


def add_bevel(obj, width=0.015, segments=3):
    """
    Rounded edges — the single highest-impact change from the original logo.
    A perfectly sharp CG cube edge can't catch a specular highlight from any
    single light angle (zero-width surface), which is why the original render
    looked flat/matte despite having 3 lights; a beveled edge always has some
    facet angled toward each light.
    """
    bevel = obj.modifiers.new("bevel", type="BEVEL")
    bevel.width = width
    bevel.segments = segments
    bevel.limit_method = "ANGLE"
    return bevel


def setup_render(scene, draft, resolution_x, resolution_y=None, samples_draft=64, samples_final=192):
    """Common Eevee + film settings for a transparent-background render."""
    resolution_y = resolution_y or resolution_x
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.use_bloom = True
    scene.eevee.bloom_threshold = 0.85
    scene.eevee.bloom_intensity = 0.05
    scene.eevee.bloom_radius = 3.0
    scene.eevee.use_ssr = True
    scene.eevee.use_ssr_refraction = True
    scene.eevee.ssr_thickness = 0.2
    scene.eevee.use_soft_shadows = True
    # Ambient occlusion — the second highest-impact change: without it,
    # objects floating in front of/behind each other have no contact/depth
    # cue at all against a transparent background (no ground plane to shadow
    # onto), so nothing reads as "inside" anything else, just "overlapping".
    scene.eevee.use_gtao = True
    scene.eevee.gtao_distance = 0.4
    scene.eevee.gtao_factor = 1.0
    scene.eevee.taa_render_samples = samples_draft if draft else samples_final

    scene.render.film_transparent = True
    scene.render.resolution_x = resolution_x // 2 if draft else resolution_x
    scene.render.resolution_y = resolution_y // 2 if draft else resolution_y
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"


def setup_ambient_world(strength=0.6, color=(0.12, 0.18, 0.24)):
    """
    A dim cool-toned world background — invisible in the final render
    (film_transparent hides it from camera rays) but still feeds the glass/
    metallic surfaces' reflections and AO a non-black environment to bounce,
    which is what makes the glass box read as glass instead of a flat tinted
    silhouette. The original logo had no world lighting at all (pure black
    default world), so its glass had nothing to reflect.
    """
    world = bpy.data.worlds.new("AmbientWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs["Color"].default_value = (*color, 1.0)
    bg.inputs["Strength"].default_value = strength
    bpy.context.scene.world = world
    return world


def three_point_lighting(key_pos, fill_pos, rim_pos,
                          key_energy=900, fill_energy=250, rim_energy=450,
                          key_color=(1.0, 0.98, 0.92), fill_color=(0.6, 0.8, 1.0),
                          rim_color=(1.0, 0.6, 0.3), size=4.0):
    """Standard key/fill/rim area-light rig, parameterized so the icon and
    wordmark can share the same lighting mood without copy-pasted numbers."""
    lights = {}
    for name, pos, energy, color in (
        ("key", key_pos, key_energy, key_color),
        ("fill", fill_pos, fill_energy, fill_color),
        ("rim", rim_pos, rim_energy, rim_color),
    ):
        bpy.ops.object.light_add(type="AREA", location=pos)
        light = bpy.context.active_object
        light.data.energy = energy
        light.data.size = size
        light.data.color = color
        light.name = f"Light_{name}"
        lights[name] = light
    return lights


def aim_camera_at(camera, target_location=(0, 0, 0)):
    empty = bpy.data.objects.new("AimTarget", None)
    empty.location = target_location
    bpy.context.collection.objects.link(empty)
    track = camera.constraints.new(type="TRACK_TO")
    track.target = empty
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    return empty


def enable_dof(camera, focus_distance, fstop=2.8):
    """
    Photographic depth of field, focused on the hero element — kept SUBTLE
    (moderate f-stop, not a razor-thin macro blur) since this asset also
    has to read clearly at 128x128 favicon size, where heavy blur would just
    look like a rendering artifact rather than an intentional focus pull.
    """
    camera.data.dof.use_dof = True
    camera.data.dof.focus_distance = focus_distance
    camera.data.dof.aperture_fstop = fstop
