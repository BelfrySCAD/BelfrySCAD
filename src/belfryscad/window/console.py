from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import Qt


class ConsoleWidget(QPlainTextEdit):
    """Read-only console with collapsible multi-line output blocks.

    Multi-line text appended via append_output() gets a ▼/▶ toggle on the
    first line; clicking the arrow hides or shows the remaining lines.
    Single-line text is appended as plain paragraphs.
    """

    _COLLAPSED = "▶"
    _EXPANDED = "▼"

    def __init__(self, parent=None):
        super().__init__(parent)
        # header block number → (first_body_bn, last_body_bn)
        self._fold_headers: dict[int, tuple[int, int]] = {}
        self._folded: set[int] = set()
        self.setUndoRedoEnabled(False)

    def append_output(self, text: str):
        """Append text. Multi-line output gets a fold toggle on the first line."""
        lines = text.rstrip('\n').split('\n')
        if len(lines) <= 1:
            self.appendPlainText(text)
        else:
            self._append_foldable(lines[0], '\n'.join(lines[1:]))

    def _append_foldable(self, summary: str, detail: str):
        doc = self.document()
        self.appendPlainText(f"{self._EXPANDED} {summary}")
        header_bn = doc.blockCount() - 1
        first_body_bn = header_bn + 1
        for line in detail.split('\n'):
            self.appendPlainText(line)
        last_body_bn = doc.blockCount() - 1
        self._fold_headers[header_bn] = (first_body_bn, last_body_bn)

    def _toggle_fold(self, header_bn: int):
        if header_bn not in self._fold_headers:
            return
        first_body_bn, last_body_bn = self._fold_headers[header_bn]
        doc = self.document()
        collapsing = header_bn not in self._folded

        if collapsing:
            self._folded.add(header_bn)
            new_arrow = self._COLLAPSED
        else:
            self._folded.discard(header_bn)
            new_arrow = self._EXPANDED

        # Swap the arrow character on the header line
        hb = doc.findBlockByNumber(header_bn)
        hcursor = QTextCursor(hb)
        hcursor.movePosition(QTextCursor.MoveOperation.NextCharacter,
                             QTextCursor.MoveMode.KeepAnchor)
        hcursor.insertText(new_arrow)

        # Show or hide body blocks
        block = doc.findBlockByNumber(first_body_bn)
        while block.isValid() and block.blockNumber() <= last_body_bn:
            block.setVisible(not collapsing)
            block = block.next()

        # Force the document layout to recalculate (same trick as CodeEditor)
        tmp = QTextCursor(doc)
        tmp.beginEditBlock()
        tmp.endEditBlock()
        self.viewport().update()

    def _header_at(self, pos) -> int | None:
        """Return the header block number if pos is over a fold arrow, else None."""
        cursor = self.cursorForPosition(pos)
        bn = cursor.blockNumber()
        if bn not in self._fold_headers:
            return None
        block = self.document().findBlockByNumber(bn)
        if cursor.position() - block.position() <= 1:
            return bn
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            header_bn = self._header_at(event.pos())
            if header_bn is not None:
                self._toggle_fold(header_bn)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self._header_at(event.pos()) is not None:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)

    def clear(self):
        super().clear()
        self._fold_headers.clear()
        self._folded.clear()
