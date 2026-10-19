"""Configurable keyboard shortcuts (#821).

Every command in a main window's menus and toolbars, plus the debugger's
keys, is a *binding* with a stable id -- its menu path, "Edit > Comment" --
and the shortcut it was built with as its default. The user's changes are
stored as overrides, `{id: "Ctrl+Shift+E"}` (an empty string for none), in
the `shortcuts/overrides` preference, and `apply` sets each binding to its
override or its default. Bindings are collected after the menus are built,
so the dynamic lists (Open Recent, Examples, the Window menu's documents)
are never among them.

The standard shortcuts -- New, Open, Close, Save, Cut, Copy, Paste and the
like -- are fixed: they are what every application uses, so they are shown
but cannot be changed, and their keys cannot be given to another command.
"""
from __future__ import annotations

import json

import shiboken6
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu

PREF_KEY = "shortcuts/overrides"

#: Commands whose shortcuts are the platform's own, never reassigned.
LOCKED = {
    "File > New", "File > Open…", "File > Close", "File > Save", "File > Save As…",
    "File > Quit", "Edit > Undo", "Edit > Redo", "Edit > Cut", "Edit > Copy",
    "Edit > Paste", "Edit > Select All", "Edit > Find…", "Edit > Preferences…",
    "Window > Minimize", "Window > Show Next Tab", "Window > Show Previous Tab",
}


class Binding:
    """One configurable command: a QAction (any number of keys) or a
    QShortcut (one key), with the keys it was built with."""

    def __init__(self, ident: str, target):
        self.id = ident
        self.target = target
        self.default = [k.toString(QKeySequence.SequenceFormat.PortableText) for k in self.keys()]

    @property
    def locked(self) -> bool:
        return self.id in LOCKED

    def keys(self) -> list[QKeySequence]:
        if isinstance(self.target, QAction):
            return list(self.target.shortcuts())
        k = self.target.key()
        return [] if k.isEmpty() else [k]

    def set(self, portable: list[str]):
        seqs = [QKeySequence(k) for k in portable if k]
        if isinstance(self.target, QAction):
            self.target.setShortcuts(seqs)
            # A tooltip naming its key ("Debug (Shift+F6)") names the new one.
            tip = self.target.toolTip()
            base = self.target.property("tooltipBase")
            if base is None and self.default and tip.endswith(f" ({self.default[0]})"):
                base = tip[:-len(f" ({self.default[0]})")]
                self.target.setProperty("tooltipBase", base)
            if base is not None:
                shown = seqs[0].toString(QKeySequence.SequenceFormat.NativeText) if seqs else ""
                self.target.setToolTip(f"{base} ({shown})" if shown else base)
        else:
            self.target.setKey(seqs[0] if seqs else QKeySequence())


def _label(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def collect(menubar, toolbars=(), extra=(), skip_menus=()) -> list[Binding]:
    """The bindings of a window: every action in `menubar`'s menus (by menu
    path, skipping `skip_menus`), every toolbar action no menu holds, and
    `extra`, `(id, QShortcut)` pairs. In menu order, then toolbars, then extra."""
    out, seen = [], set()
    # Submenus found from the menus' side, never through QAction.menu(): in
    # PySide6 that hands back a wrapper that deletes the QMenu when it is
    # collected, which took Open Recent and Design's submenus with it.
    ptr = lambda o: shiboken6.getCppPointer(o)[0]
    menus = {ptr(m.menuAction()): m for m in menubar.findChildren(QMenu)}
    skip = {ptr(m) for m in skip_menus}

    def walk(menu, path):
        for act in menu.actions():
            if act.isSeparator() or not act.text():
                continue
            sub = menus.get(ptr(act))
            if sub is not None:
                if ptr(sub) not in skip:
                    walk(sub, path + [_label(act.text())])
                continue
            if id(act) in seen:
                continue
            seen.add(id(act))
            out.append(Binding(" > ".join(path + [_label(act.text())]), act))

    for act in menubar.actions():
        sub = menus.get(ptr(act))
        if sub is not None and ptr(sub) not in skip:
            walk(sub, [_label(act.text())])
    for bar in toolbars:
        for act in bar.actions():
            # Only those with a key of their own (Debug, Animate): the
            # rest repeat a menu command, which is listed under its menu.
            if act.isSeparator() or not act.text() or id(act) in seen \
                    or ptr(act) in menus or not act.shortcuts():
                continue
            seen.add(id(act))
            out.append(Binding(f"Toolbar > {_label(act.text())}", act))
    for ident, sc in extra:
        out.append(Binding(ident, sc))
    return out


def load_overrides(settings) -> dict:
    try:
        value = json.loads(settings.value(PREF_KEY, "{}") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_overrides(settings, overrides: dict):
    settings.setValue(PREF_KEY, json.dumps(overrides, sort_keys=True))


def effective(binding: Binding, overrides: dict) -> list[str]:
    """The keys `binding` has with `overrides`: its override, else its default."""
    if binding.locked or binding.id not in overrides:
        return binding.default
    return [overrides[binding.id]] if overrides[binding.id] else []


def apply(bindings: list[Binding], overrides: dict):
    for b in bindings:
        b.set(effective(b, overrides))


def owner_of(key: str, bindings: list[Binding], overrides: dict, besides: str = "") -> Binding | None:
    """The binding other than `besides` that `key` triggers now, if any."""
    seq = QKeySequence(key)
    for b in bindings:
        if b.id != besides and any(QKeySequence(k) == seq for k in effective(b, overrides)):
            return b
    return None
