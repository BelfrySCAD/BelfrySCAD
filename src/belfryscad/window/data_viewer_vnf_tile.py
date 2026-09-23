"""VNFTileViewer: a VNFViewer for BOSL2 VNF tile textures -- tiling preview,
seam check, and edge vertices that move with their twins."""
from __future__ import annotations

import copy

import numpy as np

from PySide6.QtWidgets import QTableWidgetItem, QCheckBox, QLabel

from belfryscad import vnf_tile
from belfryscad.window.data_viewer_common import _parse_number
from belfryscad.window.data_viewer_vnf import VNFViewer, _is_vnf


def _is_vnf_tile(v) -> bool:
    """A VNF lying within the unit square in X and Y -- the shape of a
    BOSL2 VNF tile texture. Offered alongside "VNF", never instead of it:
    plenty of small meshes happen to fit."""
    return _is_vnf(v) and vnf_tile.is_tile(v[0])


# ---------------------------------------------------------------------------
# Path Viewer
# ---------------------------------------------------------------------------

class VNFTileViewer(VNFViewer):
    """A `VNFViewer` for a BOSL2 VNF tile texture -- a VNF within the unit
    square, repeated edge to edge (see `belfryscad.vnf_tile`).

    Adds what a lone mesh cannot show: the neighbouring copies ("Show
    tiling", drawn in a lighter grey and never picked), a live
    check of the seams BOSL2 asserts on, and edge vertices that drag
    with their twins on the opposite edge, so a seam that matched keeps
    matching."""

    def __init__(self, title: str, vnf_value: list, parent=None, editable: bool = False):
        self._tiled = False
        super().__init__(title, vnf_value, parent, editable)
        label = "VNF Tile Editor" if editable else "VNF Tile Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self._tile_cb = QCheckBox("Show tiling")
        self._tile_cb.setToolTip(
            "Draw the tile's eight neighbours as the texture would repeat,\n"
            "so the seams between copies are visible.")
        self._tile_cb.toggled.connect(self._on_tiling_toggled)
        self._btn_row.insertWidget(1, self._tile_cb)
        self._seam_label = QLabel("")
        self._btn_row.insertWidget(2, self._seam_label)
        self._update_seam_label()

    def _adjust_report(self, report):
        return vnf_tile.set_aside_rim_holes(report)

    def _on_tiling_toggled(self, on: bool):
        self._tiled = on
        self._rebuild(reframe=True)     # fit the 3x3 block, or the tile again

    def _load_mesh(self, reframe: bool = True):
        super()._load_mesh(reframe)
        if not self._tiled or not self._vp._renderer._buffers:
            return
        # The tile's own triangles, as VNFViewer uploaded them. Opaque rather
        # than the translucent `%` ghost pass, which turned the seams to mush.
        tile = self._vp._renderer._buffers[-1]
        positions = np.stack([tile.cpu_v0, tile.cpu_v1, tile.cpu_v2], axis=1).reshape(-1, 3)
        normals = np.repeat(np.cross(tile.cpu_v1 - tile.cpu_v0, tile.cpu_v2 - tile.cpu_v0), 3, axis=0)
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    # Empty tri_ids: ray_cast skips the buffer, so a click
                    # on a neighbour never selects the tile's own face.
                    self._vp._renderer.upload_mesh(
                        positions + np.array([dx, dy, 0], dtype=np.float32), normals,
                        color=(0.78, 0.78, 0.78, 1.0), tri_ids=np.zeros(0, dtype=np.int32))
        if reframe:
            verts = np.array(self._vnf[0], dtype=np.float32)
            self._vp.frame_scene(verts.min(axis=0) - [1, 1, 0], verts.max(axis=0) + [1, 1, 0])

    def _update_seam_label(self):
        out_of_range, unmatched = vnf_tile.tile_problems(self._vnf)
        problems = []
        if out_of_range:
            problems.append(f"{len(out_of_range)} outside the unit square")
        if unmatched:
            problems.append(f"{len(unmatched)} edge "
                            f"{'vertex' if len(unmatched) == 1 else 'vertices'} with no twin")
        if problems:
            self._seam_label.setText("Won't tile: " + ", ".join(problems))
            self._seam_label.setToolTip(
                "BOSL2 rejects this texture.\n"
                + (f"Outside [0,1]: {out_of_range}\n" if out_of_range else "")
                + (f"No twin on the opposite edge: {unmatched}" if unmatched else ""))
            self._seam_label.setStyleSheet("color: #b32020;")
        else:
            self._seam_label.setText("Tiles cleanly")
            self._seam_label.setToolTip("")
            self._seam_label.setStyleSheet("color: #1a7f37;")

    def _rebuild(self, reframe: bool = True):
        super()._rebuild(reframe)
        if hasattr(self, "_seam_label"):
            self._update_seam_label()

    def _on_item_changed(self, item: QTableWidgetItem):
        i, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            super()._on_item_changed(item)      # reverts the cell
            return
        new_pos = list(self._vnf[0][i])
        new_pos[j] = parsed
        new_value = copy.deepcopy(self._vnf)
        for k, p in vnf_tile.linked_moves(self._vnf[0], i, new_pos, lock=False):
            new_value[0][k] = p
        self._commit_value(new_value, "Edit Vertex")

    def _on_viewport_vertex_moved(self, vi: int, x: float, y: float, z: float):
        moves = vnf_tile.linked_moves(self._vnf[0], vi, [x, y, z], lock=True)
        self._vert_table.blockSignals(True)
        for k, p in moves:
            self._vnf[0][k] = p
            for c in range(3):
                self._vert_table.item(k, c).setText(f"{p[c]:g}")
        self._vert_table.blockSignals(False)
        self._rebuild(reframe=False)
        self._vp.scroll_to_visible(np.array(moves[0][1]))
