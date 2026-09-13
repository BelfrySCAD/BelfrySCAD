"""Package versions, read the way a stacked install requires.

Qt-free on purpose: both the GUI's About box and the CLI's --version/--info
ask, and the CLI must not drag Qt in to answer.
"""
from __future__ import annotations

import re


def _version_key(text: str) -> tuple:
    """Sort key for a version string. Never raises: an unparsable chunk
    sorts below any number rather than blowing up the About box."""
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[._\-+]", text))


def package_version(package: str, default: str = "unknown") -> str:
    """The installed version of `package` -- the HIGHEST, when more than one
    is visible.

    A Windows installer that fails to replace its predecessor leaves two
    `belfryscad-X.Y.Z.dist-info` directories side by side in the same
    folder. `importlib.metadata.version()` then answers with whichever it
    enumerates FIRST, which is directory order, which is alphabetical --
    so `belfryscad-1.18.6` beats `belfryscad-1.23.2` and About reports a
    version older than the code actually running (#411).

    The highest is the right answer in that situation: the installs share
    one directory, so the newest one's files overwrote the shared paths and
    are what is executing. Only the metadata stacked up.
    """
    import importlib.metadata

    wanted = package.lower().replace("-", "_")
    found = []
    for dist in importlib.metadata.distributions():
        try:
            name = dist.metadata["Name"]
        except Exception:       # noqa: BLE001 -- a broken sibling must not hide a good one
            continue
        if name and name.lower().replace("-", "_") == wanted and dist.version:
            found.append(dist.version)
    if not found:
        return default
    return max(found, key=_version_key)


def duplicate_installs(package: str) -> list:
    """Every version of `package` visible at once, when there is more than
    one -- the signature of an installer that did not replace what was
    there. Empty in the normal case."""
    import importlib.metadata

    wanted = package.lower().replace("-", "_")
    found = []
    for dist in importlib.metadata.distributions():
        try:
            name = dist.metadata["Name"]
        except Exception:       # noqa: BLE001
            continue
        if name and name.lower().replace("-", "_") == wanted and dist.version:
            found.append(dist.version)
    return sorted(set(found), key=_version_key) if len(found) > 1 else []
