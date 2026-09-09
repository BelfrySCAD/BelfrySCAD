"""Help ▸ Font List — every font `text()` can actually use.

The names a system font dialog shows are frequently not the names
OpenSCAD takes, which is what issue #379 asked this for: *"All too often I
specify an interesting font only to get the default Liberation Sans
instead."* So the list comes from the evaluator's own FreeType index, via
`openscad_cpp_evaluator.list_fonts()` — the same index `text()`,
`textmetrics()` and `fontmetrics()` match against. A list read from Qt
would reproduce the very mismatch it is meant to cure.

The third column is the string to paste: `Family` for a regular face,
`Family:style=Style` otherwise. Double-click a row (or press Copy) to put
it on the clipboard.

The fourth column is a sample of the face (issue #388): a pangram plus the
digits, painted by a delegate in the face itself. Nothing is pre-rendered
-- a row's font file is registered with Qt the first time that row is
painted, so opening the list costs nothing extra and scrolling only pays
for the rows that come into view.
"""
import zlib

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

#: Column order. The spec comes before the sample: it is what gets read
#: across from a family the user already spotted, and the sample is the
#: widest thing on the row.
_COLUMNS = ("Family", "Style", "font= spec", "Sample")
_SPEC_COL = 2
_SAMPLE_COL = 3
_SAMPLE_POINT_SIZE = 16

#: One of these per row, chosen by family so a face keeps its sentence
#: however the table is sorted or filtered. The digits follow every one.
PANGRAMS = (
    "Waltz, bad nymph, for quick jigs vex.",
    "Glib jocks quiz nymph to vex dwarf.",
    "Sphinx of black quartz, judge my vow!",
    "How quickly daft jumping zebras vex!",
    "The five boxing wizards jump quickly.",
    "Jackdaws love my big sphinx of quartz.",
    "Pack my box with five dozen liquor jugs.",
    "The quick brown fox jumps over the lazy dog.",
)


def sample_text_for(family: str) -> str:
    """The sample sentence for a family: a pangram, then the digits.

    Deterministic in the family name (crc32, not hash(): Python salts
    hash() per process, and a face that changed sentence between two
    openings of the list would look like a different font).
    """
    return PANGRAMS[zlib.crc32(family.encode("utf-8")) % len(PANGRAMS)] + " 0123456789"


class _SampleDelegate(QStyledItemDelegate):
    """Paints the row's sample text in the row's own face."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded: dict = {}   # font path -> Qt family, see qt_family_for

    def paint(self, painter, option, index):
        from belfryscad.window.font_picker import qt_font_for
        font = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(font, dict):
            super().paint(painter, option, index)
            return
        # Selection/alternate-row background as the style would draw it,
        # then our own text on top.
        opt = option
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(style.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        painter.setFont(qt_font_for(font, font["family"], font["style"], _SAMPLE_POINT_SIZE, self._loaded))
        role = (opt.palette.ColorRole.HighlightedText
                if opt.state & style.StateFlag.State_Selected else opt.palette.ColorRole.Text)
        painter.setPen(opt.palette.color(role))
        rect = opt.rect.adjusted(6, 0, -6, 0)
        text = painter.fontMetrics().elidedText(
            sample_text_for(font["family"]), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        painter.restore()

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(hint.height(), _SAMPLE_POINT_SIZE * 2))


def load_fonts() -> list:
    """Every resolvable face, as dicts of family/style/spec/path.

    Separated from the dialog so what is shown can be tested without a
    widget. Returns [] rather than raising if the evaluator is too old to
    have `list_fonts` — an out-of-date wheel should cost the Font List,
    not the whole Help menu.
    """
    try:
        from openscad_cpp_evaluator import list_fonts
    except ImportError:
        return []
    return list_fonts()


def matches(font: dict, needle: str) -> bool:
    """Case-insensitive substring over family and style.

    Matching the SPEC too would be redundant — it is built from those two
    — and would make every query match the family half twice.
    """
    if not needle:
        return True
    needle = needle.casefold()
    return needle in font["family"].casefold() or needle in font["style"].casefold()


class FontListDialog(QDialog):
    """Modeless, so a spec can be copied while the editor stays reachable."""

    def __init__(self, parent=None, fonts: list | None = None):
        super().__init__(parent)
        self.setWindowTitle("Font List")
        self.resize(980, 560)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        # Passed in only by tests; loading here keeps the caller a one-liner.
        self._fonts = load_fonts() if fonts is None else list(fonts)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Fonts <b>text()</b> can use. Double-click a row to copy its <b>font=</b> spec."))

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by family or style…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setSectionResizeMode(
            _SPEC_COL, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            _SAMPLE_COL, QHeaderView.ResizeMode.Stretch)
        self._table.setItemDelegateForColumn(_SAMPLE_COL, _SampleDelegate(self._table))
        self._table.itemDoubleClicked.connect(lambda _item: self._copy_selected())
        layout.addWidget(self._table)

        self._count = QLabel()
        row = QHBoxLayout()
        row.addWidget(self._count)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._copy_btn = buttons.addButton("Copy font= Spec", QDialogButtonBox.ButtonRole.ActionRole)
        self._copy_btn.clicked.connect(self._copy_selected)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._fill()

    # ------------------------------------------------------------------

    def _fill(self):
        # Sorting off while filling: a sorted table reorders rows as they
        # arrive, so the row index being written to stops being the row
        # just created.
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(self._fonts))
        for r, font in enumerate(self._fonts):
            for c, key in enumerate(("family", "style", "spec")):
                self._table.setItem(r, c, QTableWidgetItem(font[key]))
            # No text: the delegate paints it. The dict rides along so the
            # delegate can find the face after any sort or filter.
            sample = QTableWidgetItem("")
            sample.setData(Qt.ItemDataRole.UserRole, font)
            self._table.setItem(r, _SAMPLE_COL, sample)
        self._table.resizeRowsToContents()
        self._table.setSortingEnabled(True)
        self._apply_filter(self._search.text())

    def _apply_filter(self, needle: str):
        # Read the ROW, not self._fonts[r]: clicking a header sorts the
        # table, after which row r is no longer the r'th font and every
        # filter would hide the wrong lines.
        shown = 0
        for r in range(self._table.rowCount()):
            family = self._table.item(r, 0)
            style = self._table.item(r, 1)
            hit = matches({"family": family.text() if family else "",
                            "style": style.text() if style else ""}, needle)
            self._table.setRowHidden(r, not hit)
            shown += hit
        total = self._table.rowCount()
        self._count.setText(
            f"{total} fonts" if shown == total else f"{shown} of {total} fonts")

    def _copy_selected(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, _SPEC_COL)
        if item is not None:
            QApplication.clipboard().setText(item.text())


def show_font_list(parent=None):
    """Open the Font List. Modeless and self-deleting."""
    dlg = FontListDialog(parent)
    dlg.show()
    return dlg
