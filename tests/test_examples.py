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
import sys

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


def test_examples_find_their_data_files_when_opened_as_copies():
    """An example opens as an untitled copy, so its render temp used to land
    in the system temp folder, where `data/knob.stl` does not exist (#883)."""
    from openscad_cpp_evaluator import Evaluator
    from belfryscad import scad_temp
    from belfryscad.window.main_window import example_categories, example_resolve_path

    for _, paths in example_categories():
        for path in paths:
            # ponytail: evaluator <= 1.49.6 writes profile_time()'s "µs" as cp1252
            # on Windows (no /utf-8 for MSVC), which fails to decode. Drop this
            # skip once BelfrySCAD requires the evaluator release that fixes it.
            if sys.platform == "win32" and path.name == "profiling.scad":
                continue
            messages = []
            tmp = scad_temp.write_temp_scad(path.read_text(encoding="utf-8"),
                                            near=example_resolve_path(path))
            try:
                Evaluator(echo_fn=messages.append).evaluate(tmp)
            finally:
                scad_temp.remove(tmp)
            assert not [m for m in messages if "couldn't be opened" in m], (path, messages)
