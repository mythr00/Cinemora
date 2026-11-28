"""Terrain — a real 3D landscape built from a recorded
elevation grid (mission §4 terrain/environments; §9
satellite-to-terrain direction: the grid comes from recorded
elevation data — the engine module records its provenance —
never from invented noise). The mesh is genuine geometry
displaced by the grid; vertex colours shade by elevation;
recorded markers stand as emissive pins.

Payload data contract:
  terrain: {grid: [[0..1, ...], ...] (rows x cols, >= 4x4),
            relief (world height of value 1.0),
            markers: [{x: 0..1, y: 0..1, label?}]}
Camera: orbit at a low angle; lighting: sun.
"""

import bpy

from blender_scenes import common


def _shade_by_height(obj, colors):
    """Vertex colours from elevation: valley -> slope -> peak,
    from the theme tokens, so the relief reads at a glance."""
    mesh = obj.data
    layer = mesh.vertex_colors.new(name="elevation")
    low = common.hex_rgba(colors["muted"])
    mid = common.hex_rgba(colors["surface"])
    high = common.hex_rgba(colors["accent2"])
    zs = [v.co.z for v in mesh.vertices]
    z_min, z_max = min(zs), max(zs)
    span = (z_max - z_min) or 1.0
    for poly in mesh.polygons:
        for loop_index in poly.loop_indices:
            vertex = mesh.loops[loop_index].vertex_index
            t = (mesh.vertices[vertex].co.z - z_min) / span
            if t < 0.5:
                a, b, f = low, mid, t * 2
            else:
                a, b, f = mid, high, (t - 0.5) * 2
            layer.data[loop_index].color = (
                a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f,
                a[2] + (b[2] - a[2]) * f, 1.0)
    mat = bpy.data.materials.new("terrain")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.95
    vcol = nodes.new("ShaderNodeVertexColor")
    vcol.layer_name = "elevation"
    links.new(vcol.outputs["Color"], bsdf.inputs["Base Color"])
    mesh.materials.append(mat)


def main():
    import os
    payload = common.load_payload()
    data = payload["data"]
    terrain = data["terrain"]
    grid = terrain["grid"]
    colors = payload["colors"]
    scene = common.clean_scene()
    common.set_world(scene, colors)
    materials = common.theme_materials(colors)
    rows = len(grid)
    cols = len(grid[0])
    size = 16.0
    relief = float(terrain.get("relief") or 3.2)
    verts = []
    for r in range(rows):
        for c in range(cols):
            x = -size / 2 + size * c / (cols - 1)
            y = -size / 2 + size * r / (rows - 1)
            verts.append((x, y, float(grid[r][c]) * relief))
    faces = []
    for r in range(rows - 1):
        for c in range(cols - 1):
            a = r * cols + c
            faces.append((a, a + 1, a + 1 + cols, a + cols))
    mesh = bpy.data.meshes.new("terrain")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("terrain", mesh)
    scene.collection.objects.link(obj)
    _shade_by_height(obj, colors)
    for marker in terrain.get("markers") or []:
        mx = -size / 2 + size * float(marker.get("x") or 0.5)
        my = -size / 2 + size * float(marker.get("y") or 0.5)
        r = min(rows - 1, max(0, round(float(marker.get("y") or 0.5)
                                      * (rows - 1))))
        c = min(cols - 1, max(0, round(float(marker.get("x") or 0.5)
                                      * (cols - 1))))
        mz = float(grid[r][c]) * relief
        bpy.ops.mesh.primitive_cylinder_add(
            radius=0.07, depth=1.1, location=(mx, my, mz + 0.55))
        pin = bpy.context.object
        pin.data.materials.append(materials["accent"])
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=0.26, location=(mx, my, mz + 1.25))
        head = bpy.context.object
        head.data.materials.append(materials["lit"])
    fps = int(payload.get("fps") or 15)
    frames = max(2, int(round(float(payload.get("duration") or 5.0) * fps)))
    common.add_lighting("sun", colors, target=(0, 0, 1.0))
    common.add_camera_rig(data.get("camera") or "orbit", frames,
                          target=(0, 0, 0.8), radius=21.0, height=8.5)
    common.setup_render(scene, payload, os.environ["CINEMORA_BLENDER_OUT"])
    common.render(scene)
