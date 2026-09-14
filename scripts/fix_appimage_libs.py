#!/usr/bin/env python3
"""Two fixes to an AppDir's bundled libraries, applied before it is packed.

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

The second fix drops libraries that MUST come from the host. A bundled
library is safe only if everything that links against it is bundled too. Qt
loads `libxkbcommon-x11.so.0`, which the wheel does not ship and linuxdeploy
therefore never copied -- so the host's current `-x11` half was being paired
with the bundle's older core half, and `xkb_x11_keymap_new_from_device`
walked off the end of it. Every Qt app in the AppImage segfaulted at
`QApplication()` on any distro new enough for the two halves to have
diverged (#430).

Reproduced and fixed on CI, not reasoned about: the released AppImage exits
139 under xvfb as shipped, and prints "qt ok" with this one file removed.

Note glib/gio/gobject are NOT in that category and are left alone -- they
are bundled as a complete set, so nothing pairs a bundled half with a host
half.

Usage: fix_appimage_libs.py <AppDir>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: Bundled libraries whose companions are not bundled, so both halves have
#: to come from the host. Matched as `<name>.so*`.
SPLIT_LIBRARIES = ("libxkbcommon",)

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


def drop_split_libraries(appdir: Path) -> list:
    """Delete the bundled halves of libraries whose other half is not
    bundled. Returns the paths removed."""
    removed = []
    lib = appdir / DEPLOY_LIB
    if not lib.is_dir():
        return removed
    for stem in SPLIT_LIBRARIES:
        for path in sorted(lib.glob(f"{stem}.so*")):
            path.unlink()
            # as_posix: an AppDir path is POSIX whatever builds it, and this
            # string is both printed and asserted on.
            removed.append(path.relative_to(appdir).as_posix())
    return removed


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
    for path in drop_split_libraries(appdir):
        print(f"Dropped {path} -- must come from the host (#430).")
    # Nothing to do is not a failure -- but it IS worth saying loudly, since
    # it means the layout moved and this script has quietly stopped working.
    if count == 0:
        print("WARNING: no duplicates found; has the AppDir layout changed?",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
