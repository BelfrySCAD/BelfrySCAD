"""Exit 0 when BASE..HEAD changes nothing but BelfrySCAD's own version.

A release bump edits pyproject.toml's two `version` keys, uv.lock's record of
this package's version, and release.py's VERSION/DATE -- none of which can
change what the BOSL2 docs check checks, yet each one used to trigger its
full 18-minute run. Decided by parsing, not by matching diff lines: an
evaluator bump also changes a `version =` line in uv.lock, and must still run
the check, because that is the change it exists to catch.

    python version_only_change.py BASE HEAD    # exit 0 = version-only, 1 = not
"""
import subprocess
import sys
import tomllib

SELF = "belfryscad"
ALLOWED = {"pyproject.toml", "uv.lock", "src/belfryscad/release.py"}


def show(rev, path):
    r = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def pyproject_sans_version(text):
    d = tomllib.loads(text)
    d.get("project", {}).pop("version", None)
    d.get("tool", {}).get("briefcase", {}).pop("version", None)
    return d


def lock_sans_self_version(text):
    d = tomllib.loads(text)
    for p in d.get("package", []):
        if p.get("name") == SELF:
            p.pop("version", None)
    return d


def main(base, head):
    changed = subprocess.run(["git", "diff", "--name-only", base, head],
                             capture_output=True, text=True, check=True).stdout.split()
    if not changed:
        print("no changes")
        return 1
    extra = sorted(set(changed) - ALLOWED)
    if extra:
        print("not version-only; also changes:", ", ".join(extra[:10]))
        return 1
    for path, norm in (("pyproject.toml", pyproject_sans_version), ("uv.lock", lock_sans_self_version)):
        if path not in changed:
            continue
        a, b = show(base, path), show(head, path)
        if a is None or b is None or norm(a) != norm(b):
            print(f"not version-only: {path} changes more than {SELF}'s own version")
            return 1
    print("version-only change:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
