from html import escape

from PySide6.QtWidgets import QTextBrowser
from PySide6.QtGui import QTextCharFormat, QTextCursor
from PySide6.QtCore import QUrl, Qt

_PLAIN_FMT = QTextCharFormat()  # default format with no anchor href


class ConsoleWidget(QTextBrowser):
    """Read-only console with collapsible multi-line output blocks.

    Multi-line text appended via append_output() gets a clickable ▼/▶
    anchor on the first line; clicking collapses or expands the remaining
    lines. QTextBrowser handles cursor shapes automatically: PointingHandCursor
    over the toggle anchor, IBeamCursor over selectable text.
    """

    _COLLAPSED = "▶"
    _EXPANDED = "▼"

    def __init__(self, parent=None):
        super().__init__(parent)
        # fold_id → (header_bn, first_body_bn, last_body_bn)
        self._fold_headers: dict[int, tuple[int, int, int]] = {}
        self._folded: set[int] = set()
        self.setOpenLinks(False)
        self.document().setDefaultStyleSheet(
            "a { color: inherit; text-decoration: none; }"
        )
        self.anchorClicked.connect(self._on_anchor_clicked)

    def append_output(self, text: str):
        """Append text. Multi-line output gets a fold toggle on the first line."""
        lines = text.rstrip('\n').split('\n')
        if len(lines) <= 1:
            self._append_plain(text)
        else:
            self._append_foldable(lines[0], '\n'.join(lines[1:]))

    def _append_plain(self, text: str):
        doc = self.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if cursor.position() > 0:
            cursor.insertBlock()
        cursor.insertText(text, _PLAIN_FMT)

    def _append_foldable(self, summary: str, detail: str):
        doc = self.document()
        fold_id = len(self._fold_headers)
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if cursor.position() > 0:
            cursor.insertBlock()
        cursor.insertHtml(
            f'<a href="fold:{fold_id}">{self._EXPANDED} {escape(summary)}</a>'
        )
        header_bn = doc.blockCount() - 1
        first_body_bn = header_bn + 1
        for line in detail.split('\n'):
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertBlock()
            cursor.insertText(line, _PLAIN_FMT)
        last_body_bn = doc.blockCount() - 1
        self._fold_headers[fold_id] = (header_bn, first_body_bn, last_body_bn)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self.anchorAt(event.pos()):
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)

    def _on_anchor_clicked(self, url: QUrl):
        href = url.toString()
        if href.startswith('fold:'):
            try:
                self._toggle_fold(int(href[5:]))
            except ValueError:
                pass

    def _toggle_fold(self, fold_id: int):
        if fold_id not in self._fold_headers:
            return
        header_bn, first_body_bn, last_body_bn = self._fold_headers[fold_id]
        doc = self.document()
        collapsing = fold_id not in self._folded
        if collapsing:
            self._folded.add(fold_id)
            new_arrow = self._COLLAPSED
        else:
            self._folded.discard(fold_id)
            new_arrow = self._EXPANDED

        # Set visibility BEFORE the arrow update so that the document-change
        # triggered by insertText causes QTextDocumentLayout to recalculate
        # with the correct block visibility already in place.
        block = doc.findBlockByNumber(first_body_bn)
        while block.isValid() and block.blockNumber() <= last_body_bn:
            block.setVisible(not collapsing)
            block = block.next()

        # Swap the arrow character (triggers documentChanged → layout recalc)
        hb = doc.findBlockByNumber(header_bn)
        hcursor = QTextCursor(hb)
        hcursor.movePosition(QTextCursor.MoveOperation.NextCharacter,
                             QTextCursor.MoveMode.KeepAnchor)
        hcursor.insertText(new_arrow)
        self.viewport().update()

    def clear(self):
        super().clear()
        self._fold_headers.clear()
        self._folded.clear()
