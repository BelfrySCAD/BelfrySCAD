"""
Compares two trees of rendered docsgen images, ranking by how differently
the GEOMETRY came out.

Built to answer "does our docs build still match OpenSCAD's?" across a whole
library -- BOSL2 is ~2576 images -- where eyeballing is hopeless and a plain
pixel diff is useless: every image differs, because the two renderers light
and label things differently.

The metric is the intersection-over-union of each image's FOREGROUND MASK
(pixels differing from the corner background colour), after averaging the
mask over 4x4 blocks and thresholding at half coverage.

  * Lighting drops out entirely. A shaded pixel is still foreground, so
    shading changes a pixel's colour but never the mask.
  * Axes, tick marks and scale labels mostly drop out. They are only a pixel
    or two wide, so the block average erases them, while a solid body
    survives untouched. Without that step they dominated the score: there
    are hundreds of them and they never land in quite the same place.

What it does NOT forgive is a missing, moved, or wrongly-scaled model, which
is the point.

Read the ranking with its limits in mind: a thin 2D line drawing of a few
hundred blocks scores badly for a 2-3 pixel offset, so a low score there
means "look at it", not "it is broken". Sort by how much of the image is
involved (ref_px) when triaging.

Usage:

  # rank one build against another
  uv run python scripts/compare_docs_images.py compare REF_DIR NEW_DIR \
      [--json out.json] [--worst 40]

  # what changed between two rankings (did a fix help? did it break anything?)
  uv run python scripts/compare_docs_images.py delta BEFORE.json AFTER.json

Producing the two trees, from inside the library checkout (BOSL2 writes to
BOSL2.wiki/, per its own .openscad_docsgen_rc; move each aside before the
next run):

  PATH=/path/to/openscad/bin:$PATH openscad-docsgen -m     # the reference
  belfryscad --docsgen -m                                  # ours

`openscad-docsgen` must find an `openscad` binary on PATH -- symlink the one
you mean to compare against, since a stale /Applications copy is a different
renderer (see the reference_openscad_binaries_available note).
"""

import argparse
import json
import pathlib
import sys

import numpy as np
from PySide6.QtGui import QImage

#: Side of the averaging block, in pixels. 4 erases the 1-2px lines axes and
#: glyph strokes are made of while leaving a solid body's interior intact.
BLOCK = 4

#: How far a pixel must sit from the background colour to count as
#: foreground. Loose enough to ignore PNG/antialiasing noise, tight enough
#: that a pale 2D slab still registers.
FG_THRESHOLD = 24


def load_rgb(path):
    """An (h, w, 3) int array, or None if the file will not decode.

    An animated PNG decodes to its first frame, which is what we want: a
    Spin example's frames differ from each other by design.
    """
    img = QImage(str(path))
    if img.isNull():
        return None
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    if w == 0 or h == 0:
        return None
    buf = np.frombuffer(memoryview(img.constBits()), np.uint8)
    return buf.reshape(h, img.bytesPerLine())[:, : w * 3].reshape(h, w, 3).astype(np.int16)


def foreground(rgb):
    """Mask of pixels that are not the background.

    The background is the median of the four corners rather than a fixed
    colour, so this works whatever colour scheme the images were built with.
    """
    corners = np.stack([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]])
    bg = np.median(corners, axis=0)
    return np.abs(rgb - bg).sum(axis=2) > FG_THRESHOLD


