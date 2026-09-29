"""Regenerate every app icon from resources/belfryscad.scad.

    uv run python scripts/make_icons.py

Renders the model with BelfrySCAD's own headless renderer, at the default
camera fitted to the model (--viewall), in Cornfield's object colour. The
PNG export is always opaque, so it renders twice -- over black and over
white -- and recovers each pixel's true alpha from the difference, which
keeps anti-aliased edges clean where keying out a background colour would
leave a fringe. Then writes:

- resources/BelfrySCAD.png           1024px master
- resources/belfryscad.iconset/*     macOS sizes, and belfryscad.icns from them
                                     (iconutil, so macOS only)
- resources/belfryscad.ico           Windows: 16-256px, PNG-compressed entries
- resources/belfryscad-<N>.png       Linux (AppImage, Flatpak): 16-512px.
                                     briefcase looks for exactly these names and
                                     silently used its default icon without them
- src/belfryscad/resources/logo.png  192px (About-style uses of the logo)
- src/belfryscad/resources/logo_mesh.npz  the model itself, per coloured body
                                     (vertices, triangles, colour): the Welcome
                                     window's logo is a live viewport drawing
                                     this, so the app never has to parse or
                                     evaluate anything to show it

Downscales by repeated halving, since a single smooth scale from 1024 to 16
samples too few source pixels and aliases.
"""
from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "resources"
SOURCE = RES / "belfryscad.scad"
MASTER = 1024
CORNFIELD_OBJECT = (0.9765, 0.8431, 0.1725, 1.0)


def _render(tmp: Path) -> QImage:
    """The model at MASTER px with true alpha, from a black and a white render."""
    from belfryscad.headless_render import render_png
    from belfryscad.settings import use_scratch_settings
    from belfryscad.window.color_themes import save_custom_themes

    use_scratch_settings(str(tmp / "settings"), seed=False)   # never the user's real themes
    theme = {"object": CORNFIELD_OBJECT, "axes": (0, 0, 0, 1), "unselected_vertex": (0, 0, 0, 1)}
    save_custom_themes({"IconBlack": {**theme, "background": (0, 0, 0, 1)},
                        "IconWhite": {**theme, "background": (1, 1, 1, 1)}})
    shots = {}
    for name in ("IconBlack", "IconWhite"):
        out = tmp / f"{name}.png"
        if render_png(str(SOURCE), str(out), imgsize=f"{MASTER},{MASTER}", viewall=True,
                      colorscheme=name, quiet=True) != 0:
            sys.exit(f"render failed ({name})")
        img = QImage(str(out)).convertToFormat(QImage.Format.Format_RGBA8888)
        shots[name] = np.array(img.constBits(), dtype=np.float64).reshape(MASTER, MASTER, 4)[..., :3]

    black, white = shots["IconBlack"], shots["IconWhite"]
    # Over black a pixel is a*c; over white a*c + (1-a)*255. So the channel
    # difference is (1-a)*255, the same in all three; average it for noise.
    alpha = np.clip(1.0 - (white - black).mean(axis=2) / 255.0, 0.0, 1.0)
    rgb = np.where(alpha[..., None] > 0, black / np.maximum(alpha[..., None], 1e-6), 0)
    rgba = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).round().astype(np.uint8)
    img = QImage(rgba.tobytes(), MASTER, MASTER, MASTER * 4, QImage.Format.Format_RGBA8888)
    return img.copy()   # own the pixels; rgba goes out of scope


def _bake_mesh(path: Path):
    """The evaluated model's bodies as plain arrays, for the Welcome logo."""
    from belfryscad.headless import _evaluate

    result = _evaluate(str(SOURCE), {}, quiet=True)
    if result is None:
        sys.exit("evaluation failed")
    arrays = {}
    for i, b in enumerate(result[0]):
        if b.tri_colors is not None or b.color is None:
            sys.exit("the logo mesh needs one explicit color() per body")
        mesh = b.body.to_mesh()
        arrays[f"v{i}"] = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
        arrays[f"t{i}"] = np.asarray(mesh.tri_verts, dtype=np.int32)
        arrays[f"c{i}"] = np.asarray(b.color, dtype=np.float32)
    # What it was baked from, so test_welcome can tell the icons are stale
    # without re-rendering (the font the "B" uses exists only on macOS).
    arrays["source_sha256"] = np.array(hashlib.sha256(SOURCE.read_bytes()).hexdigest())
    np.savez_compressed(path, **arrays)


def _scaled(master: QImage, size: int) -> QImage:
    img = master
    while img.width() // 2 >= size:
        img = img.scaled(img.width() // 2, img.height() // 2,
                         Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if img.width() != size:
        img = img.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return img


def _png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def _write_ico(path: Path, master: QImage, sizes=(16, 32, 48, 64, 128, 256)):
    """ICONDIR + one ICONDIRENTRY per size + the PNGs; 256 is stored as 0."""
    blobs = [_png_bytes(_scaled(master, s)) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for s, blob in zip(sizes, blobs):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(out + b"".join(blobs))


def main():
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)   # noqa: F841 (QImage needs one)
    with tempfile.TemporaryDirectory(prefix="belfryscad-icons-") as tmp:
        master = _render(Path(tmp))

    master.save(str(RES / "BelfrySCAD.png"))
    iconset = RES / "belfryscad.iconset"
    for s in (16, 32, 128, 256, 512):
        _scaled(master, s).save(str(iconset / f"icon_{s}x{s}.png"))
        _scaled(master, s * 2).save(str(iconset / f"icon_{s}x{s}@2x.png"))
    if shutil.which("iconutil"):
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(RES / "belfryscad.icns")], check=True)
    else:
        print("iconutil not found (macOS only): belfryscad.icns left unchanged", file=sys.stderr)
    _write_ico(RES / "belfryscad.ico", master)
    for s in (16, 32, 64, 128, 256, 512):
        _scaled(master, s).save(str(RES / f"belfryscad-{s}.png"))
    _scaled(master, 192).save(str(ROOT / "src" / "belfryscad" / "resources" / "logo.png"))
    _bake_mesh(ROOT / "src" / "belfryscad" / "resources" / "logo_mesh.npz")
    print("icons written")


if __name__ == "__main__":
    main()
