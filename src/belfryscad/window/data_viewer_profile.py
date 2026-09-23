"""ProfileViewer: per-call-site timings from Render with Profiling."""
from __future__ import annotations

import csv

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView, QMenu,
                               QLabel, QPushButton, QTabWidget, QWidget, QComboBox,
                               QLineEdit, QTreeWidget, QTreeWidgetItem, QFileDialog,
                               QMessageBox)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from belfryscad.window.data_viewer_common import _style_table_headers


# ---------------------------------------------------------------------------
# Profile Viewer
# ---------------------------------------------------------------------------

class _NumericTableWidgetItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by a numeric value instead of its
    displayed text -- setSortingEnabled's default comparison is
    lexical/string-based, which would sort "100.0" before "20.0". No
    sortable-column precedent exists elsewhere in this file; this is the
    minimal fix (one method), not a new dependency."""

    def __init__(self, value: float, text: str):
        super().__init__(text)
        self._value = value
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, _NumericTableWidgetItem):
            return self._value < other._value
        return super().__lt__(other)


class ProfileViewer(QDialog):
    """Sortable per-call-site profiling report from a "Render with
    Profiling" run (see Evaluator's profile=True instrumentation).

    Self time is the primary, always-correct metric (a frame's own code,
    excluding children -- disjoint wall-clock slices, never overlapping).
    Cumulative time is secondary and recursion-guarded: a call site that
    recurses through itself only counts its outermost invocation's
    elapsed time, since that already includes every nested invocation's
    time -- see CallSiteProfile's docstring. Double-click or right-click
    a row to jump to its call site or declaration in the editor."""

    navigate_requested = Signal(str, int)  # (file_path, line)

    _SELF_MS_COL = 4
    _SEARCH_COLS = (0, 1, 10)  # Name, Caller, Caller File

    def __init__(self, result: "ProfileResult", parent=None,
                 path_labels: "dict[str, str] | None" = None,
                 trim_prefix: str = ""):
        """`path_labels` maps a raw path the evaluator reported to a
        friendlier name -- used for the temp .scad each render writes the
        live editor buffer to, which is what call sites in an unsaved tab
        are attributed to. `trim_prefix` (the OpenSCAD library directory)
        is stripped from any path under it, so a BOSL2 call site reads
        `BOSL2/vnf.scad` rather than an absolute path nobody can scan."""
        super().__init__(parent)
        self._path_labels = dict(path_labels or {})
        self._trim_prefix = trim_prefix.rstrip("/") + "/" if trim_prefix else ""
        self.setWindowTitle("Profile Report")
        # Wide enough that the ten columns fit without horizontal scrolling
        # or hand-resizing on first open -- Name and Caller File are both
        # long, and the eight numeric columns after them add up.
        self.resize(1233, 500)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._result = result

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        resolve_ms = result.resolve_time * 1000
        unattributed_pct = 100 * result.unattributed_time / result.resolve_time if result.resolve_time > 0 else 0.0
        summary = QLabel(
            f"Total: {result.total_time * 1000:.1f} ms   "
            f"Resolve: {resolve_ms:.1f} ms   "
            f"Generate: {result.generate_time * 1000:.1f} ms   "
            f"Unattributed: {result.unattributed_time * 1000:.1f} ms ({unattributed_pct:.1f}% of resolve)"
        )
        layout.addWidget(summary)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)
        # The tree goes first and shows by default: the flat table is every
        # call site at once, which answers "what is slow" but not "slow
        # because of what" -- for that you need to see what called what.
        self._tabs.addTab(self._build_tree_tab(result), "Call Tree")

        flat_page = QWidget()
        flat_layout = QVBoxLayout(flat_page)
        flat_layout.setContentsMargins(0, 0, 0, 0)
        self._tabs.addTab(flat_page, "All Call Sites")

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self._search_mode = QComboBox()
        self._search_mode.addItem("Contains")
        self._search_mode.addItem("Starts With")
        self._search_mode.addItem("Exact Match")
        search_row.addWidget(self._search_mode)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search Name / Caller / Caller File…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_search_filter)
        search_row.addWidget(self._search, 1)
        flat_layout.addLayout(search_row)
        self._search_mode.currentIndexChanged.connect(self._apply_search_filter)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        # Caller File goes last and takes the spare width: it is the widest
        # and most variable column, so anywhere else it either gets clipped
        # or shoves the numbers off to the right. Line and Col sit right
        # before it -- together they are one location, read as a unit.
        cols = ["Name", "Caller", "Kind", "Calls",
                "Self (ms)", "Self %", "Total (ms)", "Total %",
                "Line", "Col", "Caller File"]
        self._table.setColumnCount(len(cols))
        self._table.setHorizontalHeaderLabels(cols)
        self._table.verticalHeader().setVisible(False)
        _style_table_headers(self._table)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        self._table.cellDoubleClicked.connect(self._goto_call_site)
        header = self._table.horizontalHeader()
        # Caller File is last and absorbs the window's spare width. Name and
        # Caller just start wide; they stay Interactive so they can be
        # drag-resized. (QTableView defaults stretchLastSection to False --
        # unlike QTreeView, which is why the tree needs no such call.)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 220)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 160)
        flat_layout.addWidget(self._table)

        self._populate(result)
        self._table.setSortingEnabled(True)
        self._table.sortItems(self._SELF_MS_COL, Qt.SortOrder.DescendingOrder)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self._export_csv)
        # A QPushButton in a QDialog is autoDefault, so Export would claim
        # the default the moment it took focus and Return would then start
        # writing a file. Dismiss is the one Return should hit.
        export.setAutoDefault(False)
        btn_row.addWidget(export)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        dismiss.setDefault(True)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

    # Rows come from self._result / self._paths, not from the widgets: the
    # tree attaches children lazily on expand, so walking QTreeWidgetItems
    # would export only whatever the user happened to have opened.
    def _csv_rows(self):
        """(header, rows) for whichever tab is showing."""
        resolve = self._result.resolve_time
        pct = lambda t: 100 * t / resolve if resolve > 0 else 0.0   # noqa: E731

        if self._tabs.currentIndex() != 0:
            header = ["name", "caller", "kind", "line", "column", "file",
                      "calls", "self_ms", "self_pct", "total_ms", "total_pct"]
            rows = [
                [s.name, s.caller_name, s.kind, s.call_line, s.call_column,
                 self._display_path(s.call_origin), s.call_count,
                 f"{s.self_time * 1000:.3f}", f"{pct(s.self_time):.2f}",
                 f"{s.cumulative_time * 1000:.3f}", f"{pct(s.cumulative_time):.2f}"]
                for s in self._result.call_sites
            ]
            return header, rows

        # Tree: depth plus the caller chain, so a row stays interpretable
        # once a spreadsheet sorts the nesting away.
        header = ["depth", "path", "name", "kind", "line", "column", "file",
                  "calls", "self_ms", "total_ms", "total_pct"]
        rows = []
        total = self._paths[0]["cumulative_time"] or 0.0
        root_name = self._paths[0].get("name") or "<toplevel>"
        stack = [(0, 0, root_name)]   # (index, depth, path-so-far)
        while stack:
            i, depth, chain = stack.pop()
            n = self._paths[i]
            if i == 0:
                rows.append([0, "", root_name, "", "", "", "", "",
                             f"{n['self_time'] * 1000:.3f}",
                             f"{n['cumulative_time'] * 1000:.3f}", "100.00"])
            else:
                cum = n["cumulative_time"]
                rows.append([depth, chain, n["name"], n["kind"], n["call_line"],
                             n["call_column"], self._display_path(n["call_origin"]),
                             n["call_count"], f"{n['self_time'] * 1000:.3f}",
                             f"{cum * 1000:.3f}",
                             f"{100 * cum / total if total > 0 else 0.0:.2f}"])
                chain = f"{chain}/{n['name']}"
            # Reversed: pop() is LIFO, so this emits children in the same
            # cost order the tree shows them in.
            for c in sorted(n["children"], key=lambda j: self._paths[j]["cumulative_time"]):
                stack.append((c, depth + 1, chain))
        return header, rows

    def _export_csv(self):
        which = "call_tree" if self._tabs.currentIndex() == 0 else "call_sites"
        path, _ = QFileDialog.getSaveFileName(self, "Export Profile CSV",
                                               f"profile_{which}.csv", "CSV Files (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        header, rows = self._csv_rows()
        try:
            # newline="" per the csv module's own docs -- without it the
            # writer's \r\n meets the text layer's translation and every
            # row ends up blank-line separated on Windows.
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
        except OSError as e:
            QMessageBox.warning(self, "Export Failed", f"Could not write {path}:\n{e}")

    def _populate(self, result: "ProfileResult"):
        resolve_time = result.resolve_time
        self._table.setRowCount(len(result.call_sites))
        for row, site in enumerate(result.call_sites):
            self_ms = site.self_time * 1000
            cum_ms = site.cumulative_time * 1000
            self_pct = 100 * site.self_time / resolve_time if resolve_time > 0 else 0.0
            cum_pct = 100 * site.cumulative_time / resolve_time if resolve_time > 0 else 0.0
            values = [
                QTableWidgetItem(site.name),
                QTableWidgetItem(site.caller_name),
                QTableWidgetItem(site.kind),
                _NumericTableWidgetItem(site.call_count, str(site.call_count)),
                _NumericTableWidgetItem(self_ms, f"{self_ms:.2f}"),
                _NumericTableWidgetItem(self_pct, f"{self_pct:.1f}"),
                _NumericTableWidgetItem(cum_ms, f"{cum_ms:.2f}"),
                _NumericTableWidgetItem(cum_pct, f"{cum_pct:.1f}"),
                _NumericTableWidgetItem(site.call_line, str(site.call_line)),
                _NumericTableWidgetItem(site.call_column, str(site.call_column)),
                QTableWidgetItem(self._display_path(site.call_origin)),
            ]
            for col, item in enumerate(values):
                if col in (0, 1, 2, 10):   # the text columns
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, col, item)
            # setSortingEnabled(True) reorders the table's *visual* rows
            # without touching self._entries, so a post-sort row index no
            # longer matches self._entries[row] -- stash the site directly
            # on the row's own item instead, so lookups stay correct no
            # matter how the user has sorted the table (a real bug: this
            # is the codebase's first sortable table, and the "index a
            # parallel list by row" pattern every other viewer here uses
            # only happens to work because none of their tables sort).
            self._table.item(row, 0).setData(Qt.ItemDataRole.UserRole, site)

    # -- Call tree ---------------------------------------------------------
    #
    # Built from ProfileResult.paths -- the evaluator's calling-context tree
    # (evaluator >= 0.19.0). Each node is one call site on ONE path from
    # <toplevel>, so a row's times are that path's own, not a total across
    # every caller of the name.
    #
    # This used to be reconstructed here from `caller_name` edges, which
    # made a graph rather than a tree: a callee appeared under every caller
    # with the same children, its nested times were totals across all of
    # them, and a cycle guard was needed to stop recursion unrolling
    # forever. None of that applies now -- the data is already a finite,
    # acyclic tree with recursion folded into single nodes.

    def _build_tree_tab(self, result: "ProfileResult") -> QWidget:
        page = QWidget()
        vbox = QVBoxLayout(page)
        vbox.setContentsMargins(0, 0, 0, 0)

        self._paths = list(getattr(result, "paths", None) or [])
        if not self._paths:
            vbox.addWidget(QLabel(
                "No call tree in this profile — it was produced by an "
                "evaluator older than 0.19.0. The flat view has the same "
                "data aggregated per call site."))
            return page

        self._tree = QTreeWidget()
        self._tree.setFont(QFont("Menlo", 11))
        self._tree.setColumnCount(9)
        # Cost first, identity after: the reason to open this tab is to find
        # where the time went, so Total % leads and the descriptive columns
        # (Kind, Line, Col, File) trail.
        self._tree.setHeaderLabels(
            ["Name", "Total %", "Total (ms)", "Self (ms)", "Calls", "Kind", "Line", "Col", "File"])
        self._tree.setUniformRowHeights(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._tree_context_menu)
        self._tree.itemDoubleClicked.connect(self._tree_goto)
        # Children attach on first expand: a modest BOSL2 model already
        # produces ~9000 nodes and most branches are never opened.
        self._tree.itemExpanded.connect(self._on_tree_expand)
        # File is last, so stretchLastSection (Qt's default) hands it the
        # spare width -- it is the widest and most variable column. Name just
        # starts wide, and stays draggable.
        self._tree.setColumnWidth(0, 300)
        vbox.addWidget(self._tree)

        root_node = self._paths[0]
        root = QTreeWidgetItem([root_node.get("name") or "<toplevel>", "100.0",
                                 f"{root_node['cumulative_time'] * 1000:.2f}", "", "", "", "", "", ""])
        for col in (1, 2, 3, 4, 6, 7):
            root.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root.setData(0, Qt.ItemDataRole.UserRole, 0)
        self._tree.addTopLevelItem(root)
        self._add_tree_children(root, 0)
        root.setExpanded(True)
        return page

    def _add_tree_children(self, parent_item: QTreeWidgetItem, index: int):
        """Attach one level of children of paths[index]."""
        total = self._paths[0]["cumulative_time"] or 0.0
        kids = sorted(self._paths[index]["children"],
                       key=lambda i: -self._paths[i]["cumulative_time"])
        for i in kids:
            n = self._paths[i]
            cum_ms = n["cumulative_time"] * 1000
            pct = 100 * n["cumulative_time"] / total if total > 0 else 0.0
            item = QTreeWidgetItem([
                n["name"],
                f"{pct:.1f}",
                f"{cum_ms:.2f}",
                f"{n['self_time'] * 1000:.2f}",
                str(n["call_count"]),
                n["kind"],
                str(n["call_line"]),
                str(n["call_column"]),
                self._display_path(n["call_origin"]),
            ])
            for col in (1, 2, 3, 4, 6, 7):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            parent_item.addChild(item)
            if n["children"]:
                item.addChild(QTreeWidgetItem(["\u2026"]))   # expand arrow; filled on open

    def _on_tree_expand(self, item: QTreeWidgetItem):
        if item.childCount() != 1 or item.child(0).text(0) != "\u2026":
            return   # already populated (or genuinely has one child)
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if index is None:
            return
        item.takeChildren()
        self._add_tree_children(item, index)

    def _tree_node(self, item: QTreeWidgetItem) -> "dict | None":
        index = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return self._paths[index] if index is not None and index < len(self._paths) else None

    def _tree_goto(self, item: QTreeWidgetItem, _col: int = 0):
        n = self._tree_node(item)
        if n and n.get("call_origin"):
            self.navigate_requested.emit(n["call_origin"], n["call_line"])

    def _tree_context_menu(self, pos):
        n = self._tree_node(self._tree.itemAt(pos))
        if not n or not n.get("call_origin"):
            return
        menu = QMenu(self._tree)
        menu.addAction("Go to Call Site",
                        lambda: self.navigate_requested.emit(n["call_origin"], n["call_line"]))
        if n.get("decl_origin"):
            menu.addAction(f"Go to Declaration of '{n['name']}'",
                            lambda: self.navigate_requested.emit(n["decl_origin"], n["decl_line"]))
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _display_path(self, origin: str) -> str:
        """How a call site's file is shown. Navigation still uses the raw
        `site.call_origin`, so shortening here never breaks jumping to it."""
        if not origin:
            return ""
        label = self._path_labels.get(origin)
        if label is not None:
            return label
        if self._trim_prefix and origin.startswith(self._trim_prefix):
            return origin[len(self._trim_prefix):]
        return origin

    def _site_at_row(self, row: int) -> "CallSiteProfile | None":
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _text_matches(self, cell: str, text: str) -> bool:
        mode = self._search_mode.currentText()
        if mode == "Exact Match":
            return cell == text
        if mode == "Starts With":
            return cell.startswith(text)
        return text in cell  # "Contains"

    def _apply_search_filter(self, _=None):
        text = self._search.text().strip().lower()
        for row in range(self._table.rowCount()):
            match = not text or any(
                (item := self._table.item(row, col)) is not None and self._text_matches(item.text().lower(), text)
                for col in self._SEARCH_COLS
            )
            self._table.setRowHidden(row, not match)

    def _goto_call_site(self, row: int, _col: int):
        site = self._site_at_row(row)
        if site is not None:
            self.navigate_requested.emit(site.call_origin, site.call_line)

    def _context_menu(self, pos):
        item = self._table.itemAt(pos)
        if item is None:
            return
        site = self._site_at_row(item.row())
        if site is None:
            return

        menu = QMenu(self)
        menu.addAction("Go to Call Site", lambda: self.navigate_requested.emit(site.call_origin, site.call_line))
        menu.addAction("Go to Declaration", lambda: self.navigate_requested.emit(site.decl_origin, site.decl_line))
        menu.addAction("Filter by Name", lambda: self._filter_by(site.name))
        menu.addAction("Filter by Caller", lambda: self._filter_by(site.caller_name))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _filter_by(self, text: str):
        """Used by the "Filter by Name"/"Filter by Caller" context menu
        actions -- an exact match, since the text comes from a real
        site's own name/caller_name rather than something the user is
        still typing."""
        self._search_mode.setCurrentText("Exact Match")
        self._search.setText(text)
