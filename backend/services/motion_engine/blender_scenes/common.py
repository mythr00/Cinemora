"""Cinemora Blender department — shared scene library.

Fixed production code for the Blender render engine (mission
§4/§8): camera rigs, lighting presets, materials and the render
setup every department scene builds on. Production data reaches
scenes ONLY through the JSON payload (the driver loads it from
the file named by CINEMORA_BLENDER_DATA) — data is never
interpolated into code, so a crafted label cannot inject scene
code (the Manim engine's discipline, unchanged).

Render engine: Cycles on CPU. EEVEE/Workbench need a system EGL
the production host class does not guarantee; Cycles CPU is the
engine that renders identically everywhere, with real shadows,
reflections and materials. Scenes render PNG sequences
(RGBA when the payload asks for transparency) at a reduced
internal resolution; the engine module conforms them to the
house format with one ffmpeg pass.
"""

import json
import math
import os

import bpy


def load_payload():
    with open(os.environ["CINEMORA_BLENDER_DATA"], "r",
              encoding="utf-8") as fh:
        return json.load(fh)


def hex_rgba(value, alpha=1.0):
    value = (value or "#888888").lstrip("#")
    r = int(value[0:2], 16) / 255.0
    g = int(value[2:4], 16) / 255.0
    b = int(value[4:6], 16) / 255.0
    # Blender colour inputs are linear; payload colours are sRGB
    # theme tokens — convert so scenes match the house palette.
    def _lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return (_lin(r), _lin(g), _lin(b), alpha)


def clean_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return bpy.context.scene


def make_material(name, color, metallic=0.0, roughness=0.55,
                  emission=None, emission_strength=0.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = hex_rgba(color)
    bsdf.inputs["Metallic"].default_value = float(metallic)
    bsdf.inputs["Roughness"].default_value = float(roughness)
    if emission is not None:
        bsdf.inputs["Emission Color"].default_value = hex_rgba(emission)
        bsdf.inputs["Emission Strength"].default_value = float(
            emission_strength)
    return mat


def theme_materials(colors):
    """The reusable material library (§8): body / accent /
    secondary / ground / glass / emissive, from the style tokens
    the pipeline resolved — scenes share the film's palette."""
    return {
        "body": make_material("body", colors["surface"], metallic=0.05,
                              roughness=0.5),
        "accent": make_material("accent", colors["accent"],
                                metallic=0.25, roughness=0.35),
        "accent2": make_material("accent2", colors["accent2"],
                                 metallic=0.25, roughness=0.35),
        "muted": make_material("muted", colors["muted"], metallic=0.0,
                               roughness=0.8),
        "ground": make_material("ground", colors["bg"], metallic=0.0,
                                roughness=0.95),
        "glass": make_material("glass", colors["accent2"], metallic=0.1,
                               roughness=0.12),
        "lit": make_material("lit", colors["accent2"],
                             emission=colors["accent2"],
                             emission_strength=1.6),
    }


def set_world(scene, colors, transparent=False):
    world = bpy.data.worlds.new("world")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    if transparent:
        bg.inputs["Color"].default_value = (0, 0, 0, 1)
        bg.inputs["Strength"].default_value = 0.0
    else:
        bg.inputs["Color"].default_value = hex_rgba(colors["bg"])
        bg.inputs["Strength"].default_value = 0.55


def add_ground(size=60.0, material=None, z=0.0):
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0, 0, z))
    ground = bpy.context.object
    ground.name = "ground"
    if material is not None:
        ground.data.materials.append(material)
    return ground


def add_lighting(preset, colors, target=(0, 0, 1.0), scale=10.0):
    """Reusable lighting presets (§4/§8). `sun` — documentary
    exterior (one strong sun + soft world); `three_point` —
    studio product lighting for objects/mechanisms; `soft_top`
    — interiors/architecture (big soft top light + rim)."""
    if preset == "sun":
        bpy.ops.object.light_add(type="SUN", location=(6, -4, 10))
        sun = bpy.context.object
        sun.data.energy = 3.2
        sun.rotation_euler = (math.radians(48), math.radians(8),
                              math.radians(38))
        bpy.ops.object.light_add(type="AREA", location=(-8, 6, 6))
        fill = bpy.context.object
        fill.data.energy = 220.0
        fill.data.shape = "DISK"
        fill.data.size = 9.0
        _point_at(fill, target)
        return
    if preset == "soft_top":
        bpy.ops.object.light_add(type="AREA", location=(0, 0, 14))
        top = bpy.context.object
        top.data.energy = 900.0
        top.data.shape = "DISK"
        top.data.size = 12.0
        _point_at(top, target)
        bpy.ops.object.light_add(type="AREA", location=(-9, -7, 5))
        rim = bpy.context.object
        rim.data.energy = 500.0
        rim.data.size = 6.0
        _point_at(rim, target)
        bpy.ops.object.light_add(type="SUN", location=(5, 6, 8))
        sun = bpy.context.object
        sun.data.energy = 1.1
        return
    # three_point (default)
    key_pos = (scale * 0.8, -scale * 0.7, scale * 0.9)
    fill_pos = (-scale * 0.9, -scale * 0.3, scale * 0.45)
    rim_pos = (scale * 0.2, scale * 0.95, scale * 0.8)
    for pos, energy, size in ((key_pos, 1100.0, 6.0),
                              (fill_pos, 380.0, 8.0),
                              (rim_pos, 800.0, 5.0)):
        bpy.ops.object.light_add(type="AREA", location=pos)
        light = bpy.context.object
        light.data.energy = energy
        light.data.shape = "DISK"
        light.data.size = size
        _point_at(light, target)


