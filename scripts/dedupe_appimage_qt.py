#!/usr/bin/env python3
"""Replace an AppDir's duplicated Qt libraries with symlinks.

briefcase hands linuxdeploy every folder in the AppDir that contains a `.so`
(`appimage.py`: `--deploy-deps-only` per folder). One of them is PySide6's,
so linuxdeploy resolves the wheel's Qt through its RUNPATH and copies all of
it into `usr/lib` -- beside the copy the wheel already shipped. The result is
129 Qt libraries present twice, about 300MB of a 343MB download.

Deleting either copy breaks something, which is why this replaces rather than
removes. The wheel's Python extension modules (`QtCore.abi3.so`) have
`RUNPATH=$ORIGIN/../../lib:$ORIGIN`, reaching `usr/lib`; but the xcb platform
plugin -- the one Qt aborts without -- has `RUNPATH=$ORIGIN/../../lib`
relative to `Qt/plugins/platforms`, which is the wheel's own `Qt/lib` and
nothing else. linuxdeploy patched some plugins to point at `usr/lib` and left
that one alone.

Pointing the wheel's copy at linuxdeploy's keeps both RUNPATHs satisfied and
changes nothing about what actually loads: the two have the same SONAME, so
the dynamic loader already resolves all of Qt to whichever was loaded first,
which is always `usr/lib`'s (Python imports PySide6 long before any plugin is
dlopened).

Usage: dedupe_appimage_qt.py <AppDir>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: Where the wheel keeps its Qt, and where linuxdeploy puts its copies.
WHEEL_QT_LIB = "usr/app_packages/PySide6/Qt/lib"
DEPLOY_LIB = "usr/lib"


def dedupe(appdir: Path) -> tuple[int, int]:
    """Symlink every wheel Qt library that `usr/lib` also has. Returns
    (files replaced, bytes reclaimed)."""
    wheel = appdir / WHEEL_QT_LIB
    deployed = appdir / DEPLOY_LIB
    if not wheel.is_dir() or not deployed.is_dir():
        return 0, 0

    count = saved = 0
    for path in sorted(wheel.iterdir()):
        # Only real files with a counterpart. Anything the wheel ships that
        # linuxdeploy did not copy stays exactly as it is.
        if path.is_symlink() or not path.is_file():
            continue
        target = deployed / path.name
        if not target.is_file():
            continue
        size = path.stat().st_size
        # Relative, so the link keeps working wherever the AppImage mounts:
        # Qt/lib -> Qt -> PySide6 -> app_packages -> usr, then down to lib.
        rel = os.path.relpath(target, path.parent)
        path.unlink()
        path.symlink_to(rel)
        count += 1
        saved += size
    return count, saved


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    appdir = Path(argv[1])
    if not appdir.is_dir():
        print(f"No such AppDir: {appdir}", file=sys.stderr)
        return 1
    count, saved = dedupe(appdir)
    print(f"Deduplicated {count} Qt libraries, {saved / 1048576:.0f}MB reclaimed.")
    # Nothing to do is not a failure -- but it IS worth saying loudly, since
    # it means the layout moved and this script has quietly stopped working.
    if count == 0:
        print("WARNING: no duplicates found; has the AppDir layout changed?",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
