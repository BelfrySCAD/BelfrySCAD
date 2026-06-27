from __future__ import annotations
from PySide6.QtCore import QObject
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
        for editor in self._docs.get(file_path, []):
            if editor is source_editor:
                continue
            editor.blockSignals(True)
            editor.setPlainText(text)
            editor.blockSignals(False)

    def get_current_text(self, file_path: str) -> str | None:
        editors = self._docs.get(file_path, [])
        return editors[0].toPlainText() if editors else None
