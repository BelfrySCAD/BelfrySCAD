"""View ▸ Aspect Ratio: resize the main window so its viewport has a ratio.

A one-off resize, not a constraint: the window is free to be resized
afterwards. The viewport is resized rather than drawn letterboxed, so
picking, the projection, the size label and every capture (Save Image,
Copy Image, animation frames) get exactly that shape.
"""

# (menu label, width / height)
RATIOS = [("&1:1", 1.0), ("&4:3", 4 / 3), ("&3:2", 3 / 2),
          ("16:1&0", 16 / 10), ("16:&9", 16 / 9), ("&2:1", 2.0)]


def target(vw: int, vh: int, ratio: float, max_vw: int, max_vh: int) -> tuple[int, int]:
    """The viewport size to aim for: its longer side kept and the other
    fitted to `ratio`; or, when that won't fit on screen (`max_vw` x
    `max_vh`), its shorter side kept and the longer one fitted instead."""
    keep_width = (vw, round(vw / ratio))
    keep_height = (round(vh * ratio), vh)
    first, second = (keep_width, keep_height) if vw >= vh else (keep_height, keep_width)
    if first[0] <= max_vw and first[1] <= max_vh:
        return first
    return second
