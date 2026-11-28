"""Mechanical motion — a gear train built procedurally from
recorded gear data (mission §4: rotating machinery; §9 the
recorded-geometry mechanism capability). Gears are genuine
geometry: a rim cylinder plus procedurally placed teeth, joined
into one mesh that spins about its axle axis. Rotation is
keyframed continuously with LINEAR interpolation; speeds and
directions come from the recorded contract.

Payload data contract:
  gears: [{name, radius, teeth, thickness, position: [x,y,z],
           speed (rpm), direction: 1|-1, material}]
  shafts: [{position: [x,y,z], length, radius}] (optional)
Camera: orbit or dolly; lighting: three_point.
"""

import math

import bpy

from blender_scenes import common


def _gear(gear, materials):
    radius = float(gear["radius"])
    teeth = max(6, int(gear["teeth"]))
    thick = float(gear.get("thickness") or 0.5)
    pos = gear.get("position") or [0, 0, 0]
    mat = materials.get(gear.get("material") or "accent",
                        materials["accent"])
    bpy.ops.mesh.primitive_cylinder_add(
        radius=radius, depth=thick, location=pos)
    gear_obj = bpy.context.object
    gear_obj.name = str(gear.get("name") or "gear")[:60]
    gear_obj.data.materials.append(mat)
    tooth_w = 2 * math.pi * radius / teeth * 0.42
    tooth_objs = []
    for i in range(teeth):
        ang = 2 * math.pi * i / teeth
        bpy.ops.mesh.primitive_cube_add(location=(
            pos[0] + (radius + tooth_w * 0.30) * math.cos(ang),
            pos[1] + (radius + tooth_w * 0.30) * math.sin(ang),
            pos[2]))
        tooth = bpy.context.object
        tooth.scale = (tooth_w, tooth_w * 0.8, thick * 0.96)
        tooth.rotation_euler = (0, 0, ang)
        bpy.ops.object.transform_apply(scale=True)
        tooth.data.materials.append(mat)
        tooth_objs.append(tooth)
    # Join teeth into the rim: one mesh, one spin. (Gears lie in
    # the XY plane facing the camera; the spin axis is Z.)
    bpy.ops.object.select_all(action="DESELECT")
    for tooth in tooth_objs:
        tooth.select_set(True)
    gear_obj.select_set(True)
    bpy.context.view_layer.objects.active = gear_obj
    bpy.ops.object.join()
    # Hub: a small muted cylinder at the axle.
    bpy.ops.mesh.primitive_cylinder_add(
        radius=radius * 0.22, depth=thick * 1.3, location=pos)
    hub = bpy.context.object
    hub.data.materials.append(materials["muted"])
    bpy.ops.object.select_all(action="DESELECT")
    hub.select_set(True)
    gear_obj.select_set(True)
    bpy.context.view_layer.objects.active = gear_obj
    bpy.ops.object.join()
    # Stand the gear upright for the department camera (which
    # looks along +Y): bake a 90-degree X rotation into the
    # mesh so the object itself keeps identity rotation and the
    # spin keyframes turn cleanly about the world Y axle.
    gear_obj.rotation_euler = (math.radians(90), 0, 0)
    bpy.ops.object.transform_apply(rotation=True)
    return gear_obj


def _animate_rotation(obj, gear, frames, fps):
    speed = float(gear.get("speed") or 6.0)  # rpm
    direction = 1 if float(gear.get("direction") or 1) >= 0 else -1
    turns = speed / 60.0 * (frames / fps)
    obj.keyframe_insert(data_path="rotation_euler", frame=1)
    # The gear stands in the XZ plane (mesh rotation baked in
    # _gear), so the axle — and the spin — is the Y axis.
    obj.rotation_euler = (0.0,
                          direction * turns * 2 * math.pi, 0.0)
    obj.keyframe_insert(data_path="rotation_euler", frame=frames)
    if obj.animation_data and obj.animation_data.action:
        for layer in obj.animation_data.action.layers:
            for strip in layer.strips:
                channelbag = getattr(strip, "channelbag", None)
                for fcurve in getattr(channelbag, "fcurves", []) or []:
                    for point in fcurve.keyframe_points:
                        point.interpolation = "LINEAR"


def main():
    import os
    payload = common.load_payload()
    data = payload["data"]
    colors = payload["colors"]
    scene = common.clean_scene()
    common.set_world(scene, colors)
    materials = common.theme_materials(colors)
    common.add_ground(70.0, materials["ground"], z=-3.2)
    fps = int(payload.get("fps") or 15)
    frames = max(2, int(round(float(payload.get("duration") or 5.0) * fps)))
    for shaft in data.get("shafts") or []:
        pos = shaft.get("position") or [0, 0, 0]
        bpy.ops.mesh.primitive_cylinder_add(
            radius=float(shaft.get("radius") or 0.18),
            depth=float(shaft.get("length") or 6.0), location=pos)
        obj = bpy.context.object
        # Axles point at the camera (along Y).
        obj.rotation_euler = (math.radians(90), 0, 0)
        obj.data.materials.append(materials["muted"])
    for gear in data["gears"]:
        obj = _gear(gear, materials)
        _animate_rotation(obj, gear, frames, fps)
    common.add_lighting("three_point", colors, target=(0, 0, 0),
                        scale=10.0)
    common.add_camera_rig(data.get("camera") or "orbit", frames,
                          target=(0, 0, 0), radius=11.0, height=3.4)
    common.setup_render(scene, payload, os.environ["CINEMORA_BLENDER_OUT"])
    common.render(scene)
