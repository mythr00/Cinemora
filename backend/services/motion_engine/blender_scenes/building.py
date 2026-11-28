"""Building — procedural illustrative architecture from a
recorded structure contract (mission §4: buildings and
structures). Floors stack as slabs with columns; the window
grid is real geometry; the recorded `lit_fraction` of windows
uses the emissive material. This is an ILLUSTRATIVE
visualisation of a recorded structure description — never a
claim about a specific real building's appearance (the engine
metadata stamps it illustrative).

Payload data contract:
  structure: {floors (1..40), width, depth, floor_height,
              lit_fraction (0..1), material}
Camera: crane or orbit; lighting: soft_top.
"""

import random

import bpy

from blender_scenes import common


def main():
    import os
    payload = common.load_payload()
    data = payload["data"]
    structure = data["structure"]
    colors = payload["colors"]
    scene = common.clean_scene()
    common.set_world(scene, colors)
    materials = common.theme_materials(colors)
    common.add_ground(90.0, materials["ground"])
    floors = max(1, min(40, int(structure.get("floors") or 6)))
    width = float(structure.get("width") or 10.0)
    depth = float(structure.get("depth") or 8.0)
    floor_h = float(structure.get("floor_height") or 1.1)
    lit_fraction = float(structure.get("lit_fraction") or 0.35)
    body_mat = materials.get(structure.get("material") or "body",
                             materials["body"])
    # Deterministic window lighting from the recorded fraction.
    rng = random.Random(20261128)
    for floor in range(floors):
        z = 0.35 + floor * floor_h + floor_h / 2
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, z))
        slab = bpy.context.object
        slab.scale = (width / 2, depth / 2, floor_h * 0.42)
        bpy.ops.object.transform_apply(scale=True)
        slab.data.materials.append(body_mat)
        # Window band on the front face: real thin boxes, lit or
        # glass by the recorded fraction.
        cols = max(2, int(width / 1.6))
        for col in range(cols):
            x = -width / 2 + (col + 0.5) * (width / cols)
            bpy.ops.mesh.primitive_cube_add(
                location=(x, -depth / 2 - 0.03, z))
            win = bpy.context.object
            win.scale = (0.42, 0.03, floor_h * 0.26)
            bpy.ops.object.transform_apply(scale=True)
            win.data.materials.append(
                materials["lit"] if rng.random() < lit_fraction
                else materials["glass"])
    total_h = 0.35 + floors * floor_h
    # Roof slab + parapet.
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, total_h + 0.18))
    roof = bpy.context.object
    roof.scale = (width / 2 + 0.25, depth / 2 + 0.25, 0.18)
    bpy.ops.object.transform_apply(scale=True)
    roof.data.materials.append(materials["muted"])
    fps = int(payload.get("fps") or 15)
    frames = max(2, int(round(float(payload.get("duration") or 5.0) * fps)))
    centre = (0, 0, total_h / 2)
    common.add_lighting("soft_top", colors, target=centre, scale=12.0)
    common.add_camera_rig(data.get("camera") or "crane", frames,
                          target=centre,
                          radius=max(width, depth) * 2.3,
                          height=total_h * 0.55)
    common.setup_render(scene, payload, os.environ["CINEMORA_BLENDER_OUT"])
    common.render(scene)
