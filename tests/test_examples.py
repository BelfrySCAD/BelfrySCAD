"""The bundled example scripts and their menu manifest must agree.

`examples.json` drives the File > Examples menu (MainWindow.
_populate_examples_menu), and nothing else checks it against what is
actually on disk. A name listed but missing renders a dead menu entry; a
file present but unlisted is invisible in the app and easy to add and
forget. Both directions are checked, because a one-way check is how the
export format list drifted (see CLAUDE.md's Export section).

Pure filesystem/JSON, no GL or Qt, so it is safe under pytest.
"""

import json

from belfryscad.window.main_window import examples_dir


def _manifest() -> dict[str, list[str]]:
    return json.loads((examples_dir() / "examples.json").read_text(encoding="utf-8"))


def test_every_listed_example_exists():
    missing = [
        f"{category}/{name}"
        for category, names in _manifest().items()
        for name in names
        if not (examples_dir() / category / name).is_file()
    ]
    assert not missing, f"examples.json lists files that do not exist: {missing}"


def test_every_example_on_disk_is_listed():
    listed = {
        f"{category}/{name}"
        for category, names in _manifest().items()
        for name in names
    }
    on_disk = {
        f"{path.parent.name}/{path.name}"
        for path in examples_dir().glob("*/*.scad")
    }
    assert not (on_disk - listed), (
        f"example files not listed in examples.json (so invisible in the "
        f"Examples menu): {sorted(on_disk - listed)}"
    )


def test_categories_are_real_directories():
    for category in _manifest():
        assert (examples_dir() / category).is_dir(), f"no such category dir: {category}"
