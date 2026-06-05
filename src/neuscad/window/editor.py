from PySide6.QtWidgets import QPlainTextEdit, QWidget, QTextEdit
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont,
    QPainter, QTextFormat, QPainterPath,
)
from PySide6.QtCore import Qt, QRect, QSize, QRegularExpression, QPoint


def _compute_fold_regions(doc) -> dict[int, int]:
    """Scan the document for matching multi-line {…} pairs.
    Returns {open_block_number: close_block_number}."""
    regions: dict[int, int] = {}
    stack: list[int] = []
    block = doc.begin()
    while block.isValid():
        bn = block.blockNumber()
        text = block.text()
        ci = text.find("//")          # strip line comments (simple heuristic)
        if ci >= 0:
            text = text[:ci]
        for ch in text:
            if ch == "{":
                stack.append(bn)
            elif ch == "}" and stack:
                start = stack.pop()
                if start != bn:       # skip single-line {…}
                    regions[start] = bn
        block = block.next()
    return regions


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.line_number_area_paint_event(event)

    def mousePressEvent(self, event):
        fold_x = self.width() - 14
        if event.position().x() < fold_x:
            return
        y = int(event.position().y())
        ed = self._editor
        block = ed.firstVisibleBlock()
        top = round(ed.blockBoundingGeometry(block).translated(ed.contentOffset()).top())
        while block.isValid():
            if block.isVisible():
                h = round(ed.blockBoundingRect(block).height())
                if top <= y < top + h:
                    ed.toggle_fold(block.blockNumber())
                    return
                top += h
            block = block.next()


class OpenSCADHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self._rules = []

        keyword_format = QTextCharFormat()
        keyword_format.setForeground(QColor("#569CD6"))
        keyword_format.setFontWeight(QFont.Weight.Bold)
        keywords = [
            "module", "function", "if", "else", "for", "let",
            "each", "true", "false", "undef", "include", "use",
        ]
        for kw in keywords:
            self._rules.append((
                QRegularExpression(rf"\b{kw}\b"),
                keyword_format,
            ))

        builtin_format = QTextCharFormat()
        builtin_format.setForeground(QColor("#4EC9B0"))
        builtins = [
            "cube", "sphere", "cylinder", "cone", "polyhedron",
            "translate", "rotate", "scale", "mirror", "multmatrix",
            "color", "hull", "minkowski", "resize", "offset",
            "union", "difference", "intersection",
            "echo", "assert", "children",
        ]
        for b in builtins:
            self._rules.append((
                QRegularExpression(rf"\b{b}\b"),
                builtin_format,
            ))

        number_format = QTextCharFormat()
        number_format.setForeground(QColor("#5A9E4A"))
        self._rules.append((
            QRegularExpression(r"\b\d+\.?\d*\b"),
            number_format,
        ))

        string_format = QTextCharFormat()
        string_format.setForeground(QColor("#CE9178"))
        self._rules.append((
            QRegularExpression(r'"[^"]*"'),
            string_format,
        ))

        comment_format = QTextCharFormat()
        comment_format.setForeground(QColor("#6A9955"))
        self._rules.append((
            QRegularExpression(r"//[^\n]*"),
            comment_format,
        ))

        self._special_var_format = QTextCharFormat()
        self._special_var_format.setForeground(QColor("#C586C0"))
        self._rules.append((
            QRegularExpression(r"\$\w+"),
            self._special_var_format,
        ))

    def highlightBlock(self, text):
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_number_area = LineNumberArea(self)
        self._highlighter = OpenSCADHighlighter(self.document())

        self.setUndoRedoEnabled(False)

        font = QFont("Menlo", 13)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.setTabStopDistance(
            self.fontMetrics().horizontalAdvance(" ") * 4
        )

        self._error_selections: list = []
        self._selection_extra: list = []

        self._fold_regions: dict[int, int] = {}
        self._folded: set[int] = set()
        self._fold_dirty: bool = True
        self._fold_busy: bool = False

        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.document().contentsChanged.connect(self._on_doc_changed)
        self._update_line_number_area_width()

    def line_number_area_width(self):
        digits = max(1, len(str(self.blockCount())))
        return 6 + self.fontMetrics().horizontalAdvance("9") * digits + 14

    def _update_line_number_area_width(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect, dy):
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0, rect.y(), self._line_number_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def line_number_area_paint_event(self, event):
        if self._fold_dirty and not self._fold_busy:
            self._fold_busy = True
            self._recompute_fold_regions()
            self._fold_dirty = False
            self._fold_busy = False

        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), QColor("#CCCCCC"))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())

        lh = self.fontMetrics().height()
        num_w = self._line_number_area.width() - 16

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#000000"))
                painter.drawText(
                    0, top, num_w, lh,
                    Qt.AlignmentFlag.AlignRight,
                    str(block_number + 1),
                )

                if block_number in self._fold_regions:
                    mid_y = top + lh / 2
                    tx = float(num_w + 2)
                    tw, th = 8.0, 5.0
                    path = QPainterPath()
                    if block_number in self._folded:
                        path.moveTo(tx,        mid_y - th)
                        path.lineTo(tx + tw,   mid_y)
                        path.lineTo(tx,        mid_y + th)
                    else:
                        path.moveTo(tx,        mid_y - th + 2)
                        path.lineTo(tx + tw,   mid_y - th + 2)
                        path.lineTo(tx + tw/2, mid_y + th - 1)
                    path.closeSubpath()
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor("#555555"))
                    painter.drawPath(path)

            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    # ------------------------------------------------------------------
    # Code folding
    # ------------------------------------------------------------------

    def _on_doc_changed(self):
        self._fold_dirty = True
        self._line_number_area.update()

    def _recompute_fold_regions(self):
        new_regions = _compute_fold_regions(self.document())
        stale = self._folded - set(new_regions.keys())
        for bn in stale:
            self._set_range_visible(bn, self._fold_regions.get(bn), True)
            self._folded.discard(bn)
        self._fold_regions = new_regions

    def _set_range_visible(self, start_bn: int, end_bn: int | None, visible: bool):
        if end_bn is None:
            return
        block = self.document().findBlockByNumber(start_bn + 1)
        while block.isValid() and block.blockNumber() <= end_bn:
            block.setVisible(visible)
            block = block.next()
        self.document().markContentsDirty(0, self.document().characterCount())
        self.viewport().update()

    def toggle_fold(self, block_number: int):
        if self._fold_dirty:
            self._recompute_fold_regions()
            self._fold_dirty = False
        if block_number not in self._fold_regions:
            return
        end_bn = self._fold_regions[block_number]
        if block_number in self._folded:
            self._folded.discard(block_number)
            self._set_range_visible(block_number, end_bn, True)
        else:
            self._folded.add(block_number)
            self._set_range_visible(block_number, end_bn, False)
        self._update_line_number_area_width()
        self._line_number_area.update()

    def set_error_location(self, line, col):
        fmt = QTextCharFormat()
        fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        fmt.setUnderlineColor(QColor("#F44747"))
        block = self.document().findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        cursor_start = block.position() + max(0, col - 1)
        cursor_end = block.position() + block.length() - 1
        sel = QTextEdit.ExtraSelection()
        sel.format = fmt
        sel.cursor = self.textCursor()
        sel.cursor.setPosition(cursor_start)
        sel.cursor.setPosition(cursor_end, sel.cursor.MoveMode.KeepAnchor)
        self._error_selections = [sel]
        self._refresh_extra_selections()

    def clear_errors(self):
        self._error_selections = []
        self._refresh_extra_selections()

    def set_selection(self, start_offset: int, end_offset: int):
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#ADD6FF"))
        sel = QTextEdit.ExtraSelection()
        sel.format = fmt
        sel.cursor = self.textCursor()
        sel.cursor.setPosition(start_offset)
        sel.cursor.setPosition(end_offset, sel.cursor.MoveMode.KeepAnchor)
        self._selection_extra = [sel]
        self._refresh_extra_selections()
        # Scroll to the selected node
        c = self.textCursor()
        c.setPosition(start_offset)
        self.setTextCursor(c)
        self.ensureCursorVisible()

    def clear_selection(self):
        self._selection_extra = []
        self._refresh_extra_selections()

    def _refresh_extra_selections(self):
        self.setExtraSelections(self._error_selections + self._selection_extra)
