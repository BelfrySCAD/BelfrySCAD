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
