"""Previewing a library from a checkout that is not in the libraries folder.

A library's own documentation examples include the library by name --
BOSL2's say `include <BOSL2/std.scad>`. That name is resolved against the
libraries folder, so previewing docs from a clone, worktree or review
checkout renders the examples against the *installed* copy instead of the
one being edited. Nothing reports this: the examples render, they just
render the wrong code, and a function the checkout adds comes out as
`Ignoring unknown module`.

The fix is a shim directory holding one symlink, `<name> -> <the checkout>`,
prepended to OPENSCADPATH for the evaluation. `include <BOSL2/std.scad>`
then lands in the checkout, and everything else still resolves through the
real libraries folder.

Detection is by agreement rather than configuration: if a script says
`include <NAME/rest>` and `rest` exists in the directory the script is being
run from, then that directory IS the library called NAME. A file that merely
*uses* BOSL2 has no `std.scad` beside it, so it is left alone.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

#: `use`/`include` with an angle-bracket target, as the parser accepts it.
_INCLUDE_RE = re.compile(r'\b(?:use|include)\s*<([^>]+)>')

#: (library name, real directory) -> shim directory. One per pair, reused:
#: a docs build evaluates hundreds of examples and they all want the same
#: symlink.
_shims: dict[tuple[str, str], str] = {}


#: How far above the script's own directory to look for the library root.
#: A suite lives in `tests/`, examples in `examples/`; three levels covers
#: those and anything similar without wandering off towards the home
#: directory.
_MAX_CLIMB = 3


def detect(src_dir: str, script_lines) -> tuple[str, str] | None:
    """`(library name, its root directory)` for the library the script is
    running from inside, or None.

    `include <BOSL2/std.scad>` names BOSL2 and asks for `std.scad` inside
    it; if `std.scad` is a real file at or above `src_dir`, that is the
    BOSL2 this script means.

    The climb is what makes a test suite work. A `.scadtest` runs from
    `tests/`, so `tests/std.scad` does not exist and only the parent
    identifies the library. Examples in an `examples/` subfolder are the
    same shape.

    The first include that resolves wins -- a script including two
    libraries by name can only be inside one of them, and that is the one
    whose files sit above it.
    """
    if not src_dir:
        return None
    base = Path(src_dir)
    roots = [base, *list(base.parents)[:_MAX_CLIMB]]
    for line in script_lines:
        for target in _INCLUDE_RE.findall(line):
            name, _, rest = target.partition("/")
            # No slash means a plain `include <foo.scad>`, which names no
            # library at all.
            if not rest or not name or name in (".", ".."):
                continue
            for root in roots:
                if (root / rest).is_file():
                    return name, str(root)
    return None


def _library_path_base() -> str:
    """What OPENSCADPATH should fall back to when we prepend to it.

    Setting OPENSCADPATH *replaces* the platform default rather than adding
    to it (api.cpp: `env = envPath ? envPath : dfltPath`), so a shim that
    simply assigned the variable would hide every installed library.
    """
    existing = os.environ.get("OPENSCADPATH")
    if existing:
        return existing
    from belfryscad.scad_deps import _library_dirs
    return os.pathsep.join(str(d) for d in _library_dirs())


def _link(shim: str, name: str, real: str):
    """Point `shim/name` at `real`, by whatever means this OS allows."""
    dest = os.path.join(shim, name)
    try:
        os.symlink(real, dest, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows refuses symlinks without Developer Mode or admin, but
        # grants directory junctions to anyone.
        if os.name != "nt":
            raise
        import _winapi
        _winapi.CreateJunction(real, dest)


def shim_dir(name: str, real: str) -> str:
    """A directory in which `name` resolves to `real`. Cached per pair."""
    real = str(Path(real).resolve())
    key = (name, real)
    if key not in _shims:
        shim = tempfile.mkdtemp(prefix="belfryscad-libshim-")
        _link(shim, name, real)
        _shims[key] = shim
        atexit.register(shutil.rmtree, shim, ignore_errors=True)
    return _shims[key]


@contextmanager
def library_shim(found: tuple[str, str] | None):
    """Resolve a library name to the checkout it was found in, for the
    duration of the block. `found` is what `detect` returned.

    A no-op when `found` is None, so callers can wrap unconditionally.

    ponytail: os.environ is process-wide, so a render on another thread
    during this block would see the shimmed path too. The block is one
    evaluate() call and the shim only ever *adds* a name that would
    otherwise resolve to the installed copy of the same library, so the
    blast radius is small. Give the parser a per-call search path and this
    can stop touching the environment at all.
    """
    if not found:
        yield
        return
    name, real = found
    try:
        path = shim_dir(name, real)
    except OSError:
        yield                    # no symlink, no shim; render as before
        return
    previous = os.environ.get("OPENSCADPATH")
    os.environ["OPENSCADPATH"] = path + os.pathsep + _library_path_base()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OPENSCADPATH", None)
        else:
            os.environ["OPENSCADPATH"] = previous
