"""Tests for belfryscad.png_writer -- the hand-rolled PNG encoder (no
Pillow dependency; see its own module docstring for why)."""

import struct
import zlib

import pytest

from belfryscad.png_writer import write_png


def _read_ihdr(path):
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">IIBBBBB", data[16:29])


class TestWritePng:
    def test_writes_valid_png_signature_and_ihdr(self, tmp_path):
        path = tmp_path / "out.png"
        rgba = bytes([255, 0, 0, 255]) * (4 * 3)  # 4x3 solid red
        write_png(str(path), rgba, 4, 3)
        w, h, bit_depth, color_type, interlace, _c1, _c2 = _read_ihdr(path)
        assert (w, h, bit_depth, color_type, interlace) == (4, 3, 8, 6, 0)

    def test_roundtrips_pixel_data(self, tmp_path):
        path = tmp_path / "out.png"
        w, h = 2, 2
        rgba = bytes([10, 20, 30, 255, 40, 50, 60, 255, 70, 80, 90, 255, 100, 110, 120, 255])
        write_png(str(path), rgba, w, h)

        data = open(path, "rb").read()
        pos = 8
        idat = b""
        while pos < len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            tag = data[pos + 4:pos + 8]
            if tag == b"IDAT":
                idat += data[pos + 8:pos + 8 + length]
            pos += 8 + length + 4
        raw = zlib.decompress(idat)
        stride = w * 4
        rows = [raw[y * (stride + 1) + 1:y * (stride + 1) + 1 + stride] for y in range(h)]
        assert b"".join(rows) == rgba

    def test_wrong_byte_count_raises(self, tmp_path):
        with pytest.raises(ValueError):
            write_png(str(tmp_path / "out.png"), b"\x00" * 10, 4, 4)


def test_apng_round_trips_back_into_one_still_png_per_frame(tmp_path):
    """The docs pane animates by swapping frames, so what write_apng packs
    read_apng_frames must be able to take apart again."""
    from belfryscad.png_writer import read_apng_frames, write_apng

    w = h = 2
    frames = [bytes([i * 40, 0, 0, 255] * (w * h)) for i in range(5)]
    path = str(tmp_path / "a.png")
    write_apng(path, frames, w, h, delay_ms=120)

    out, delay = read_apng_frames(path)
    assert len(out) == 5 and delay == 120
    assert all(b.startswith(b"\x89PNG\r\n\x1a\n") and b.endswith(b"IEND\xae\x42\x60\x82")
               for b in out), "each frame must be a complete standalone PNG"
    assert len(set(out)) == 5, "frames must not all decode to the same bytes"


def test_a_still_png_reports_no_animation(tmp_path):
    from belfryscad.png_writer import read_apng_frames, write_png

    path = str(tmp_path / "s.png")
    write_png(path, bytes([1, 2, 3, 255] * 4), 2, 2)
    assert read_apng_frames(path) == ([], 0)
    assert read_apng_frames(str(tmp_path / "missing.png")) == ([], 0)
