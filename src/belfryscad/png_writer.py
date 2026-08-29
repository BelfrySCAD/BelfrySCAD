"""Minimal, dependency-free PNG encoder -- 8-bit RGBA only, no interlacing,
no palette. Written by hand instead of adding Pillow as a new dependency:
the format is small and well-bounded for this one use case (headless PNG
export), and stdlib zlib already does the actual compression work.

write_apng adds animated PNG on top of the same primitives, for docsgen's
Spin/Anim examples. APNG rather than GIF because GIF would need an LZW
encoder and a colour quantiser for what is already a 24-bit render, and
because openscad_docsgen's own UsePNGAnimations option exists precisely to
prefer it.
"""

import struct
import zlib


_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def _ihdr(width: int, height: int) -> bytes:
    return struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 6 = RGBA, no interlacing


def _compress_scanlines(rgba: bytes, width: int, height: int) -> bytes:
    """zlib stream of the image data, with one filter-type byte (0 = None)
    prepended to each scanline -- PNG's own per-row framing, not part of
    the pixel data itself."""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])
    return zlib.compress(bytes(raw), level=6)


def write_png(path: str, rgba: bytes, width: int, height: int):
    """rgba: width*height*4 raw bytes, row-major, top-to-bottom (matches
    OpenGL's own bottom-to-top framebuffer readback REVERSED by the
    caller -- see headless_render.py)."""
    if len(rgba) != width * height * 4:
        raise ValueError(f"expected {width * height * 4} bytes for a {width}x{height} RGBA image, got {len(rgba)}")

    with open(path, "wb") as f:
        f.write(_SIGNATURE)
        f.write(_chunk(b"IHDR", _ihdr(width, height)))
        f.write(_chunk(b"IDAT", _compress_scanlines(rgba, width, height)))
        f.write(_chunk(b"IEND", b""))


def write_apng(path: str, frames, width: int, height: int, delay_ms: int = 250, plays: int = 0):
    """Animated PNG. `frames` is a sequence of same-sized RGBA byte strings
    in the same top-to-bottom layout write_png expects. plays=0 loops
    forever, which is what a docs example wants.

    Every frame is stored full-size with dispose=NONE/blend=SOURCE, so each
    one simply overwrites the last. That skips APNG's inter-frame diffing
    entirely -- the frames of a spinning model differ nearly everywhere, so
    diffing would cost CPU and save almost nothing.
    """
    frames = list(frames)
    if not frames:
        raise ValueError("write_apng needs at least one frame")
    expected = width * height * 4
    for i, fr in enumerate(frames):
        if len(fr) != expected:
            raise ValueError(f"frame {i}: expected {expected} bytes for a {width}x{height} RGBA image, got {len(fr)}")

    def fctl(seq: int) -> bytes:
        # delay is a rational: delay_num/delay_den seconds, so ms over 1000.
        return struct.pack(">IIIIIHHBB", seq, width, height, 0, 0,
                           max(1, int(delay_ms)), 1000, 0, 0)

    with open(path, "wb") as f:
        f.write(_SIGNATURE)
        f.write(_chunk(b"IHDR", _ihdr(width, height)))
        # acTL must precede the first IDAT.
        f.write(_chunk(b"acTL", struct.pack(">II", len(frames), plays)))
        seq = 0
        # Frame 0 is the IDAT itself -- it doubles as the still image any
        # non-APNG-aware viewer shows.
        f.write(_chunk(b"fcTL", fctl(seq)))
        seq += 1
        f.write(_chunk(b"IDAT", _compress_scanlines(frames[0], width, height)))
        for fr in frames[1:]:
            f.write(_chunk(b"fcTL", fctl(seq)))
            seq += 1
            f.write(_chunk(b"fdAT", struct.pack(">I", seq) + _compress_scanlines(fr, width, height)))
            seq += 1
        f.write(_chunk(b"IEND", b""))
