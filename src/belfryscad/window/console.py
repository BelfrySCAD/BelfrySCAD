from html import escape

from PySide6.QtWidgets import QTextBrowser
from PySide6.QtGui import QColor, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtCore import QUrl, Qt

from belfryscad.window.ui_colors import (console_severity_colors, on_appearance_change,
                                          text_color)


def _severity_of(text: str) -> str | None:
    """"error" / "warning" for a message the evaluator has already labelled.

    Scans EVERY line, not just the first, and reports the worst it finds.
    A failed render arrives as one multi-line block whose first line is the
    GUI's own summary -- "Eval error:  (5 ms)" -- with the ERROR: and TRACE:
    lines beneath it, so keying off the first line alone banded nothing at
    all, which is exactly what shipped in the first cut of this.

    The whole block takes the worst severity because it is one message, and
    because a collapsed block shows only its header: if the header were left
    unbanded, folding an error away would hide the fact that it was one.

    Matched on the prefixes the evaluator actually emits rather than by
    searching for the word anywhere, so a script echoing "warning" in its
    own output is not banded as one.
    """
    worst = None
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("ERROR:") or stripped.startswith("Eval error:"):
            return "error"
        if stripped.startswith("WARNING:"):
            worst = "warning"
    return worst


def _plain_fmt() -> QTextCharFormat:
    """Default format with no anchor href, and an explicit foreground.

    The colour is not decoration: a QTextDocument's default character
    format is black no matter what the palette says, so on a Mac in Dark
    Mode this text was black on a near-black base. Built per append rather
    than once at import, so it costs nothing to be right after the user
    switches appearance mid-session -- and so it does not need a
    QApplication to exist at import time.
    """
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(text_color()))
    return fmt


class ConsoleWidget(QTextBrowser):
    """Read-only console with collapsible multi-line output blocks.

    Multi-line text appended via append_output() gets a clickable ▼/▶
    anchor on the first line; clicking collapses or expands the remaining
    lines. QTextBrowser handles cursor shapes automatically: PointingHandCursor
    over the toggle anchor, IBeamCursor over selectable text.
    """

    _COLLAPSED = "▶"
    _EXPANDED = "▼"

    # block number -> "error" / "warning". Kept because _retheme has to
    # recolour these differently from ordinary text after a light/dark
    # switch, and because folding only hides blocks (setVisible) rather
    # than deleting them, so a block's number is stable for the life of
    # the document.

    def __init__(self, parent=None):
        super().__init__(parent)
        # fold_id → (header_bn, first_body_bn, last_body_bn)
        self._fold_headers: dict[int, tuple[int, int, int]] = {}
        self._folded: set[int] = set()
        # fold_id → (name, value) for blocks appended via append_value()
        self._fold_values: dict[int, tuple[str, object]] = {}
        self._severity: dict[int, str] = {}
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._on_anchor_clicked)
        on_appearance_change(self, self._retheme)

    def _band(self, first_bn: int, last_bn: int, kind: str):
        """Paint blocks first_bn..last_bn as an error or warning band.

        Note the matching reset at every insertBlock: a block format is
        inherited by the next block inserted after it, so without that, one
        banded line paints every line that follows it for the rest of the
        session. Caught by rendering the widget and looking at it -- the
        "Exported to ..." line after a traceback came out red.
        """
        bg, fg = console_severity_colors(kind)
        doc = self.document()
        for bn in range(first_bn, last_bn + 1):
            block = doc.findBlockByNumber(bn)
            if not block.isValid():
                continue
            self._severity[bn] = kind
            cursor = QTextCursor(block)
            # Background on the BLOCK, not the characters: a char-level
            # background stops at the end of the text, giving a ragged tag
            # rather than a band, and short lines would barely register.
            bf = cursor.blockFormat()
            bf.setBackground(QColor(bg))
            cursor.setBlockFormat(bf)
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor)
            cf = QTextCharFormat()
            cf.setForeground(QColor(fg))
            cursor.mergeCharFormat(cf)

    def _retheme(self):
        """Recolour text already in the document after a light/dark switch.

        New appends pick the colour up on their own; text written before
        the switch keeps whatever it was given, so it has to be rewritten.
        `mergeCharFormat` only touches the foreground, leaving the fold
        anchors' hrefs -- and so the fold toggles -- intact.
        """
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(text_color()))
        cursor.mergeCharFormat(fmt)
        # That blanket merge just flattened the severity foregrounds too, and
        # the band backgrounds are the wrong theme's now -- repaint them.
        for bn, kind in list(self._severity.items()):
            self._band(bn, bn, kind)

    def append_output(self, text: str):
        """Append text. Multi-line output gets a fold toggle on the first line."""
        lines = text.rstrip('\n').split('\n')
        if len(lines) <= 1:
            self._append_plain(text)
        else:
            self._append_foldable(lines[0], '\n'.join(lines[1:]))

    def append_value(self, name: str, value: object, text: str):
        """Like append_output but stores *value* so right-click can launch viewers."""
        lines = text.rstrip('\n').split('\n')
        if len(lines) <= 1:
            self._append_plain(text)
        else:
            fold_id = len(self._fold_headers)
            self._fold_values[fold_id] = (name, value)
            self._append_foldable(lines[0], '\n'.join(lines[1:]))

    def value_at(self, pos) -> tuple[str, object] | None:
        """Return (name, value) if *pos* is inside a foldable block with a stored value."""
        cursor = self.cursorForPosition(pos)
        bn = cursor.blockNumber()
        for fold_id, (header_bn, first_bn, last_bn) in self._fold_headers.items():
            if bn == header_bn or first_bn <= bn <= last_bn:
                return self._fold_values.get(fold_id)
        return None

    def _append_plain(self, text: str):
        doc = self.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if cursor.position() > 0:
            cursor.insertBlock(QTextBlockFormat())
        cursor.insertText(text, _plain_fmt())
        kind = _severity_of(text)
        if kind:
            bn = doc.blockCount() - 1
            self._band(bn, bn, kind)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _append_foldable(self, summary: str, detail: str):
        doc = self.document()
        fold_id = len(self._fold_headers)
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if cursor.position() > 0:
            cursor.insertBlock(QTextBlockFormat())
        # Colour set inline rather than through the document stylesheet:
        # `a { color: inherit }` inherits the same black default the plain
        # text had, which is the bug this is avoiding.
        cursor.insertHtml(
            f'<a href="fold:{fold_id}" style="color:{text_color()};'
            f'text-decoration:none">{self._EXPANDED} {escape(summary)}</a>'
        )
        header_bn = doc.blockCount() - 1
        first_body_bn = header_bn + 1
        for line in detail.split('\n'):
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertBlock(QTextBlockFormat())
            cursor.insertText(line, _plain_fmt())
        last_body_bn = doc.blockCount() - 1
        self._fold_headers[fold_id] = (header_bn, first_body_bn, last_body_bn)
        # Banded as one region: an ERROR and the TRACE lines under it are a
        # single message, and banding only the header would leave the trace
        # looking like unrelated output.
        kind = _severity_of(summary + "\n" + detail)
        if kind:
            self._band(header_bn, last_body_bn, kind)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

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
        self._fold_values.clear()
        self._severity.clear()