def _point_at(obj, target):
    direction = (target[0] - obj.location.x,
                 target[1] - obj.location.y,
                 target[2] - obj.location.z)
    obj.rotation_euler = _vec_to_euler(direction)


def _vec_to_euler(direction):
    # -Z forward, Y up: the rotation Blender's own
    # Vector.to_track_quat computes; done arithmetically here so
    # the module needs no mathutils import at scene scope.
    import mathutils
    return mathutils.Vector(direction).to_track_quat("-Z", "Y").to_euler()


def add_camera_rig(kind, frames, target=(0, 0, 1.0), radius=11.0,
                   height=5.0, lens=42):
    """Reusable camera rigs (§4/§8). Every rig aims at `target`
    through a Track-To constraint on a target empty, and moves
    on smooth (auto-eased) keyframes:
      orbit  — a slow ~40° arc around the subject;
      dolly  — a straight push-in toward the subject;
      crane  — a rise with a settling tilt;
      static — a locked frame (diagrams, text)."""
    cam_data = bpy.data.cameras.new("camera")
    cam_data.lens = lens
    cam = bpy.data.objects.new("camera", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    anchor = bpy.data.objects.new("camera_target", None)
    anchor.location = target
    bpy.context.scene.collection.objects.link(anchor)
    track = cam.constraints.new(type="TRACK_TO")
    track.target = anchor
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    tx, ty, tz = target
    if kind == "dolly":
        positions = [
            (tx + radius * 0.95, ty - radius * 1.25, height * 1.1),
            (tx + radius * 0.45, ty - radius * 0.62, height * 0.8),
        ]
    elif kind == "crane":
        positions = [
            (tx + radius * 0.7, ty - radius * 0.9, height * 0.25),
            (tx + radius * 0.55, ty - radius * 0.75, height * 1.5),
        ]
    elif kind == "static":
        positions = [
            (tx + radius * 0.55, ty - radius * 0.85, height),
            (tx + radius * 0.55, ty - radius * 0.85, height),
        ]
    else:  # orbit
        positions = []
        for frac in (0.0, 0.5, 1.0):
            # A ±30° arc (tightened from ±38° after the first
            # mechanical evidence render: at the wider extreme
            # the subject drifted high in frame as the camera
            # dropped and swung).
            ang = math.radians(-30 + 60 * frac)
            positions.append((
                tx + radius * math.sin(ang) * 1.05,
                ty - radius * math.cos(ang),
                height * (1.0 - 0.10 * frac),
            ))
    n = len(positions)
    for i, pos in enumerate(positions):
        frame = 1 if n == 1 else 1 + round((frames - 1) * i / (n - 1))
        cam.location = pos
        cam.keyframe_insert(data_path="location", frame=frame)
    if cam.animation_data and cam.animation_data.action:
        for layer in cam.animation_data.action.layers:
            for strip in layer.strips:
                channelbag = getattr(strip, "channelbag", None)
                fcurves = getattr(channelbag, "fcurves", None)
                if fcurves:
                    for fcurve in fcurves:
                        for point in fcurve.keyframe_points:
                            point.interpolation = "BEZIER"
    return cam


def add_text_3d(text, location, size=1.0, extrude=0.12,
                material=None, align="CENTER"):
    curve = bpy.data.curves.new("text", type="FONT")
    curve.body = text
    curve.size = size
    curve.extrude = extrude
    curve.align_x = align
    curve.align_y = "CENTER"
    obj = bpy.data.objects.new("text", curve)
    obj.location = location
    # Department cameras look along +Y, so text stands in the
    # XZ plane: rotate the font curve's XY plane upright.
    import math as _math
    obj.rotation_euler = (_math.radians(90), 0, 0)
    bpy.context.scene.collection.objects.link(obj)
    if material is not None:
        curve.materials.append(material)
    return obj


def setup_render(scene, payload, out_dir):
    """Cycles CPU, adaptive sampling, PNG sequence. Internal
    resolution comes from the payload (the engine module renders
    reduced and conforms up — resolution-aware rendering, §10).
    Returns the frame count."""
    duration = float(payload.get("duration") or 5.0)
    fps = int(payload.get("fps") or 15)
    frames = max(1, int(round(duration * fps)))
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = int(payload.get("samples") or 12)
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.adaptive_threshold = 0.03
    # Measured on this host class (2026-11-28): Cycles CPU is
    # bound by ~1.5 throttled shared cores (Blender's own
    # factory-default cube scene needs ~8 s at 640x360/8spp), so
    # the department renders lean: OpenImageDenoise cleans the
    # low sample counts and capped light paths keep diffuse /
    # glossy transport cheap for these stylised scenes.
    scene.cycles.use_denoising = True
    scene.cycles.denoiser = "OPENIMAGEDENOISE"
    scene.cycles.max_bounces = 6
    scene.cycles.diffuse_bounces = 3
    scene.cycles.glossy_bounces = 3
    scene.cycles.transmission_bounces = 2
    scene.cycles.volume_bounces = 0
    scene.render.resolution_x = int(payload.get("width") or 1280)
    scene.render.resolution_y = int(payload.get("height") or 720)
    scene.render.resolution_percentage = 100
    scene.render.fps = fps
    scene.frame_start = 1
    scene.frame_end = frames
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = os.path.join(out_dir, "frame_")
    scene.render.film_transparent = bool(payload.get("transparent"))
    return frames


def render(scene):
    bpy.ops.render.render(animation=True)
