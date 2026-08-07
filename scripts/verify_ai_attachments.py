#!/usr/bin/env python3
"""Dropping a file into the chat input must send its content, not its path.

The bug this fixes: a dropped image inserted a file:// URL, which reads
like an attachment and is nothing of the sort -- the model cannot open it.

Images go as image blocks; text files are inlined, because no provider
takes arbitrary bytes but every one of them reads text.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import QMimeData, QUrl  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from belfryscad.window.ai_chat import AIChatPane, _ChatInput, _encode_image  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "sketch.png"
        QImage(40, 30, QImage.Format.Format_RGB32).save(str(png))
        big = Path(td) / "huge.png"
        QImage(4000, 3000, QImage.Format.Format_RGB32).save(str(big))
        scad = Path(td) / "bracket.scad"
        scad.write_text("cube([10,20,30]);\n")
        binary = Path(td) / "mesh.stl"
        binary.write_bytes(b"\x00\x01\x02solid nonsense\x00")

        def drop(paths):
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
            return md

        inp = _ChatInput(lambda: None)
        got = {"images": [], "texts": []}
        inp.images_added.connect(lambda v: got["images"].extend(v))
        inp.text_added.connect(lambda v: got["texts"].extend(v))

        # --- images ------------------------------------------------------
        check("an image file is harvested, not left as a URL",
              inp._take(drop([png])) and len(got["images"]) == 1, str(got["images"])[:80])
        if got["images"]:
            name, b64, mime = got["images"][0]
            check("the image keeps its filename", name == "sketch.png", name)
            check("the image is sent as base64 png", mime == "image/png" and len(b64) > 40)

        # --- oversized images are scaled --------------------------------
        enc = _encode_image(QImage(4000, 3000, QImage.Format.Format_RGB32))
        check("an oversized image is re-encoded", enc is not None)
        if enc:
            import base64
            out = QImage.fromData(base64.b64decode(enc[0]))
            check("it is scaled down rather than sent whole",
                  max(out.width(), out.height()) <= 1568, f"{out.width()}x{out.height()}")
            check("its aspect ratio survives",
                  abs(out.width() / out.height() - 4000 / 3000) < 0.01,
                  f"{out.width()}x{out.height()}")

        # --- text files ---------------------------------------------------
        got["images"].clear(); got["texts"].clear()
        check("a .scad file is harvested as text",
              inp._take(drop([scad])) and len(got["texts"]) == 1, str(got["texts"])[:60])
        if got["texts"]:
            name, lang, body = got["texts"][0]
            check("the text file keeps its name", name == "bracket.scad", name)
            check("it is labelled with a language for fencing", lang == "openscad", lang)
            check("its contents come through", "cube([10,20,30])" in body, body[:40])

        # --- files we cannot send ------------------------------------------
        got["images"].clear(); got["texts"].clear()
        check("a binary file is not harvested, so the default drop handles it",
              not inp._take(drop([binary])), str(got))

        # --- composing the message ----------------------------------------
        compose = AIChatPane._compose
        shown, sent, images = compose(
            "have a look",
            [("sketch.png", "image", ("BASE64DATA", "image/png")),
             ("bracket.scad", "text", ("openscad", "cube([10,20,30]);"))])
        check("the image is carried separately, not inlined",
              images == [("BASE64DATA", "image/png")], str(images))
        check("the text file is inlined into the message",
              "cube([10,20,30]);" in sent and "```openscad" in sent, sent[:80])
        check("the typed text is kept", sent.startswith("have a look"), sent[:30])
        check("the transcript summarises rather than repeating the file",
              "cube([10,20,30])" not in shown and "bracket.scad" in shown, shown)
        check("the transcript names the image too", "sketch.png" in shown, shown)

        shown, sent, images = compose("", [])
        check("nothing attached and nothing typed composes to nothing",
              shown == "" and sent == "" and images == [])

        # --- the pane's own bookkeeping --------------------------------------
        pane = AIChatPane()
        pane._on_images_added([("a.png", "B64", "image/png")])
        pane._on_text_added([("b.scad", "openscad", "cube(1);")])
        check("attachments show in the bar once added",
              len(pane._attachments) == 2 and not pane._attach_bar.isHidden())
        pane._drop_attachment(0)
        check("an attachment can be removed individually",
              len(pane._attachments) == 1 and pane._attachments[0][0] == "b.scad")
        taken = pane._take_attachments()
        check("sending takes the attachments", len(taken) == 1 and pane._attachments == [])
        check("the bar hides once empty", pane._attach_bar.isHidden())

        # --- both CLI transports accept images -------------------------------
        import inspect
        from belfryscad.window.ai_cli import ClaudeCliSession
        from belfryscad.window.ai_copilot_cli import CopilotCliSession
        for cls in (ClaudeCliSession, CopilotCliSession):
            check(f"{cls.__name__}.send_turn takes images",
                  "images" in inspect.signature(cls.send_turn).parameters)

        sess = CopilotCliSession("sys", "http://x/mcp")
        import base64
        paths = sess._write_attachments([(base64.b64encode(b"\x89PNG fake").decode(), "image/png")])
        check("copilot writes attachments to real files it can pass by path",
              len(paths) == 1 and Path(paths[0]).exists() and paths[0].endswith(".png"),
              str(paths))
        if paths:
            cmd = sess._command("hi", paths)
            check("and passes them with --attachment",
                  "--attachment" in cmd and paths[0] in cmd, " ".join(cmd[-4:]))
            Path(paths[0]).unlink(missing_ok=True)

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
