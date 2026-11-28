"""3D title — genuine extruded typography for the opening
beat (mission §4: title sequences; the 3D counterpart of the
PIL staged title_sequence). The text is the recorded film
title only — the same fact-guarded data the archival title
uses. A key light sweeps across the letters while the camera
pushes in gently.

Payload data contract:
  title, kicker?, date_label?
Camera: dolly; lighting: three_point with an animated key.
"""

import bpy

from blender_scenes import common


def main():
    import os
    payload = common.load_payload()
    data = payload["data"]
    colors = payload["colors"]
    scene = common.clean_scene()
    common.set_world(scene, colors)
    materials = common.theme_materials(colors)
    fps = int(payload.get("fps") or 15)
    frames = max(2, int(round(float(payload.get("duration") or 5.0) * fps)))
    title = str(data.get("title") or "")[:60]
    size = 1.35 if len(title) <= 16 else 1.0
    common.add_text_3d(title.upper(), (0, 0, 1.55), size=size,
                       extrude=0.22, material=materials["body"])
    kicker = str(data.get("kicker") or "")[:80]
    if kicker:
        common.add_text_3d(kicker.upper(), (0, 0, 3.05), size=0.42,
                           extrude=0.05, material=materials["accent"])
    date_label = str(data.get("date_label") or "")[:40]
    if date_label:
        common.add_text_3d(date_label, (0, 0, 0.15), size=0.5,
                           extrude=0.05, material=materials["accent2"])
    common.add_ground(80.0, materials["ground"], z=-0.6)
    common.add_lighting("three_point", colors, target=(0, 0, 1.5),
                        scale=11.0)
    common.add_camera_rig("dolly", frames, target=(0, 0, 1.5),
                          radius=10.0, height=1.9, lens=50)
    common.setup_render(scene, payload, os.environ["CINEMORA_BLENDER_OUT"])
    common.render(scene)