def thick_mask(mask):
    """`mask` with anything thinner than ~2px erased. See the module docstring."""
    h, w = mask.shape
    hh, ww = h // BLOCK * BLOCK, w // BLOCK * BLOCK
    blocks = mask[:hh, :ww].reshape(hh // BLOCK, BLOCK, ww // BLOCK, BLOCK)
    return blocks.mean(axis=(1, 3)) >= 0.5


def compare_pair(ref_path, new_path):
    ref, new = load_rgb(ref_path), load_rgb(new_path)
    if ref is None or new is None:
        return {"iou": 0.0, "note": "unreadable"}
    if ref.shape != new.shape:
        return {"iou": 0.0, "note": f"size {ref.shape[:2]} vs {new.shape[:2]}"}
    a, b = thick_mask(foreground(ref)), thick_mask(foreground(new))
    union = int((a | b).sum())
    return {
        "iou": 1.0 if union == 0 else int((a & b).sum()) / union,
        "ref_px": int(a.sum()),
        "new_px": int(b.sum()),
    }


def cmd_compare(args):
    ref_root, new_root = pathlib.Path(args.ref), pathlib.Path(args.new)
    ref_imgs = {p.relative_to(ref_root).as_posix() for p in ref_root.rglob("*.png")}
    new_imgs = {p.relative_to(new_root).as_posix() for p in new_root.rglob("*.png")}

    rows = []
    for rel in sorted(ref_imgs & new_imgs):
        row = compare_pair(ref_root / rel, new_root / rel)
        row["image"] = rel
        rows.append(row)
    rows.sort(key=lambda r: r["iou"])

    print(f"reference images : {len(ref_imgs)}")
    print(f"our images       : {len(new_imgs)}")
    # An image only one side produced is the loudest possible signal:
    # either we failed to render it, or -- as has happened -- the reference
    # crashed on it and we did not.
    for label, missing in (("only in reference", ref_imgs - new_imgs),
                            ("only in ours", new_imgs - ref_imgs)):
        print(f"{label:<17}: {len(missing)}")
        for rel in sorted(missing)[:20]:
            print(f"    {rel}")

    if rows:
        ious = np.array([r["iou"] for r in rows])
        print("\ngeometry IoU distribution:")
        for t in (0.995, 0.99, 0.95, 0.90, 0.75, 0.50, 0.20):
            print(f"  below {t:.3f}: {int((ious < t).sum()):5d}")
        print(f"\nworst {args.worst}:")
        for r in rows[: args.worst]:
            note = f"  [{r['note']}]" if r.get("note") else ""
            print(f"  {r['iou']:.3f}  {r['image']:<50}"
                  f" ref={r.get('ref_px', '?'):>6} ours={r.get('new_px', '?'):>6}{note}")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(rows))
        print(f"\nwrote {args.json}")
    return 0


def cmd_delta(args):
    """Which images a change actually moved -- and, more importantly, which
    it moved backwards."""
    before = {r["image"]: r["iou"] for r in json.loads(pathlib.Path(args.before).read_text())}
    after = {r["image"]: r["iou"] for r in json.loads(pathlib.Path(args.after).read_text())}
    shared = before.keys() & after.keys()
    deltas = sorted(((after[k] - before[k], k, before[k], after[k]) for k in shared), reverse=True)

    improved = [d for d in deltas if d[0] > args.threshold]
    regressed = [d for d in deltas if d[0] < -args.threshold]
    print(f"compared {len(shared)} images (threshold {args.threshold})")
    print(f"  improved : {len(improved)}")
    print(f"  REGRESSED: {len(regressed)}")
    for title, group in (("improvements", improved), ("regressions", reversed(regressed))):
        rows = list(group)
        if not rows:
            continue
        print(f"\n{title}:")
        for d, k, b, a in rows[:30]:
            print(f"  {d:+.3f}  {k:<50} {b:.3f} -> {a:.3f}")
    return 1 if regressed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compare", help="rank one image tree against another")
    c.add_argument("ref", help="reference tree (e.g. BOSL2.wiki from openscad-docsgen)")
    c.add_argument("new", help="our tree (e.g. BOSL2.wiki from belfryscad --docsgen)")
    c.add_argument("--json", help="write the full ranking here, for `delta` later")
    c.add_argument("--worst", type=int, default=40, help="how many worst pairs to list")
    c.set_defaults(func=cmd_compare)

    d = sub.add_parser("delta", help="what changed between two rankings")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--threshold", type=float, default=0.02,
                    help="ignore moves smaller than this (default 0.02)")
    d.set_defaults(func=cmd_delta)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
