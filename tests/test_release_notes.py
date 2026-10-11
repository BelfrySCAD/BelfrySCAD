"""Help ▸ Show Release Notes… -- the markdown, without a network."""
from belfryscad.window.release_notes import notes_markdown


def test_sections_per_release_newest_first_skipping_unpublished():
    md = notes_markdown([
        {"tag_name": "v1.3.0", "published_at": "2026-11-01T00:00:00Z", "body": "future"},
        {"tag_name": "v1.2.0", "published_at": "2026-10-10T12:00:00Z",
         "body": "## Highlights\r\nFaster.\n### Evaluator\n- Unchanged."},
        {"tag_name": "v1.1.9", "draft": True, "body": "secret"},
        {"tag_name": "v1.1.8", "prerelease": True, "body": "beta"},
        {"tag_name": "v1.1.0", "published_at": "2026-09-01T00:00:00Z", "body": None},
    ], current="1.2.0")
    assert md.index("# 1.2.0 — 2026-10-10") < md.index("# 1.1.0 — 2026-09-01")
    assert "### Highlights\nFaster." in md and "#### Evaluator" in md   # demoted
    assert "future" not in md and "1.3.0" not in md                  # newer than the app
    assert "secret" not in md and "beta" not in md
    assert "*No notes.*" in md and "\r" not in md


def test_bare_urls_become_whole_autolinks():
    # Qt's own autolinker stopped a compare URL at its "...", linking half of it.
    url = "https://github.com/BelfrySCAD/BelfrySCAD/compare/v1.70.7...v1.70.8"
    md = notes_markdown([{"tag_name": "v1.70.8", "body":
        f"**Full Changelog**: {url}\n* x by @a in https://github.com/o/r/pull/735.\n"
        "* [already](https://example.com/a) and <https://example.com/b>"}], current="1.70.8")
    assert f"<{url}>" in md
    assert "<https://github.com/o/r/pull/735>." in md          # sentence period stays outside
    assert "(https://example.com/a)" in md and "<<" not in md   # existing links untouched
