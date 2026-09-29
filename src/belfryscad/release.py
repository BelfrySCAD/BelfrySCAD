"""The date this version was released, for the About and Welcome windows.

Nothing records it otherwise: package metadata has no release date, and an
editable install has no build date. So it is written here, beside the
version it belongs to, in the release's own bump commit (see CLAUDE.md's
Versioning). test_release.py fails when pyproject.toml's version moves on
without VERSION, so the date cannot be left behind silently.
"""
VERSION = "1.60.0"
DATE = "2026-09-29"   # ISO 8601: unambiguous in every locale
