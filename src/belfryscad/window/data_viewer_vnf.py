"""VNFViewer: a 3D mesh viewer and editor for [vertices, faces] structures."""
from __future__ import annotations

import copy

import numpy as np

from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout,
                               QTableWidget, QTableWidgetItem, QAbstractItemView,
                               QCheckBox, QMenu, QLabel, QPushButton, QSplitter,
                               QTabWidget, QWidget)
from PySide6.QtCore import Qt, QPoint, Signal, QTimer, QItemSelectionModel
from PySide6.QtGui import QFont, QMouseEvent

from belfryscad.window.viewport import (Viewport, _key_nudge_delta,
                                        _key_nudge_magnitude, _view_locked_axis)
from belfryscad.window.data_viewer_common import (_UndoableViewerMixin,
                                                  _apply_click_selection,
                                                  _dodecahedron_faces, _format_value,
                                                  _is_list, _is_numeric_point,
                                                  _lit_marker_field,
                                                  _lit_marker_triangles,
                                                  _marker_radii_for_points,
                                                  _marker_radius_for_point,
                                                  _parse_number, _ray_plane_axis_locked,
                                                  _style_table_headers,
                                                  _sync_viewport_to_main_window,
                                                  _unlocked_plane_name,
                                                  _vertex_click_mode)


def _is_vnf(v) -> bool:
    if not (_is_list(v) and len(v) == 2):
        return False
    verts, faces = v[0], v[1]
    if not (_is_list(verts) and len(verts) >= 3
            and _is_list(faces) and len(faces) >= 1):
        return False
    if not all(_is_numeric_point(p) and len(p) == 3 for p in verts):
        return False
    return all(_is_list(f) and len(f) >= 3
               and all(isinstance(i, (int, float)) for i in f) for f in faces)


def _tube_triangles(p0, p1, r0: float, r1: float, color: np.ndarray,
                     sides: int = 6) -> list:
    """Build one capped low-poly tube from `p0` to `p1` as marker rows.

    Used instead of `GL_LINES` for the validation overlay: line width is a
    no-op in a core profile on macOS, so a "thick" line is a hairline and
    a mark on a mesh edge is unreadable. Real faces have a real thickness
    at any width, and go through the same phong-lit `_marker_prog` rows
    (`[x,y,z, nx,ny,nz, r,g,b]`) the vertex markers already use.

    A radius per end, not one for the whole tube: apparent size in
    perspective depends on each point's own distance from the eye, so a
    tube sized from its midpoint is too thin at the near end and too fat
    at the far one -- the same reason `_marker_radius_for_point` is called
    per vertex rather than once per mesh. An edge running away from the
    camera is exactly where a single radius shows.

    Six sides: at a few pixels across, more is invisible and each extra
    side costs two triangles on every edge of a possibly large report."""
    p0 = np.asarray(p0, dtype=np.float32)
    p1 = np.asarray(p1, dtype=np.float32)
    axis = p1 - p0
    length = float(np.linalg.norm(axis))
    if length < 1e-9:
        return []
    axis = axis / length
    # Any vector not parallel to the axis will do for the first radial.
    ref = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    if abs(float(np.dot(ref, axis))) > 0.9:
        ref = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    u = np.cross(axis, ref)
    u /= np.linalg.norm(u)
    v = np.cross(axis, u)

    ring = [(u * np.cos(t) + v * np.sin(t)).astype(np.float32)
            for t in np.linspace(0.0, 2.0 * np.pi, sides, endpoint=False)]
    a = [p0 + d * r0 for d in ring]
    b = [p1 + d * r1 for d in ring]

    rows = []

    def tri(q0, q1, q2):
        # Faceted, not smoothed: the flat shading is what makes a tube
        # this thin read as solid rather than as a soft smear. Taken from
        # the winding rather than from the radial direction, which is only
        # the true normal when the two ends share a radius.
        n = np.cross(q1 - q0, q2 - q0)
        ln = np.linalg.norm(n)
        if ln > 0:
            n = (n / ln).astype(np.float32)
        for q in (q0, q1, q2):
            rows.append(np.concatenate([q, n, color]))

    for k in range(sides):
        k2 = (k + 1) % sides
        # Wound CCW seen from outside -- the overlay is drawn with back
        # faces culled, so a wrong winding here does not merely light the
        # tube oddly, it drops the side facing the camera.
        tri(a[k], b[k2], b[k])
        tri(a[k], a[k2], b[k2])
    # Caps, so an end seen down the axis is not a hole.
    for k in range(1, sides - 1):
        tri(a[0], a[k + 1], a[k])
        tri(b[0], b[k], b[k + 1])
    return rows


# ---------------------------------------------------------------------------
# VNF Viewport (adds face picking and highlight)
# ---------------------------------------------------------------------------

