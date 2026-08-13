"""Temporary .scad files, written where the parser can still resolve
relative `use`/`include` paths.

The C++ parser and evaluator are both path-based, so anything that needs to
evaluate text that isn't already a file on disk -- the live editor buffer, a
`-D` prelude appended to a script, an AI expression probe -- has to write a
temp file first. It goes in the *same directory* as the real script when
there is one, because a relative `use <lib/foo.scad>` inside it resolves
against wherever the file sits.

That last part is why these leak visibly: the temp lands next to the user's
own work rather than in /tmp, so anything left behind is sitting in their
project directory.

Every caller wraps creation in try/finally, which covers normal failures.
What it cannot cover is the process not getting to run the `finally` at all
-- SIGKILL, a hard crash, a segfault in the C++ evaluator. So this module
does two things beyond writing the file:

1. Names temps `belfryscad-*.scad` instead of tempfile's default
   `tmp*.scad`. An orphan is then obviously ours rather than mystery junk
   the user has to identify, and -- more usefully -- it is safe to delete
   automatically, which a bare `tmp*.scad` glob never is.
2. Sweeps its own stale orphans out of the directory each time it writes a
   new one. Self-healing after the crash that stranded them, without a
   startup hook or a scan of anywhere the user did not point us at.
"""
import os
import tempfile
import time
from pathlib import Path

_PREFIX = "belfryscad-"
_SUFFIX = ".scad"

# An orphan is by definition old. Anything younger than this could be a
# concurrently-running instance's live temp -- two BelfrySCAD windows
# rendering scripts from the same directory is ordinary -- and deleting it
# mid-render would break that render. An hour is far past any render and
# far short of "the user will notice the clutter".
_STALE_AFTER_SECONDS = 3600


def _sweep(directory: str) -> None:
    """Delete this module's own leftovers from `directory`.

    Deliberately narrow: only our prefix, only that one directory, only
    files old enough that nothing live could own them. Every failure is
    ignored -- a sweep that cannot run is not a reason to fail the render
    the caller actually asked for.
    """
    try:
        cutoff = time.time() - _STALE_AFTER_SECONDS
        for entry in os.scandir(directory):
            if not (entry.name.startswith(_PREFIX) and entry.name.endswith(_SUFFIX)):
                continue
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
            except OSError:
                pass
    except OSError:
        pass


def write_temp_scad(text: str, near=None) -> str:
    """Write `text` to a new temp .scad and return its path.

    `near` is the real script this text stands in for, if any -- the temp is
    created in that file's directory so relative use/include still resolve.
    With no `near`, the platform temp directory is used.

    The caller owns the returned path and should pass it to `remove()` when
    done, normally from a `finally`.
    """
    directory = str(Path(near).parent) if near else None
    if directory:
        _sweep(directory)
    fd, path = tempfile.mkstemp(prefix=_PREFIX, suffix=_SUFFIX, dir=directory)
    # mkstemp rather than NamedTemporaryFile(delete=False): it hands back
    # the path at the same moment the file starts existing, so there is no
    # window in which the file is on disk but the caller has nothing to
    # clean up yet. Every previous version of this assigned its path
    # variable only after writing and closing.
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        remove(path)
        raise
    return path


def remove(path) -> None:
    """Delete a temp written by `write_temp_scad`, ignoring failures."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
