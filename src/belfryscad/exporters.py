"""Export entry point, plus the mesh helpers the rest of the GUI shares.

The writers themselves are no longer here. Every format -- STL, OBJ, OFF,
3MF, PLY, VRML, X3D -- and the whole colour pipeline behind them (the
implicit top-level union, the per-colour volume split, per-triangle colour)
now live in openscad_cpp_evaluator's export.cpp, so the CLI and the GUI
cannot drift apart the way they had. This module is the thin interface the
GUI talks to.

What is still here is the mesh plumbing that is not about export at all:
`body_to_manifold`/`body_mesh_arrays`/`merge_bodies_to_mesh` are used by the
viewport's own mesh checks (main_window's validation panel), and `exportable`
is the `%`-background rule that several of those callers apply themselves.
"""


def exportable(bodies):
    """The bodies that belong in an exported file.

    `%` marks something as scenery: it is drawn so you can line other
    things up against it, and the reference leaves it out of the model
    entirely -- `%cube(10);` on its own exports nothing at all. It is
    excluded from booleans upstream for the same reason, so letting it
    reach a file put geometry there that no boolean had ever accounted for.

    Highlighted (`#`) bodies are NOT filtered here. `#` does not change
    the model, so a highlighted object at top level is real geometry and
    has to be written; only the display copy of one consumed by a boolean
    is redundant, and nothing in a ColoredBody tells the two apart. See
    docs/rendering.md.
    """
    return [b for b in bodies if getattr(b, "role", "normal") != "background"]


def export_model(path: str, geometry, format: str = "", ascii_stl: bool = False,
                 strip_slivers: bool = True) -> list:
    """Write `geometry` to `path`; returns the warnings to surface.

    `geometry` is the opaque handle the evaluator stashes on itself as
    `.geometry` -- the evaluated bodies, still on the C++ side. Export has
    to do real CSG, and going through the flattened arrays the renderer
    gets would mean rebuilding every Manifold first.

    Format comes from the extension unless `format` overrides it. Nothing
    here refuses to write: a deliberately open surface is a legitimate
    export, so problems come back as warnings for the caller to log.
    """
    from openscad_cpp_evaluator import export_model as _export_model

    return _export_model(path, geometry, format=format, ascii_stl=ascii_stl,
                         strip_slivers=strip_slivers)


def export_extensions() -> list:
    """The extensions export_model understands, dot-prefixed."""
    from openscad_cpp_evaluator import export_extensions as _exts

    return _exts()


def body_to_manifold(b):
    """The Manifold behind one rendered body.

    The evaluator hands back bodies wrapped in a shim that duck-types only
    what the renderer needs -- is_empty() and to_mesh(). Anything wanting to
    measure a body (volume, area, genus) has to rebuild the real thing from
    its mesh, which is what this does.
    """
    import numpy as np
    import manifold3d

    m = b.body.to_mesh()
    v = np.asarray(m.vert_properties[:, :3], dtype=np.float32)
    t = np.asarray(m.tri_verts, dtype=np.uint32)
    return manifold3d.Manifold(manifold3d.Mesh(v, t))


def body_mesh_arrays(b):
    """One body's raw triangles, as (vertices, faces) float32/uint32."""
    import numpy as np

    m = b.body.to_mesh()
    return (np.asarray(m.vert_properties[:, :3], dtype=np.float32),
            np.asarray(m.tri_verts, dtype=np.uint32))


def merge_bodies_to_mesh(bodies, open_parts=None):
    """Union the exportable bodies into one mesh for STL/OBJ. None if all
    are empty.

    `open_parts`, if a list is passed, receives the 1-based index of every
    body that is not a closed solid. Manifold discards such a body outright
    -- an open shell converts to nothing -- so before this took them into
    account, exporting a surface with a missing face wrote a valid STL
    containing zero triangles and said nothing. Their triangles are real
    geometry the user can see in the viewport, so they are concatenated onto
    the unioned solids rather than dropped; the caller warns about them.

    A union, not a concatenation. Concatenating is right only while the
    bodies are disjoint: where two touch, each writes its own copy of the
    shared face, so the file gets coincident duplicate faces and edges used
    by four triangles. A Menger sponge is 400 abutting cubes at level 2 and
    came out with 1784 non-manifold edges -- valid-looking in a viewer, and
    rejected or silently "repaired" by a slicer.

    This is what the C++ exporter has always done (composeMesh, BatchBoolean
    Add); the two disagreed, and the same sponge exported manifold through
    the CLI and non-manifold through here.
    """
    import numpy as np
    from types import SimpleNamespace
    import manifold3d

    bodies = exportable(bodies)
    parts, loose = [], []
    for i, b in enumerate(bodies):
        if b.body.is_empty():
            continue
        v, t = body_mesh_arrays(b)
        man = manifold3d.Manifold(manifold3d.Mesh(v, t))
        if man.is_empty() and len(t):
            # Manifold rejected it: not a closed solid. Keep the triangles.
            loose.append((v, t))
            if open_parts is not None:
                open_parts.append(i + 1)
        elif not man.is_empty():
            parts.append(man)
    if not parts and not loose:
        return None

    chunks = []
    if parts:
        merged = (parts[0] if len(parts) == 1
                  else manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add))
        m = merged.to_mesh()
        chunks.append((np.asarray(m.vert_properties[:, :3], dtype=np.float32),
                       np.asarray(m.tri_verts, dtype=np.uint32)))
    chunks.extend(loose)

    verts, faces, offset = [], [], 0
    for v, t in chunks:
        verts.append(v)
        faces.append(t.astype(np.int64) + offset)
        offset += len(v)
    return SimpleNamespace(
        vert_properties=np.concatenate(verts) if len(verts) > 1 else verts[0],
        tri_verts=np.concatenate(faces) if len(faces) > 1 else faces[0])
