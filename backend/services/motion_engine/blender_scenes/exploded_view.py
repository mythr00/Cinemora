"""Exploded view — a recorded mechanism/structure taken apart
in space: parts hold assembled, separate along their recorded
explode vectors, and hold apart (mission §4/§9: the
recorded-geometry exploded view).

Payload data contract (validated by the engine module before
the payload exists):
  parts: [{name, shape: box|cylinder|sphere|cone,
           size: [x,y,z] (box) | radius (cylinder/cone: size[0],
           height: size[1]), position: [x,y,z],
           explode: [dx,dy,dz], material: body|accent|accent2|
           muted|glass}]
Camera: orbit rig; lighting: three_point; ground plane.
"""

import os

import bpy

from blender_scenes import common


def _add_part(part, materials, frames):
    shape = part.get("shape") or "box"
    size = part.get("size") or [1.0, 1.0, 1.0]
    pos = part.get("position") or [0, 0, 0]
    if shape == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            radius=float(size[0]),
            depth=float(size[1]) if len(size) > 1 else 1.0,
            location=pos)
    elif shape == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=float(size[0]), location=pos)
    elif shape == "cone":
        bpy.ops.mesh.primitive_cone_add(
            radius1=float(size[0]),
            depth=float(size[1]) if len(size) > 1 else 1.0,
            location=pos)
    else:
        bpy.ops.mesh.primitive_cube_add(location=pos)
        obj = bpy.context.object
        obj.scale = (float(size[0]), float(size[1]), float(size[2]))
        bpy.ops.object.transform_apply(scale=True)
    obj = bpy.context.object
    obj.name = str(part.get("name") or "part")[:60]
    mat = materials.get(part.get("material") or "body",
                        materials["body"])
    obj.data.materials.append(mat)
    explode = part.get("explode") or [0, 0, 0]
    if any(float(v) != 0.0 for v in explode):
        hold_end = max(2, int(frames * 0.30))
        apart = int(frames * 0.82)
        obj.keyframe_insert(data_path="location", frame=1)
        obj.keyframe_insert(data_path="location", frame=hold_end)
        obj.location = (
            pos[0] + float(explode[0]),
            pos[1] + float(explode[1]),
            pos[2] + float(explode[2]),
        )
        obj.keyframe_insert(data_path="location", frame=apart)
        obj.keyframe_insert(data_path="location", frame=frames)
    return obj


def main():
    payload = common.load_payload()
    data = payload["data"]
    colors = payload["colors"]
    scene = common.clean_scene()
    common.set_world(scene, colors)
    materials = common.theme_materials(colors)
    common.add_ground(70.0, materials["ground"])
    frames = max(2, int(round(float(payload.get("duration") or 5.0)
                              * int(payload.get("fps") or 15))))
    for part in data["parts"]:
        _add_part(part, materials, frames)
    # Extent-aware framing: the camera radius covers the
    # exploded extent, so separated parts stay in frame (the
    # first evidence render cropped the lifted parts at the
    # fixed radius). The aim point rides to the exploded
    # stack's mid-height — with a tall explode the cap left
    # the top of frame when the camera aimed at the base
    # (second evidence render), so the vertical span is
    # accounted separately from the horizontal reach.
    extent = 0.0
    z_values = []
    for part in data["parts"]:
        pos = part.get("position") or [0, 0, 0]
        exp = part.get("explode") or [0, 0, 0]
        size = part.get("size") or [1.0]
        reach = max(
            abs(float(pos[i]) + float(exp[i])) for i in range(3))
        extent = max(extent, reach + max(float(s) for s in size))
        z_values.append(float(pos[2]))
        z_values.append(float(pos[2]) + float(exp[2]))
    z_mid = (min(z_values) + max(z_values)) / 2.0 if z_values else 0.9
    z_span = (max(z_values) - min(z_values)) if z_values else 0.0
    radius = min(30.0, max(10.5, extent * 2.1, z_span * 1.9))
    target = (0, 0, max(0.4, z_mid))
    common.add_lighting("three_point", colors, target=target,
                        scale=max(9.0, radius * 0.85))
    common.add_camera_rig(data.get("camera") or "orbit", frames,
                          target=target, radius=radius,
                          height=target[2] + 3.7 + radius * 0.08)
    common.setup_render(scene, payload, os.environ["CINEMORA_BLENDER_OUT"])
    common.render(scene)
