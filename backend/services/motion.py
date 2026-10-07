from __future__ import annotations

from typing import Literal

Motion = Literal[
    "zoom_in",
    "zoom_out",
    "push_in_hold",
    "pan_right",
    "pan_left",
    "static_hold",
]


def choose_motion(kind: str, visual_class: str, query: str, index: int) -> Motion:
    q = (query or "").lower()
    if kind == "video":
        return "static_hold"
    if visual_class == "name_card" or "document" in q or "gavel" in q:
        return "push_in_hold" if index % 2 == 0 else "zoom_in"
    if any(w in q for w in ("lake", "woods", "street", "dock", "swamp")):
        return "pan_right" if index % 2 == 0 else "pan_left"
    return ("zoom_in", "zoom_out", "push_in_hold")[index % 3]


def zoompan_filter(motion: Motion, seconds: float, fps: int = 30, size: str = "1920x1080") -> str:
    """
    Upscale before zoompan so slow moves do not stutter.
    Sliding slideshow transitions must not be used here.
    """
    frames = max(int(seconds * fps), fps)
    # zoom increment: reach ~1.12x over the clip
    inc = max(0.00035, 0.12 / max(frames, 1))

    if motion == "zoom_in":
        z = f"min(zoom+{inc:.5f},1.12)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "zoom_out":
        z = f"if(eq(on,1),1.12,max(zoom-{inc:.5f},1.001))"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "push_in_hold":
        hold = max(int(frames * 0.35), 1)
        z = f"if(lte(on,{hold}),min(zoom+{inc*1.6:.5f},1.10),zoom)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        z = "1.18"
        x = f"(iw-iw/zoom)*on/{max(frames-1,1)}"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "pan_left":
        z = "1.18"
        x = f"(iw-iw/zoom)*(1-on/{max(frames-1,1)})"
        y = "ih/2-(ih/zoom/2)"
    else:
        z = "1.05"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"

    return (
        "scale=3840:-2,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={size}:fps={fps},"
        "format=yuv420p"
    )