class _VNFViewport(Viewport):
    face_clicked = Signal(int)
    vertex_clicked = Signal(int, str)  # (vertex index, click mode: "replace"/"add"/"toggle" -- see _vertex_click_mode) -- takes priority over face_clicked
    vertex_moved = Signal(int, float, float, float)  # (index, new_x, new_y, new_z) -- Cmd+drag, editable only
    # Bracket a continuous vertex_moved sequence (a Cmd+drag gesture, or one
    # keyboard nudge) so the dialog can push exactly one undo step for the
    # whole gesture instead of one per live frame.
    vertex_drag_started = Signal()
    vertex_drag_finished = Signal()
    # Right-click-in-viewport face/vertex topology edits (editable only) --
    # the viewport just picks and emits; the dialog owns the actual
    # mutation, same convention as every other signal above.
    duplicate_vertex_requested = Signal(int)
    delete_face_requested = Signal(int)
    reverse_face_requested = Signal(int)

    def __init__(self, parent=None, editable: bool = False):
        super().__init__(parent, selectable=False, pan_speed=2.0)
        self._renderer.camera.fov = 45.0
        self._renderer.depth_test_points = True
        self._editable = editable
        self._cpu_positions: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._tri_to_face: np.ndarray = np.zeros(0, dtype=np.int32)
        self._highlight_vao = None
        self._highlight_vbo = None
        self._vert_marker_vao_r = None
        self._vert_marker_vbo_r = None
        self._vert_marker_vao_w = None
        self._vert_marker_vbo_w = None
        self._vert_blink_red = True
        self._vert_indices: list[int] = []
        # (p0, p1, rgb) per validation mark; kept so the tubes can be
        # rebuilt at a new radius when the camera distance changes.
        self._validation_segs: list = []
        self._validation_vao = None
        self._validation_vbo = None
        # Opt-in here, unlike the other viewers. Markers cost roughly
        # 0.7ms per vertex to build and are rebuilt on zoom, and a mesh
        # carries far more vertices than a path or grid -- see
        # set_show_unselected for the measurements.
        self._show_unselected = False
        self._vert_blink_timer = QTimer(self)
        self._vert_blink_timer.setInterval(250)
        self._vert_blink_timer.timeout.connect(self._blink_tick)
        self._verts_3d: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._selected_face: int = -1
        self._drag_started = False
        self._press_pos: QPoint | None = None
        self._drag_vertex_idx = -1
        self._drag_lock_axis = 2
        self._drag_plane_point: np.ndarray = np.zeros(3, dtype=np.float32)

    def set_face_data(self, cpu_positions: np.ndarray, tri_to_face: np.ndarray,
                      verts_3d: np.ndarray | None = None):
        self._cpu_positions = cpu_positions
        self._tri_to_face = tri_to_face
        if verts_3d is not None:
            self._verts_3d = verts_3d

    def highlight_face(self, face_idx: int):
        if face_idx == self._selected_face:
            return
        self.makeCurrent()
        self._selected_face = face_idx
        self._rebuild_highlight()
        if face_idx >= 0 and len(self._cpu_positions) > 0:
            mask = self._tri_to_face == face_idx
            tri_indices = np.where(mask)[0]
            if len(tri_indices) > 0:
                verts = np.array([
                    self._cpu_positions[ti * 3 + k]
                    for ti in tri_indices for k in range(3)
                ], dtype=np.float32)
                self.scroll_to_visible(verts.mean(axis=0))
        self.doneCurrent()
        self.update()

    def _rebuild_highlight(self):
        if self._highlight_vao is not None:
            self._highlight_vao.release()
            self._highlight_vbo.release()
            self._highlight_vao = None
            self._highlight_vbo = None

        if self._selected_face < 0 or self._ctx is None:
            return

        mask = self._tri_to_face == self._selected_face
        tri_indices = np.where(mask)[0]
        if len(tri_indices) == 0:
            return

        positions = []
        normals = []
        for ti in tri_indices:
            v0 = self._cpu_positions[ti * 3]
            v1 = self._cpu_positions[ti * 3 + 1]
            v2 = self._cpu_positions[ti * 3 + 2]
            n = np.cross(v1 - v0, v2 - v0)
            ln = np.linalg.norm(n)
            if ln > 0:
                n /= ln
            positions.extend([v0, v1, v2])
            normals.extend([n, n, n])

        pos_arr = np.array(positions, dtype=np.float32)
        norm_arr = np.array(normals, dtype=np.float32)
        interleaved = np.concatenate([pos_arr, norm_arr], axis=1)
        self._highlight_vbo = self._ctx.buffer(interleaved.tobytes())
        self._highlight_vao = self._ctx.vertex_array(
            self._renderer._mesh_prog, [(self._highlight_vbo, "3f 3f", "in_position", "in_normal")],
        )

    def _blink_tick(self):
        self._vert_blink_red = not self._vert_blink_red
        self.update()

    def highlight_vertices(self, indices: list[int]):
        self.makeCurrent()
        self._release_vert_markers()
        self._vert_indices = []

        if not indices or self._ctx is None or len(self._verts_3d) == 0:
            self._vert_blink_timer.stop()
            self._build_unselected_markers()  # selection just cleared -- refresh which vertices count as "unselected"
            self.doneCurrent()
            self.update()
            return

        valid_indices = [vi for vi in indices if 0 <= vi < len(self._verts_3d)]
        self._vert_indices = valid_indices
        if valid_indices:
            self.scroll_to_visible(self._verts_3d[valid_indices[0]])
        self._vert_blink_red = True
        self._vert_blink_timer.start()
        self._build_vert_markers()
        self._build_unselected_markers()
        self.doneCurrent()
        self.update()

    # Validation overlay colours. Each condition gets its own so a mesh
    # failing several ways can still be read apart at a glance.
    # None of them may sit near magenta: the viewport paints backfaces a
    # hardcoded magenta as its inverted-normal cue (SceneRenderer's
    # `buf.backface_color or (1, 0, 1, 1)`), so on a single-sided sheet
    # -- seen from behind, which is most of the meshes worth validating
    # -- that magenta IS the surface a mark has to stand out against.
    # The first palette put a magenta and a purple on it. The second put
    # a cyan a hair from the Cyan/Nature themes' own object colour (a
    # distance of 0.03 in RGB -- indistinguishable) and a white on the
    # two near-white themes, because it was only ever checked against
    # the default theme.
    #
    # So these clear *every* theme's object colour, not just Cornfield's,
    # which is a real constraint: all thirteen are light, so the palette
    # is forced dark or fully saturated. Hues are kept 45 degrees apart
    # as well, since RGB distance alone will happily call two greens
    # distinct. See tests/test_data_viewers.py's TestValidationColors.
    BACKFACE_MAGENTA = (1.0, 0.0, 1.0)
    VALIDATION_COLORS = {
        "hole": (1.0, 0.0, 0.35),           # crimson -- open boundary
        "flipped": (1.0, 0.45, 0.0),        # orange -- wound backwards
        "nonmanifold": (0.6, 0.15, 0.85),   # violet -- 3+ faces on an edge
        "t_joint": (0.0, 1.0, 0.0),         # green -- unwelded crack
        "intersecting": (0.0, 0.15, 0.85),  # blue -- faces through faces
        "overlapping": (0.0, 0.0, 0.0),     # black -- coplanar double skin
    }

    def show_validation(self, report, faces):
        """Draw a VNFReport over the mesh.

        Edges are drawn as tubes and faces as their tubed outlines -- one
        mechanism for everything, rather than a second translucent mesh
        pass whose depth interaction would have to be tuned separately.
        """
        self.clear_validation()
        if report is None or report.welded_points is None:
            return
        pts = report.welded_points
        if len(pts) == 0:
            return
        remap = report.remap
        segs = []

        def edge(a, b, colour):
            segs.append((pts[a], pts[b], colour))

        for a, b in report.hole_edges:
            edge(a, b, self.VALIDATION_COLORS["hole"])
        for a, b in report.flipped_edges:
            edge(a, b, self.VALIDATION_COLORS["flipped"])
        for a, b in report.nonmanifold_edges:
            edge(a, b, self.VALIDATION_COLORS["nonmanifold"])
        for _v, (a, b) in report.t_joints:
            edge(a, b, self.VALIDATION_COLORS["t_joint"])

        def outline(fi, colour):
            try:
                # int(i) first: evaluated VNF indices are doubles, and a
                # float cannot index remap -- see vnf_validate._int_faces.
                idx = [int(remap[int(i)]) for i in faces[fi]]
            except (IndexError, TypeError, ValueError):
                return
            for k in range(len(idx)):
                edge(idx[k], idx[(k + 1) % len(idx)], colour)

        for fi, fj in report.intersecting:
            outline(fi, self.VALIDATION_COLORS["intersecting"])
            outline(fj, self.VALIDATION_COLORS["intersecting"])
        for fi, fj in report.overlapping:
            outline(fi, self.VALIDATION_COLORS["overlapping"])
            outline(fj, self.VALIDATION_COLORS["overlapping"])

        if not segs:
            return
        self._validation_segs = segs
        self.makeCurrent()
        self._build_validation_tubes()
        self.doneCurrent()
        self.update()

    def _build_validation_tubes(self):
        """(Re)build the overlay geometry for `_validation_segs`.

        Separate from `show_validation` because tube radius is chosen in
        screen space, so this has to run again whenever the camera
        distance changes -- same reason the vertex markers rebuild on
        zoom. Caller must already be current."""
        self._release_validation_tubes()
        if not self._validation_segs or self._ctx is None:
            return
        rows = []
        for p0, p1, colour in self._validation_segs:
            c = np.asarray(colour, dtype=np.float32)
            # Sized exactly like the vertex indicators: the same
            # screen-space radius helper, called at each end on its own
            # point rather than once on the midpoint, so a tube running
            # away from the camera keeps a constant apparent width along
            # its length the way a row of markers does.
            #
            # Half the marker radius: a tube as fat as a marker buries
            # the geometry it is pointing at, and a marker is only about
            # 5px in radius on screen -- measured, not the ~12 the first
            # draft assumed -- so much less than half lands back at
            # hairline width, which is what tubes were meant to fix.
            r0 = _marker_radius_for_point(self, p0) * 0.5
            r1 = _marker_radius_for_point(self, p1) * 0.5
            rows.extend(_tube_triangles(p0, p1, r0, r1, c))
        if not rows:
            return
        data = np.array(rows, dtype=np.float32)
        self._validation_vbo = self._ctx.buffer(data.tobytes())
        self._validation_vao = self._ctx.vertex_array(
            self._renderer._marker_prog,
            [(self._validation_vbo, "3f 3f 3f", "in_position", "in_normal", "in_color")],
        )

    def _release_validation_tubes(self):
        for attr in ("_validation_vao", "_validation_vbo"):
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.release()
                setattr(self, attr, None)

    def clear_validation(self):
        """Remove any validation overlay."""
        if not getattr(self, "_validation_segs", None):
            return
        self._validation_segs = []
        self.makeCurrent()
        self._release_validation_tubes()
        self.doneCurrent()
        self.update()

    def set_show_unselected(self, enabled: bool):
        """Toggle the "Show Vertices" checkbox. Selected-vertex markers are
        always shown and blinking; this governs every other vertex.

        Off by default here, unlike the Path/Grid/Region/Affine viewers,
        because a mesh carries far more vertices than a path does and the
        cost is linear in that count -- and paid again whenever the markers
        are rebuilt, which includes zoom (`frame_scene`). Measured on an
        M1: ~0.33s for 448 vertices, ~1.6s for 2,400, ~6s for 9,600 and
        ~19s for 28,000. Turning it on for a large mesh is a deliberate
        choice with a visible cost, so it is not made on the viewer's
        behalf."""
        self._show_unselected = enabled
        self.makeCurrent()
        self._build_unselected_markers()
        self.doneCurrent()
        self.update()

    def _build_unselected_markers(self):
        """Theme-colored dodecahedron markers for every vertex *not*
        currently selected -- mirrors `_PathViewport`/`_GridViewport.
        _build_point_markers` (same color, same shared `SceneRenderer.
        upload_points`/`clear_points` pipeline, auto-rendered by
        `_render_simple_points` with no `_paint_extra` changes needed),
        just gated behind `_show_unselected` rather than always on."""
        self._renderer.clear_points()
        if not self._show_unselected or self._ctx is None or len(self._verts_3d) == 0:
            return
        unselected_color = np.array(self._renderer.unselected_vertex_color[:3], dtype=np.float32)
        selected = set(self._vert_indices)
        unit_faces = _dodecahedron_faces(1.0, False)
        keep = [i for i in range(len(self._verts_3d)) if i not in selected]
        if keep:
            pts = np.asarray(self._verts_3d, dtype=np.float32)[keep]
            self._renderer.upload_points(_lit_marker_field(
                pts, _marker_radii_for_points(self, pts), unit_faces,
                unselected_color))

    def _release_vert_markers(self):
        for attr in ("_vert_marker_vao_r", "_vert_marker_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_vert_marker_vbo_r", "_vert_marker_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_vert_markers(self):
        self._release_vert_markers()
        if not self._vert_indices or self._ctx is None:
            return

        unit_faces = _dodecahedron_faces(1.0, False)

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_vert_marker_vao_r", "_vert_marker_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_vert_marker_vao_w", "_vert_marker_vbo_w"),
        ]:
            tris = []
            for vi in self._vert_indices:
                pt = self._verts_3d[vi]
                r = _marker_radius_for_point(self, pt)
                tris.extend(_lit_marker_triangles(pt, r, unit_faces, color_val))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._renderer._marker_prog,
                    [(vbo, "3f 3f 3f", "in_position", "in_normal", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def frame_scene(self, bb_min, bb_max, reframe: bool = True):
        # Always called from within an already-makeCurrent'd caller
        # (load_path/load_grid/load_matrix's own bracket, or the safe
        # initial schedule_load path) -- must NOT bracket with its own
        # makeCurrent/doneCurrent, since doneCurrent() would prematurely
        # release the context out from under that caller's remaining work.
        super().frame_scene(bb_min, bb_max, reframe=reframe)
        if self._vert_indices:
            self._build_vert_markers()
        if self._show_unselected:
            self._build_unselected_markers()
        if self._validation_segs:
            self._build_validation_tubes()

    def _on_zoom_changed(self):
        # Screen-space marker sizes depend on camera distance/FOV, so they
        # have to be rebuilt whenever those change -- by wheel OR by a
        # pinch gesture, which never reaches wheelEvent. Hooking the base
        # class's notification covers both. Unlike frame_scene above, this
        # runs from a genuine external Qt event, never from inside another
        # makeCurrent'd block, so it does need its own bracket.
        if self._vert_indices or self._show_unselected or self._validation_segs:
            self.makeCurrent()
            if self._vert_indices:
                self._build_vert_markers()
            if self._show_unselected:
                self._build_unselected_markers()
            if self._validation_segs:
                self._build_validation_tubes()
            self.doneCurrent()

    def _paint_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        cam = self._renderer.camera
        view = cam.view_matrix()
        light = np.array([0.6, 0.8, 1.0], dtype=np.float32)
        light /= np.linalg.norm(light)
        L_world = (view[:3, :3].T @ light).astype(np.float32)
        L_world /= np.linalg.norm(L_world)
        eye_pos = cam.eye_position()

        # Vertex markers (swap red/white)
        vao = self._vert_marker_vao_r if self._vert_blink_red else self._vert_marker_vao_w
        if vao is not None:
            marker_prog = self._renderer._marker_prog
            marker_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            marker_prog["light_dir"].value = tuple(L_world)
            marker_prog["eye_pos"].value = tuple(eye_pos)
            # Depth-tested (not disabled): markers must stay occluded by mesh
            # faces farther in front. Small polygon offset toward the camera
            # just breaks ties against coincident wireframe edges at the same
            # vertex position -- see SceneRenderer._render_simple_points.
            self._ctx.polygon_offset = (-1.0, -1.0)
            self._ctx.enable_direct(0x8037)  # GL_POLYGON_OFFSET_FILL
            vao.render(mgl.TRIANGLES)
            self._ctx.disable_direct(0x8037)
            self._ctx.polygon_offset = (0.0, 0.0)

        # Validation tubes. Depth-tested, like the vertex markers above
        # and for the same reason: a mark on the far side of the mesh
        # showing through the near surface reads as geometry that is not
        # there. They were drawn through the surface while they were
        # hairlines and needed the help to be seen at all; now that they
        # are solid tubes they do not, and the count in the label says
        # what is round the back.
        #
        # Same polygon offset as the markers: a mark lies exactly on a
        # mesh edge, so it needs to win that coincident-depth tie rather
        # than z-fight with the wireframe drawn at the same position.
        if self._validation_vao is not None:
            marker_prog = self._renderer._marker_prog
            marker_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            marker_prog["light_dir"].value = tuple(L_world)
            marker_prog["eye_pos"].value = tuple(eye_pos)
            self._ctx.polygon_offset = (-1.0, -1.0)
            self._ctx.enable_direct(0x8037)  # GL_POLYGON_OFFSET_FILL
            self._ctx.enable(mgl.CULL_FACE)
            self._validation_vao.render(mgl.TRIANGLES)
            self._ctx.disable(mgl.CULL_FACE)
            self._ctx.disable_direct(0x8037)
            self._ctx.polygon_offset = (0.0, 0.0)

        if self._highlight_vao is None:
            return
        self._ctx.polygon_offset = (-1.0, -1.0)
        self._ctx.enable_direct(0x8037)
        mesh_prog = self._renderer._mesh_prog
        mesh_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        mesh_prog["light_dir"].value = tuple(L_world)
        mesh_prog["eye_pos"].value = tuple(eye_pos)
        mesh_prog["object_color"].value = (0.2, 0.9, 0.3, 1.0)
        mesh_prog["backface_color"].value = (0.8, 0.0, 0.8, 1.0)
        self._highlight_vao.render()
        self._ctx.disable_direct(0x8037)

    def _pick_vertex(self, px: float, py: float) -> int:
        """Nearest vertex to the cursor among *all* mesh vertices (unlike
        the tooltip logic below, which is restricted to the currently
        highlighted set) -- Cmd+drag can grab any vertex regardless of
        table/face selection, same as `_PathViewport`/`_GridViewport`."""
        if len(self._verts_3d) == 0:
            return -1
        return self._renderer.pick_nearest_point(self._verts_3d, px, py, self.width(), self.height())

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        if (self._editable
                and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier   # Cmd on macOS
                and not (event.modifiers() & Qt.KeyboardModifier.AltModifier)):
            vi = self._pick_vertex(self._press_pos.x(), self._press_pos.y())
            if vi >= 0:
                self._drag_vertex_idx = vi
                self._drag_lock_axis = _view_locked_axis(self._renderer.camera)
                self._drag_plane_point = self._verts_3d[vi].copy()
                self._show_delta(f"Plane: {_unlocked_plane_name(self._drag_lock_axis)}")
                self.vertex_drag_started.emit()
                return   # don't arm orbit/pan -- this press starts a vertex drag
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._drag_vertex_idx >= 0:
            pos = event.position().toPoint()
            ray_o, ray_d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
            hit = _ray_plane_axis_locked(ray_o, ray_d, self._drag_plane_point, self._drag_lock_axis)
            if hit is not None:
                self.vertex_moved.emit(self._drag_vertex_idx,
                                        round(float(hit[0]), 3), round(float(hit[1]), 3), round(float(hit[2]), 3))
            return
        if self._last_mouse is None and self._vert_indices:
            pos = event.position().toPoint()
            candidates = self._verts_3d[self._vert_indices]
            local_idx = self._renderer.pick_nearest_point(
                candidates, pos.x(), pos.y(), self.width(), self.height())
            if local_idx >= 0:
                best_idx = self._vert_indices[local_idx]
                pt = self._verts_3d[best_idx]
                self.setToolTip(f"[{best_idx}]: ({pt[0]:g}, {pt[1]:g}, {pt[2]:g})")
            else:
                self.setToolTip("")
        elif not self._vert_indices:
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_vertex_idx >= 0:
            vi = self._drag_vertex_idx
            moved = self._drag_started
            self._drag_vertex_idx = -1
            self._press_pos = None
            self._drag_started = False
            self._delta_label.hide()
            self.vertex_drag_finished.emit()
            if not moved:
                # Cmd was held at press time (the only way _drag_vertex_idx
                # gets armed) but nothing actually moved -- toggle, not a
                # plain click.
                self.vertex_clicked.emit(vi, "toggle")
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            if vi >= 0:
                self.vertex_clicked.emit(vi, _vertex_click_mode(event.modifiers()))
            else:
                face = self._pick_face(pos.x(), pos.y())
                self.highlight_face(face)
                self.face_clicked.emit(face)
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        """Arrow keys nudge every currently-highlighted vertex, confined to
        the same axis-locked plane Cmd+drag uses -- see
        `_PathViewport.keyPressEvent`/`_GridViewport.keyPressEvent`. VNF
        vertices are always 3D (no 2D top-down special case). Step size
        (1 unit, or 0.1/10 with Cmd/Shift held) via `_key_nudge_magnitude`
        -- note Cmd here means the fine-nudge modifier, distinct from its
        other use starting a vertex *drag* on mouse-press."""
        if self._editable and self._vert_indices:
            lock_axis = _view_locked_axis(self._renderer.camera)
            magnitude = _key_nudge_magnitude(event.modifiers())
            delta = _key_nudge_delta(self._renderer.camera, lock_axis, event.key(), magnitude)
            if delta is not None:
                self.vertex_drag_started.emit()
                for vi in self._vert_indices:
                    if 0 <= vi < len(self._verts_3d):
                        new_pt = self._verts_3d[vi] + delta
                        self.vertex_moved.emit(vi, round(float(new_pt[0]), 3),
                                                round(float(new_pt[1]), 3), round(float(new_pt[2]), 3))
                self.vertex_drag_finished.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def _pick_face(self, px: float, py: float) -> int:
        if len(self._cpu_positions) == 0:
            return -1
        w, h = self.width(), self.height()
        if w == 0 or h == 0:
            return -1
        # The mesh buffer was uploaded with tri_ids=tri_to_face (see
        # VNFViewer._load_mesh), so ray_cast directly resolves the hit
        # triangle back to its face index.
        ray_o, ray_d = self._renderer.camera_ray(px, py, w, h)
        face_id = self._renderer.ray_cast(ray_o, ray_d)
        return face_id if face_id is not None else -1

    def contextMenuEvent(self, event):
        """Right-click a vertex marker for Duplicate Vertex, or (failing
        that) a face for Delete Face / Reverse Face -- editable only,
        mirrors _PathViewport.contextMenuEvent's pick-then-menu pattern,
        including the _last_mouse/_mouse_button reset before exec(): Qt
        doesn't reliably deliver this widget's own mouseReleaseEvent for a
        right-click that opens a popup, so without this a right-click that
        opens a menu can leave stale pan-drag state behind."""
        if not self._editable:
            return
        pos = event.pos()
        vi = self._pick_vertex(pos.x(), pos.y())
        if vi >= 0:
            self._last_mouse = None
            self._mouse_button = None
            menu = QMenu(self)
            menu.addAction("Duplicate Vertex", lambda: self.duplicate_vertex_requested.emit(vi))
            menu.exec(event.globalPos())
            return
        face = self._pick_face(pos.x(), pos.y())
        if face >= 0:
            self._last_mouse = None
            self._mouse_button = None
            menu = QMenu(self)
            menu.addAction("Delete Face", lambda: self.delete_face_requested.emit(face))
            menu.addAction("Reverse Face", lambda: self.reverse_face_requested.emit(face))
            menu.exec(event.globalPos())

    def closeEvent(self, event):
        self._vert_blink_timer.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# VNF Viewer
# ---------------------------------------------------------------------------

class VNFViewer(QDialog, _UndoableViewerMixin):
    """3D mesh viewer for VNF [vertices, faces] structures with vertex/face
    tables. Read-only by default; pass `editable=True` for a Save/Cancel
    editing mode (see `MatrixViewer` for the shared editing convention).
    Editable mode also supports face topology edits: add/duplicate/delete
    vertices and add/delete/reverse faces (vertex table "+"/"-" buttons and
    right-click menus, viewport right-click, and the "Add Face" button for
    a 3-vertex selection). Deleting a vertex cascade-deletes any face that
    referenced it and renumbers every surviving face's vertex indices to
    match the shrunk vertex list."""

    committed = Signal(str)

    def __init__(self, title: str, vnf_value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        label = "VNF Editor" if editable else "VNF Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(900, 560)
        self._editable = editable
        self._vnf = vnf_value
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Viewport — match main window's perspective mode, color theme, and FOV
        self._vp = _VNFViewport(splitter, editable=editable)
        for w in QApplication.topLevelWidgets():
            if hasattr(w, '_viewport'):
                self._vp._renderer.camera.orthographic = w._viewport._renderer.camera.orthographic
                break
        _sync_viewport_to_main_window(self._vp)
        self._vp.face_clicked.connect(self._on_viewport_face_clicked)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        splitter.addWidget(self._vp)

        # Tables in a tab widget
        self._tab_widget = QTabWidget(splitter)

        self._add_face_btn = None
        self._vert_table = self._make_vert_table(vnf_value[0], editable)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        vert_container = QWidget()
        vert_layout = QVBoxLayout(vert_container)
        vert_layout.setContentsMargins(0, 0, 0, 0)
        vert_layout.addWidget(self._vert_table, 1)
        if editable:
            vert_btn_row = QHBoxLayout()
            add_vert_btn = QPushButton("+")
            add_vert_btn.setFixedWidth(42)
            add_vert_btn.setToolTip("Add a vertex (duplicates the last one, offset)")
            add_vert_btn.clicked.connect(self._add_vertex_default)
            vert_btn_row.addWidget(add_vert_btn)
            del_vert_btn = QPushButton("–")
            del_vert_btn.setFixedWidth(42)
            del_vert_btn.setToolTip("Delete the selected vertices")
            del_vert_btn.clicked.connect(lambda: self._delete_vertices(self._selected_vertex_indices()))
            vert_btn_row.addWidget(del_vert_btn)
            vert_btn_row.addSpacing(12)
            self._add_face_btn = QPushButton("Add Face")
            self._add_face_btn.setEnabled(False)
            self._add_face_btn.setToolTip("Select exactly 3 vertices to create a face from them")
            self._add_face_btn.clicked.connect(self._add_face_from_selection)
            vert_btn_row.addWidget(self._add_face_btn)
            vert_btn_row.addStretch()
            vert_layout.addLayout(vert_btn_row)
            self._vert_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._vert_table.customContextMenuRequested.connect(self._show_vert_table_context_menu)
        self._tab_widget.addTab(vert_container, f"Vertices ({len(vnf_value[0])})")

        self._face_table = self._make_face_table(vnf_value[1])
        self._face_table.itemSelectionChanged.connect(self._on_face_table_selection)
        if editable:
            self._face_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._face_table.customContextMenuRequested.connect(self._show_face_table_context_menu)
        self._tab_widget.addTab(self._face_table, f"Faces ({len(vnf_value[1])})")

        splitter.addWidget(self._tab_widget)
        vt = self._vert_table
        fm = vt.fontMetrics()
        vh_w = fm.horizontalAdvance(str(max(len(vnf_value[0]) - 1, 0))) + 20
        table_w = (vh_w
                   + sum(vt.columnWidth(j) for j in range(vt.columnCount()))
                   + vt.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = self._btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        show_unselected_cb = QCheckBox("Show Vertices")
        show_unselected_cb.toggled.connect(self._vp.set_show_unselected)
        btn_row.addWidget(show_unselected_cb)

        # Manifold validation. Deliberately a button rather than something
        # that runs on open: the pairwise face tests are the expensive part
        # and a mesh is usually opened to look at, not to audit.
        self._validate_btn = QPushButton("Validate")
        self._validate_btn.setToolTip(
            "Check for holes, flipped normals, T-joints, intersecting faces "
            "and overlapping coplanar faces, and highlight what it finds")
        self._validate_btn.clicked.connect(self._on_validate)
        btn_row.addWidget(self._validate_btn)
        self._validation_label = QLabel("")
        self._validation_label.setWordWrap(True)
        btn_row.addWidget(self._validation_label, 1)
        if editable:
            self._setup_undo(vnf_value)
            btn_row.addLayout(self._make_reset_button_row())
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._vp.schedule_load(self._load_mesh)
        if editable:
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)
            self._vp.vertex_drag_started.connect(self._begin_live_edit)
            self._vp.vertex_drag_finished.connect(lambda: self._end_live_edit("Move Vertex"))
            self._vp.duplicate_vertex_requested.connect(self._duplicate_vertex)
            self._vp.delete_face_requested.connect(self._delete_face)
            self._vp.reverse_face_requested.connect(self._reverse_face)

    def _clear_validation(self):
        """Drop a validation result that no longer describes the mesh.

        A report is a statement about one exact VNF. After an edit its
        marks sit on vertices that have moved, or on faces that are gone
        -- a hole edge left over on a mesh you just repaired reads as a
        defect that is not there, which is worse than no answer.

        Called outside `_rebuild`'s makeCurrent bracket on purpose:
        `clear_validation` brackets itself, and a nested `doneCurrent`
        would release the context out from under the caller's remaining
        work (the same trap `frame_scene` documents)."""
        self._vp.clear_validation()
        self._validation_label.setText("")
        self._validation_label.setStyleSheet("")

    def _on_validate(self):
        """Run manifold validation over the VNF as it currently stands."""
        from belfryscad.vnf_validate import validate_vnf

        self._validate_btn.setEnabled(False)
        self._validation_label.setText("Checking…")
        QApplication.processEvents()
        try:
            report = validate_vnf(self._vnf)
        except Exception as e:                       # noqa: BLE001
            # A check must never take the viewer down with it.
            self._validation_label.setText(f"Validation failed: {e}")
            self._vp.clear_validation()
            return
        finally:
            self._validate_btn.setEnabled(True)

        faces = self._vnf[1] if len(self._vnf) > 1 else []
        self._vp.show_validation(None if report.ok else report, faces)
        text = report.summary()
        if report.notes:
            text += "  (" + "; ".join(report.notes) + ")"
        self._validation_label.setText(text)
        self._validation_label.setStyleSheet(
            "color: #1a7f37;" if report.ok else "color: #b32020;")

    @staticmethod
    def _make_vert_table(verts, editable: bool = False) -> QTableWidget:
        t = QTableWidget(len(verts), 3)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["X", "Y", "Z"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(verts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if editable:
            t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                               | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, v in enumerate(verts):
            for j in range(3):
                item = QTableWidgetItem(f"{v[j]:g}")
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(3):
            t.setColumnWidth(j, min_w)
        return t

    @staticmethod
    def _make_face_table(faces) -> QTableWidget:
        t = QTableWidget(len(faces), 1)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["Vertex Indices"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(faces))])
        _style_table_headers(t)
        t.horizontalHeader().setStretchLastSection(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, f in enumerate(faces):
            text = "[" + ", ".join(str(int(idx)) for idx in f) + "]"
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            t.setItem(i, 0, item)
        return t

    def _load_mesh(self, reframe: bool = True):
        # Clears any mesh buffer from a prior call -- a no-op the first
        # time (schedule_load's initial-load path, buffers start empty),
        # but required now that an editable dialog's _rebuild() can call
        # this repeatedly, or it'd leak/duplicate GPU buffers.
        self._vp._renderer._clear_buffers()
        verts_raw, faces_raw = self._vnf
        verts = np.array(verts_raw, dtype=np.float32)
        all_positions = []
        all_normals = []
        all_edge_starts = []
        all_edge_ends = []
        tri_to_face = []

        for fi, face in enumerate(faces_raw):
            idxs = [int(i) for i in face]
            if len(idxs) < 3:
                continue
            v0 = verts[idxs[0]]
            for k in range(1, len(idxs) - 1):
                v1, v2 = verts[idxs[k]], verts[idxs[k + 1]]
                # Reverse winding: OpenSCAD uses CW from outside, OpenGL expects CCW
                n = np.cross(v2 - v0, v1 - v0)
                ln = np.linalg.norm(n)
                if ln > 0:
                    n /= ln
                all_positions.extend([v0, v2, v1])
                all_normals.extend([n, n, n])
                tri_to_face.append(fi)
            for k in range(len(idxs)):
                all_edge_starts.append(verts[idxs[k]])
                all_edge_ends.append(verts[idxs[(k + 1) % len(idxs)]])

        if not all_positions:
            return

        positions = np.array(all_positions, dtype=np.float32)
        normals = np.array(all_normals, dtype=np.float32)
        tri_to_face_arr = np.array(tri_to_face, dtype=np.int32)

        self._vp.set_face_data(positions, tri_to_face_arr, verts)

        starts = np.array(all_edge_starts, dtype=np.float32)
        ends = np.array(all_edge_ends, dtype=np.float32)
        edge_positions = np.empty((len(starts) * 2, 3), dtype=np.float32)
        edge_positions[0::2] = starts
        edge_positions[1::2] = ends

        # tri_ids=tri_to_face_arr lets SceneRenderer.ray_cast resolve a hit
        # triangle straight back to its OpenSCAD face index for picking.
        # color left as the default (None) so it tracks the live color
        # theme's object color instead of a fixed one.
        self._vp._renderer.upload_mesh(positions, normals,
                             edge_positions=edge_positions,
                             tri_ids=tri_to_face_arr)

        bb_min = verts.min(axis=0)
        bb_max = verts.max(axis=0)
        self._vp.frame_scene(bb_min, bb_max, reframe=reframe)
        self._vp.update()

    def _rebuild(self, reframe: bool = True):
        """Re-triangulate and re-upload the mesh after a vertex edit --
        face topology never changes in this dialog, so `_load_mesh` just
        recomputes from the same faces against updated vertex positions.
        Unlike the initial `schedule_load` call, this needs its own
        makeCurrent bracket (a live edit isn't guaranteed to run inside
        `initializeGL`), and needs to explicitly rebuild the face-highlight
        overlay too -- `frame_scene` (which `_load_mesh` calls) already
        rebuilds vertex markers for us but not the highlight VAO, which
        would otherwise keep showing the *old* vertex positions for
        whichever face is currently highlighted. `reframe=False` (used for
        a live vertex drag/nudge) skips the camera re-fit -- see
        `_on_viewport_vertex_moved`.

        Every path that changes the VNF comes through here -- table
        edits, vertex drags, face add/delete/reverse, undo and redo --
        which is why the validation report is dropped here rather than
        at each of them."""
        self._clear_validation()
        if self._vp._ctx is not None:
            self._vp.makeCurrent()
            self._load_mesh(reframe=reframe)
            if self._vp._selected_face >= 0:
                self._vp._rebuild_highlight()
            self._vp.doneCurrent()
            self._vp.update()

    def _get_value(self):
        return self._vnf

    def _apply_value(self, value):
        self._vnf = value
        self._populate_vert_table()
        self._populate_face_table()
        self._rebuild()

    def _populate_vert_table(self):
        """Full rebuild of the vertex table from `self._vnf[0]` -- unlike
        `_on_item_changed`/`_on_viewport_vertex_moved`, which only ever
        touch existing cells' text in place, add/duplicate/delete changes
        the row count, so the whole table needs repopulating (mirrors
        `PathViewer._populate_vert_table`'s identical row-count-change
        situation)."""
        verts = self._vnf[0]
        self._vert_table.blockSignals(True)
        self._vert_table.setRowCount(len(verts))
        self._vert_table.setVerticalHeaderLabels([str(i) for i in range(len(verts))])
        for i, v in enumerate(verts):
            for j in range(3):
                item = QTableWidgetItem(f"{v[j]:g}")
                if not self._editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._vert_table.setItem(i, j, item)
        self._vert_table.blockSignals(False)
        self._tab_widget.setTabText(0, f"Vertices ({len(verts)})")

    def _populate_face_table(self):
        """Full rebuild of the face table from `self._vnf[1]` -- same
        row-count-change situation as `_populate_vert_table`."""
        faces = self._vnf[1]
        self._face_table.blockSignals(True)
        self._face_table.setRowCount(len(faces))
        self._face_table.setVerticalHeaderLabels([str(i) for i in range(len(faces))])
        for i, f in enumerate(faces):
            text = "[" + ", ".join(str(int(idx)) for idx in f) + "]"
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._face_table.setItem(i, 0, item)
        self._face_table.blockSignals(False)
        self._tab_widget.setTabText(1, f"Faces ({len(faces)})")

    def _on_item_changed(self, item: QTableWidgetItem):
        i, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._vnf[0][i][j]:g}")
            self._vert_table.blockSignals(False)
            return
        new_value = copy.deepcopy(self._vnf)
        new_value[0][i][j] = parsed
        self._commit_value(new_value, "Edit Vertex")

    def _on_viewport_vertex_moved(self, vi: int, x: float, y: float, z: float):
        """Live update while Cmd+dragging or arrow-key-nudging a vertex
        marker in the editable viewport -- mirrors `_on_item_changed`'s
        self._vnf + table + rebuild update, just driven by the viewport
        instead of a table-cell edit. `reframe=False`: a live move
        shouldn't re-fit/zoom the camera to the whole mesh on every
        frame -- `scroll_to_visible` instead just pans (if needed) to
        keep this one vertex on-screen."""
        self._vnf[0][vi][0] = x
        self._vnf[0][vi][1] = y
        self._vnf[0][vi][2] = z
        self._vert_table.blockSignals(True)
        self._vert_table.item(vi, 0).setText(f"{x:g}")
        self._vert_table.item(vi, 1).setText(f"{y:g}")
        self._vert_table.item(vi, 2).setText(f"{z:g}")
        self._vert_table.blockSignals(False)
        self._rebuild(reframe=False)
        self._vp.scroll_to_visible(np.array([x, y, z]))

    def _on_save(self):
        self.committed.emit(_format_value(self._vnf))
        self.accept()

    def _on_vert_table_selection(self):
        if self._syncing:
            return
        rows = self._vert_table.selectionModel().selectedRows()
        indices = [r.row() for r in rows]
        self._vp.highlight_vertices(indices)
        if self._add_face_btn is not None:
            self._add_face_btn.setEnabled(len(indices) == 3)

    def _selected_vertex_indices(self) -> set:
        return {r.row() for r in self._vert_table.selectionModel().selectedRows()}

    @staticmethod
    def _offset_position(base_pos, existing_verts, step=(1.0, 0.0, 0.0)):
        """Return a position offset from `base_pos` along `step` that
        doesn't coincide (within a small float epsilon) with any position
        already in `existing_verts` -- guards a duplicated/added vertex
        against landing exactly on top of an existing one. Unlikely for a
        single duplicate, but a real risk for repeated duplicates-of-
        duplicates, or a symmetric mesh where the offset spot happens to
        already be occupied. Always terminates: a finite vertex set can't
        block an unboundedly growing offset forever."""
        n = 1
        while True:
            candidate = [base_pos[0] + step[0] * n, base_pos[1] + step[1] * n, base_pos[2] + step[2] * n]
            if not any(all(abs(candidate[k] - v[k]) < 1e-9 for k in range(3)) for v in existing_verts):
                return candidate
            n += 1

    def _duplicate_vertex(self, source_idx: int):
        """Append a copy of vertex `source_idx`, offset a bit, to the end
        of the vertex list -- triggered from the vertex table/viewport
        right-click menu. No face changes; the new vertex starts
        unreferenced by any face."""
        verts = self._vnf[0]
        if source_idx < 0 or source_idx >= len(verts):
            return
        new_value = copy.deepcopy(self._vnf)
        new_value[0].append(self._offset_position(verts[source_idx], verts))
        self._commit_value(new_value, "Duplicate Vertex")
        self._vert_table.selectRow(len(new_value[0]) - 1)

    def _add_vertex_default(self):
        """The "+" button: duplicates the last vertex (offset), or starts
        a fresh mesh at the origin if the vertex list is currently empty."""
        verts = self._vnf[0]
        new_value = copy.deepcopy(self._vnf)
        if verts:
            new_value[0].append(self._offset_position(verts[-1], verts))
        else:
            new_value[0].append([0.0, 0.0, 0.0])
        self._commit_value(new_value, "Add Vertex")
        self._vert_table.selectRow(len(new_value[0]) - 1)

    def _delete_vertices(self, indices):
        """The "-" button / Delete key / no-arg selection path: removes
        every given vertex row, cascade-deletes any face that referenced
        one of them, and renumbers every surviving face's vertex indices
        to match the shrunk vertex list (old_idx -> new_idx built from the
        survivors in order -- same technique as
        `PathViewer._delete_vertex`'s `index_map`, applied to faces instead
        of `_node_types`)."""
        if not indices:
            return
        old_verts = self._vnf[0]
        old_faces = self._vnf[1]
        index_map = {}
        new_i = 0
        for old_i in range(len(old_verts)):
            if old_i in indices:
                continue
            index_map[old_i] = new_i
            new_i += 1
        new_verts = [v for i, v in enumerate(old_verts) if i not in indices]
        new_faces = []
        for face in old_faces:
            face_idxs = [int(vi) for vi in face]
            if any(vi in indices for vi in face_idxs):
                continue  # cascade delete: face referenced a deleted vertex
            new_faces.append([index_map[vi] for vi in face_idxs])
        self._commit_value([new_verts, new_faces], "Delete Vertex")
        self._vert_table.clearSelection()

    def _add_face_from_selection(self):
        """The "Add Face" button: requires exactly 3 selected vertex rows
        (enforced by the button's own enabled state, re-checked here
        defensively). Appends the 3 indices in ascending row order --
        winding isn't inferred from geometry, use Reverse Face afterward
        if it comes out backwards."""
        indices = sorted(self._selected_vertex_indices())
        if len(indices) != 3:
            return
        new_value = copy.deepcopy(self._vnf)
        new_value[1].append(list(indices))
        self._commit_value(new_value, "Add Face")
        self._face_table.selectRow(len(new_value[1]) - 1)

    def _delete_face(self, face_idx: int):
        faces = self._vnf[1]
        if face_idx < 0 or face_idx >= len(faces):
            return
        new_value = copy.deepcopy(self._vnf)
        del new_value[1][face_idx]
        self._commit_value(new_value, "Delete Face")

    def _reverse_face(self, face_idx: int):
        faces = self._vnf[1]
        if face_idx < 0 or face_idx >= len(faces):
            return
        new_value = copy.deepcopy(self._vnf)
        new_value[1][face_idx] = list(reversed(new_value[1][face_idx]))
        self._commit_value(new_value, "Reverse Face")

    def _show_vert_table_context_menu(self, pos):
        """Right-click menu on the vertex table (`editable=True` only) --
        mirrors `PathViewer._show_vert_table_context_menu`'s `rowAt`
        idiom: no menu when the click lands on empty space below the last
        row."""
        vertex_idx = self._vert_table.rowAt(pos.y())
        if vertex_idx < 0:
            return
        menu = QMenu(self._vert_table)
        menu.addAction("Duplicate Vertex", lambda: self._duplicate_vertex(vertex_idx))
        menu.exec(self._vert_table.viewport().mapToGlobal(pos))

    def _show_face_table_context_menu(self, pos):
        """Right-click menu on the face table (`editable=True` only) --
        same `rowAt` idiom as `_show_vert_table_context_menu`."""
        face_idx = self._face_table.rowAt(pos.y())
        if face_idx < 0:
            return
        menu = QMenu(self._face_table)
        menu.addAction("Delete Face", lambda: self._delete_face(face_idx))
        menu.addAction("Reverse Face", lambda: self._reverse_face(face_idx))
        menu.exec(self._face_table.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):
        """Delete/Backspace deletes the currently selected vertices --
        mirrors `PathViewer.keyPressEvent`'s identical convention for the
        same kind of vertex table."""
        if self._editable and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete_vertices(self._selected_vertex_indices())
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_face_table_selection(self):
        if self._syncing:
            return
        rows = self._face_table.selectionModel().selectedRows()
        if rows:
            face_idx = rows[0].row()
            self._syncing = True
            self._vp.highlight_face(face_idx)
            self._select_face_vertices(face_idx)
            self._syncing = False
        else:
            self._syncing = True
            self._vp.highlight_face(-1)
            self._vert_table.clearSelection()
            self._vp.highlight_vertices([])
            self._syncing = False

    def _on_viewport_face_clicked(self, face_idx: int):
        if self._syncing:
            return
        self._syncing = True
        self._tab_widget.setCurrentIndex(1)
        if 0 <= face_idx < self._face_table.rowCount():
            self._face_table.selectRow(face_idx)
            self._face_table.scrollTo(self._face_table.model().index(face_idx, 0))
            self._select_face_vertices(face_idx)
        else:
            self._face_table.clearSelection()
            self._vert_table.clearSelection()
            self._vp.highlight_vertices([])
        self._syncing = False

    def _on_viewport_vertex_clicked(self, vi: int, mode: str):
        """Click on a vertex marker (selected or, with "Show Unselected
        Vertices" on, one of the green ones) applies the standardized
        replace/add/toggle selection mode (`_apply_click_selection`) --
        takes priority over face picking. Switches to the Vertices tab;
        doesn't touch any current face selection/highlight, matching how
        selecting vertices directly in the table already leaves face
        selection alone (`_on_vert_table_selection`). Lets the resulting
        `itemSelectionChanged` -> `_on_vert_table_selection` handle
        highlighting and the Add Face button's enabled state naturally,
        rather than bypassing it here -- necessary now that a click can
        add to an existing selection instead of always replacing it."""
        self._tab_widget.setCurrentIndex(0)
        _apply_click_selection(self._vert_table, vi, mode)

    def _select_face_vertices(self, face_idx: int):
        """Select the vertices referenced by the given face in the vertex table."""
        faces_raw = self._vnf[1]
        if face_idx < 0 or face_idx >= len(faces_raw):
            return
        vert_indices = [int(i) for i in faces_raw[face_idx]]
        sel = self._vert_table.selectionModel()
        sel.clearSelection()
        model = self._vert_table.model()
        for vi in vert_indices:
            if 0 <= vi < self._vert_table.rowCount():
                idx = model.index(vi, 0)
                sel.select(idx, QItemSelectionModel.SelectionFlag.Select
                           | QItemSelectionModel.SelectionFlag.Rows)
        self._vp.highlight_vertices(vert_indices)
