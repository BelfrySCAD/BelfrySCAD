"""Use Library offers libraries installed by hand, not just catalogued ones (#887)."""
from belfryscad.window.library_manager import _uncatalogued_libraries


def test_hand_installed_library_is_listed(tmp_path):
    (tmp_path / "BOSL2").mkdir()
    (tmp_path / "BOSL2" / "std.scad").write_text("")
    lib = tmp_path / "MyLib"
    for sub in ("", "parts", "tests", ".git"):
        (lib / sub).mkdir(parents=True, exist_ok=True)
    (lib / "util.scad").write_text("// Small helpers.\n// Two lines.\nmodule a() {}\n")
    (lib / "MyLib.scad").write_text("module b() {}\n")
    (lib / "parts" / "gear.scad").write_text("")
    (lib / "tests" / "test_util.scad").write_text("")
    (lib / ".git" / "x.scad").write_text("")
    (tmp_path / "Empty").mkdir()

    libs = _uncatalogued_libraries(tmp_path, [{"name": "BOSL2", "install_as": "BOSL2"}])

    assert [l["name"] for l in libs] == ["MyLib"]
    rows = libs[0]["includes"]
    assert [r["statement"] for r in rows] == [
        "include <MyLib/MyLib.scad>", "include <MyLib/util.scad>",
        "include <MyLib/parts/gear.scad>"]
    assert rows[0].get("primary") and not rows[1].get("primary")
    assert rows[1]["description"] == "Small helpers. Two lines."


def test_missing_library_folder(tmp_path):
    assert _uncatalogued_libraries(tmp_path / "nope", []) == []
