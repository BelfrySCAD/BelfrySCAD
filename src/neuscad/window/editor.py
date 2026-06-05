from PySide6.QtWidgets import QPlainTextEdit, QWidget, QTextEdit
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont,
    QPainter, QTextFormat
)
from PySide6.QtCore import Qt, QRect, QSize, QRegularExpression


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.line_number_area_paint_event(event)


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

        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_area_width()

    def line_number_area_width(self):
        digits = max(1, len(str(self.blockCount())))
        return 6 + self.fontMetrics().horizontalAdvance("9") * digits

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
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), QColor("#CCCCCC"))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())

        number_format = QTextCharFormat()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#000000"))
                painter.drawText(
                    0, top,
                    self._line_number_area.width() - 3,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(block_number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    def set_error_location(self, line, col):
        """Highlight a parse error with a squiggly underline."""
        fmt = QTextCharFormat()
        fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        fmt.setUnderlineColor(QColor("#F44747"))

        block = self.document().findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        cursor_start = block.position() + max(0, col - 1)
        cursor_end = block.position() + block.length() - 1

        selection = QTextEdit.ExtraSelection()
        selection.format = fmt
        selection.cursor = self.textCursor()
        selection.cursor.setPosition(cursor_start)
        selection.cursor.setPosition(cursor_end, selection.cursor.MoveMode.KeepAnchor)
        self.setExtraSelections([selection])

    def clear_errors(self):
        self.setExtraSelections([])
