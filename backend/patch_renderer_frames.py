from pathlib import Path

p = Path("services/renderer.py")
text = p.read_text(encoding="utf-8")

old = '''        duration = float(
            item.get(
                "duration",
                5,
            )
        )
'''

new = '''        # ----------------------------------------------------
        # FRAME-ACCURATE CUMULATIVE TIMING
        #
        # Use absolute render boundaries when available.
        # This prevents tiny 30-fps rounding errors from
        # accumulating across hundreds of clips.
        # ----------------------------------------------------

        if (
            item.get("render_start") is not None
            and item.get("render_end") is not None
        ):

            render_start = float(
                item["render_start"]
            )

            render_end = float(
                item["render_end"]
            )

            start_frame = round(
                render_start * 30
            )

            end_frame = round(
                render_end * 30
            )

            duration = (
                end_frame - start_frame
            ) / 30.0

        else:

            duration = float(
                item.get(
                    "duration",
                    5,
                )
            )
'''

if old not in text:
    raise SystemExit(
        "TARGET BLOCK NOT FOUND. "
        "renderer.py was not changed."
    )

text = text.replace(old, new, 1)

p.write_text(
    text,
    encoding="utf-8",
)

print("RENDERER FRAME TIMING PATCHED")
