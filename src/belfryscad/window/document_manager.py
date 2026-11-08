from __future__ import annotations
from PySide6.QtCore import QObject
from belfryscad.qt_positions import qt_length
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from belfryscad.window.editor import CodeEditor

_instance: DocumentManager | None = None


def get_document_manager() -> DocumentManager:
    global _instance
    if _instance is None:
        _instance = DocumentManager()
    return _instance


class DocumentManager(QObject):
    """Singleton that keeps all open editors for the same file in sync across windows."""

    def __init__(self):
        super().__init__()
        self._docs: dict[str, list[CodeEditor]] = {}

    def register(self, file_path: str, editor: CodeEditor) -> None:
        if file_path not in self._docs:
            self._docs[file_path] = []
        if editor not in self._docs[file_path]:
            self._docs[file_path].append(editor)

    def unregister(self, file_path: str, editor: CodeEditor) -> None:
        if file_path in self._docs:
            self._docs[file_path] = [e for e in self._docs[file_path] if e is not editor]
            if not self._docs[file_path]:
                del self._docs[file_path]

    def broadcast_change(self, file_path: str, text: str, source_editor: CodeEditor) -> None:
        # blockSignals stops the editor's signals, not its document's, so the
        # receiver's own change handler broadcasts the same text back. Skipping
        # an editor that already has it ends that echo, which otherwise reset
        # the sender's text and put its cursor at 0 on every key (#1094).
        for editor in self._docs.get(file_path, []):
            if editor is source_editor or editor.toPlainText() == text:
                continue
            pos = editor.textCursor().position()
            editor.blockSignals(True)
            editor.setPlainText(text)
            editor.blockSignals(False)
            cursor = editor.textCursor()
            cursor.setPosition(min(pos, qt_length(text)))
            editor.setTextCursor(cursor)

    def open_elsewhere(self, file_path: str, editor: CodeEditor) -> bool:
        """Whether an editor other than `editor` has `file_path` open."""
        from pathlib import Path
        want = Path(file_path).resolve()
        return any(Path(p).resolve() == want and any(e is not editor for e in eds)
                   for p, eds in self._docs.items())

    def get_current_text(self, file_path: str) -> str | None:
        editors = self._docs.get(file_path, [])
        return editors[0].toPlainText() if editors else None
