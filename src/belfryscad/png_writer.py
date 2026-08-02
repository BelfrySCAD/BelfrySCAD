"""Minimal, dependency-free PNG encoder -- 8-bit RGBA only, no interlacing,
no palette. Written by hand instead of adding Pillow as a new dependency:
the format is small and well-bounded for this one use case (headless PNG
export), and stdlib zlib already does the actual compression work.
"""

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def write_png(path: str, rgba: bytes, width: int, height: int):
    """rgba: width*height*4 raw bytes, row-major, top-to-bottom (matches
    OpenGL's own bottom-to-top framebuffer readback REVERSED by the
    caller -- see headless_render.py)."""
    if len(rgba) != width * height * 4:
        raise ValueError(f"expected {width * height * 4} bytes for a {width}x{height} RGBA image, got {len(rgba)}")

    # One filter-type byte (0 = None) prepended to each scanline -- PNG's
    # own per-row framing, not part of the pixel data itself.
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 6 = RGBA, no interlacing
    idat = zlib.compress(bytes(raw), level=6)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr))
        f.write(_chunk(b"IDAT", idat))
        f.write(_chunk(b"IEND", b""))
