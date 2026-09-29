"""Design ▸ Send to Slicer: find the slicers installed here, and how to open a
file in each. BelfrySCAD's answer to OpenSCAD's Design ▸ 3D Print.

Qt-free, so detection and the launch command test without a window. The
filesystem, environment and PATH lookup are parameters for the same reason.

A printer prints G-code, not a model, so the one hand-off that reaches every
printer is the slicer: PrusaSlicer, OrcaSlicer and Bambu Studio already send
jobs on to PrusaLink, Bambu, OctoPrint and Klipper printers. Upstream's other
two targets were weighed and left out: an online service is a partnership
question, and OctoPrint's own slicing is a dead end.
"""
from __future__ import annotations

import glob
import ntpath
import os
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Slicer:
    id: str
    name: str
    path: str          # the .app bundle, executable, or flatpak app id
    kind: str          # "app" (macOS bundle), "exe", "flatpak", or "system"


SYSTEM_DEFAULT = Slicer("system", "System Default App", "", "system")

# Order matters: it is the order the chooser lists them in, and the first one
# found is pre-selected. PrusaSlicer, OrcaSlicer and Bambu Studio lead because
# they take a multi-coloured 3MF as multi-material -- one filament per object
# -- which is what BelfrySCAD's colour export is for. Cura and the rest import
# the parts, leaving filaments to be assigned by hand.
#
# (id, name, macOS bundles, Windows paths under Program Files / LocalAppData
# Programs (globs), Linux commands, flatpak ids). Bundle and install names as
# each project ships them today; Cura renamed itself Ultimaker -> UltiMaker,
# and OrcaSlicer's flatpak moved from SoftFever's namespace to its own.
_KNOWN = (
    ("prusaslicer", "PrusaSlicer", ("PrusaSlicer.app",),
     (r"Prusa3D\PrusaSlicer\prusa-slicer.exe",), ("prusa-slicer", "PrusaSlicer"),
     ("com.prusa3d.PrusaSlicer",)),
    ("orcaslicer", "OrcaSlicer", ("OrcaSlicer.app",),
     (r"OrcaSlicer\orca-slicer.exe",), ("orca-slicer", "OrcaSlicer"),
     ("io.github.orcaslicer.OrcaSlicer", "io.github.softfever.OrcaSlicer")),
    ("bambustudio", "Bambu Studio", ("BambuStudio.app",),
     (r"Bambu Studio\bambu-studio.exe",), ("bambu-studio",),
     ("com.bambulab.BambuStudio",)),
    ("cura", "UltiMaker Cura", ("UltiMaker Cura.app", "Ultimaker Cura.app"),
     (r"UltiMaker Cura*\UltiMaker-Cura.exe", r"Ultimaker Cura*\Ultimaker-Cura.exe"),
     ("cura", "UltiMaker-Cura"), ("com.ultimaker.cura",)),
    ("superslicer", "SuperSlicer", ("SuperSlicer.app",),
     (r"SuperSlicer\superslicer.exe",), ("superslicer", "SuperSlicer"), ()),
    # macOS bundle names only: their Windows and Linux installs are not
    # verified here. "Choose Slicer ▸ Other application…" covers them.
    ("crealityprint", "Creality Print", ("Creality Print.app", "CrealityPrint.app"), (), (), ()),
    ("crealityslicer", "Creality Slicer", ("Creality Slicer.app",), (), (), ()),
)


def _version_key(path: str) -> tuple:
    """Sort key putting "Cura 5.10.1" after "Cura 5.7.0", which a plain
    string sort does not."""
    return tuple(int(n) for n in re.findall(r"\d+", path))


def detect(platform: str | None = None, env=None, exists=os.path.exists, which=shutil.which,
           globber=glob.glob) -> list[Slicer]:
    """The known slicers installed on this machine, in _KNOWN's order.

    Inside BelfrySCAD's own Flatpak, nothing on the host can be launched or
    even seen, so this finds nothing; the system default app still works
    there, through the desktop portal."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if env.get("FLATPAK_ID"):
        return []
    found = []
    for sid, name, bundles, win_paths, commands, flatpaks in _KNOWN:
        hit = None
        if platform == "darwin":
            # posixpath/ntpath, not os.path: each branch describes one
            # platform's layout, and the tests run every branch on every OS.
            roots = ["/Applications", posixpath.join(env.get("HOME", ""), "Applications")]
            hit = next(((p, "app") for r in roots for b in bundles if exists(p := posixpath.join(r, b))), None)
        elif platform.startswith("win"):
            roots = [env.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)")]
            if env.get("LOCALAPPDATA"):
                roots.append(ntpath.join(env["LOCALAPPDATA"], "Programs"))
            for root in filter(None, roots):
                for pattern in win_paths:
                    matches = sorted(globber(ntpath.join(root, pattern)), key=_version_key)
                    if matches:
                        hit = (matches[-1], "exe")   # the newest of several side-by-side installs
                        break
                if hit:
                    break
        else:
            hit = next(((p, "exe") for c in commands if (p := which(c))), None)
            if hit is None:
                homes = [posixpath.join(env.get("HOME", ""), ".local/share/flatpak/exports/share/applications"),
                         "/var/lib/flatpak/exports/share/applications"]
                hit = next(((f, "flatpak") for f in flatpaks for h in homes
                            if exists(posixpath.join(h, f + ".desktop"))), None)
        if hit:
            found.append(Slicer(sid, name, hit[0], hit[1]))
    return found


def custom(path: str) -> Slicer:
    """A slicer the user pointed at: an .app bundle or an executable."""
    name = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
    return Slicer("custom", name, path, "app" if path.rstrip("/").endswith(".app") else "exe")


def launch_command(slicer: Slicer, file_path: str, platform: str | None = None) -> list[str] | None:
    """argv that opens `file_path` in `slicer`; None for the system default,
    which the caller opens through QDesktopServices instead."""
    platform = platform or sys.platform
    if slicer.kind == "system":
        return None
    if slicer.kind == "app":
        return ["open", "-a", slicer.path, file_path]
    if slicer.kind == "flatpak":
        # --file-forwarding hands the sandboxed slicer a document-portal path
        # it can read; a plain path into /tmp would be invisible to it.
        return ["flatpak", "run", "--file-forwarding", slicer.path, "@@", file_path, "@@"]
    return [slicer.path, file_path]
