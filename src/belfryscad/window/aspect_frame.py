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
    fitted to `ratio`, shrunk (ratio kept) to the largest that fits on
    screen (`max_vw` x `max_vh`) if it would be bigger."""
    w, h = (vw, vw / ratio) if vw >= vh else (vh * ratio, vh)
    scale = min(1.0, max_vw / w, max_vh / h)
    return round(w * scale), round(h * scale)
