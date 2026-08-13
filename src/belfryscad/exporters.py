"""Mesh file writers -- STL/OBJ/3MF. Pure functions, no Qt/GL dependency, so
they're usable from both the GUI (MainWindow._export) and headless CLI export
(belfryscad.headless).
"""


def _stl_triangles(mesh):
    """(verts_per_tri_v0, v1, v2, face_normals) -- shared by write_stl and
    write_stl_ascii."""
    import numpy as np

    verts = np.asarray(mesh.vert_properties[:, :3], dtype=np.float32)
    tris = np.asarray(mesh.tri_verts, dtype=np.int32)

    v0 = verts[tris[:, 0]]
    v1 = verts[tris[:, 1]]
    v2 = verts[tris[:, 2]]

    normals = np.cross(v1 - v0, v2 - v0).astype(np.float32)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals /= np.where(lengths > 0, lengths, 1.0)
    return v0, v1, v2, normals


def write_stl(path: str, mesh):
    import struct
    import numpy as np

    v0, v1, v2, normals = _stl_triangles(mesh)

    dtype = np.dtype([
        ("normal", np.float32, (3,)),
        ("v0",     np.float32, (3,)),
        ("v1",     np.float32, (3,)),
        ("v2",     np.float32, (3,)),
        ("attr",   np.uint16),
    ])
    data = np.zeros(len(v0), dtype=dtype)
    data["normal"] = normals
    data["v0"] = v0
    data["v1"] = v1
    data["v2"] = v2

    with open(path, "wb") as f:
        f.write(b"\0" * 80)
        f.write(struct.pack("<I", len(v0)))
        f.write(data.tobytes())


def write_stl_ascii(path: str, mesh):
    """OpenSCAD-compatible ASCII STL -- `solid OpenSCAD_Model` / one `facet
    normal .. outer loop .. vertex x3 .. endloop endfacet` block per
    triangle / `endsolid`. Format confirmed directly against real
    OpenSCAD.app's own -o out.stl default output."""
    v0, v1, v2, normals = _stl_triangles(mesh)

    def fmt(v):
        return " ".join(repr(float(c)) for c in v)

    with open(path, "w", encoding="utf-8") as f:
        f.write("solid OpenSCAD_Model\n")
        for n, a, b, c in zip(normals, v0, v1, v2):
            f.write(f"  facet normal {fmt(n)}\n")
            f.write("    outer loop\n")
            f.write(f"      vertex {fmt(a)}\n")
            f.write(f"      vertex {fmt(b)}\n")
            f.write(f"      vertex {fmt(c)}\n")
            f.write("    endloop\n")
            f.write("  endfacet\n")
        f.write("endsolid OpenSCAD_Model\n")


def write_obj(path: str, objects):
    """One `o` group per object, plus a companion .mtl for the colours.

    `objects` is split_bodies_for_export()'s [(verts, tris, rgba)] -- OBJ
    can hold separate named objects, so it gets the same split 3MF does
    rather than one merged mesh.

    OBJ carries no colour of its own; `usemtl` names an entry in a .mtl
    file sitting next to the .obj, so exporting now writes TWO files. A
    reader that ignores the mtllib still gets correct geometry, and each
    distinct rgba gets one material however many objects share it.
    """
    import os

    mtl_path = os.path.splitext(path)[0] + ".mtl"

    # One material per distinct colour, in first-seen order.
    materials = {}
    for _v, _t, rgba in objects:
        materials.setdefault(tuple(rgba), f"color_{len(materials) + 1}")

    with open(path, "w", encoding="utf-8") as f:
        if materials:
            f.write(f"mtllib {os.path.basename(mtl_path)}\n\n")
        offset = 1          # OBJ vertex indices are 1-based and file-global
        for i, (verts, tris, rgba) in enumerate(objects, start=1):
            f.write(f"o object_{i}\n")
            f.write(f"usemtl {materials[tuple(rgba)]}\n")
            for v in verts:
                f.write(f"v {v[0]:.6g} {v[1]:.6g} {v[2]:.6g}\n")
            for tri in tris:
                f.write(f"f {tri[0]+offset} {tri[1]+offset} {tri[2]+offset}\n")
            f.write("\n")
            offset += len(verts)

    if not materials:
        return
    with open(mtl_path, "w", encoding="utf-8") as f:
        for rgba, name in materials.items():
            r, g, b = (max(0.0, min(1.0, float(c))) for c in rgba[:3])
            a = max(0.0, min(1.0, float(rgba[3]))) if len(rgba) > 3 else 1.0
            f.write(f"newmtl {name}\n")
            f.write(f"Kd {r:.6g} {g:.6g} {b:.6g}\n")
            # d is opacity, not transparency -- 1 is solid.
            if a < 1.0:
                f.write(f"d {a:.6g}\n")
            f.write("\n")


def write_ply(path: str, objects):
    """Binary little-endian PLY, one flat mesh with per-vertex colour.

    PLY has no object concept to map the split onto, so the objects are
    concatenated into a single vertex/face list and the colour rides on the
    vertices instead. That is lossless here only because the split
    guarantees the objects are disjoint and never share a vertex.
    """
    import numpy as np

    verts, faces, colors, offset = [], [], [], 0
    for v, t, rgba in objects:
        verts.append(np.asarray(v, dtype="<f4"))
        faces.append(np.asarray(t, dtype="<i4") + offset)
        rgb = np.array([max(0, min(255, int(round(c * 255)))) for c in rgba[:3]],
                       dtype=np.uint8)
        colors.append(np.tile(rgb, (len(v), 1)))
        offset += len(v)

    if verts:
        verts = np.concatenate(verts)
        faces = np.concatenate(faces)
        colors = np.concatenate(colors)
    else:
        verts = np.zeros((0, 3), dtype="<f4")
        faces = np.zeros((0, 3), dtype="<i4")
        colors = np.zeros((0, 3), dtype=np.uint8)

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "comment Written by BelfrySCAD\n"
        f"element vertex {len(verts)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        f"element face {len(faces)}\n"
        "property list uchar int vertex_indices\n"
        "end_header\n"
    )

    vert_dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                           ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    vrec = np.zeros(len(verts), dtype=vert_dtype)
    for i, name in enumerate(("x", "y", "z")):
        vrec[name] = verts[:, i]
    for i, name in enumerate(("red", "green", "blue")):
        vrec[name] = colors[:, i]

    # Each face is a count byte followed by its three int32 indices.
    face_dtype = np.dtype([("n", "u1"), ("v", "<i4", (3,))])
    frec = np.zeros(len(faces), dtype=face_dtype)
    frec["n"] = 3
    frec["v"] = faces

    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        f.write(vrec.tobytes())
        f.write(frec.tobytes())


def exportable(bodies):
    """The bodies that belong in an exported file.

    `%` marks something as scenery: it is drawn so you can line other
    things up against it, and the reference leaves it out of the model
    entirely -- `%cube(10);` on its own exports nothing at all, where this
    used to write the cube. It is excluded from booleans upstream for the
    same reason, so letting it reach a file put geometry there that no
    boolean had ever accounted for.

    Highlighted (`#`) bodies are NOT filtered here. `#` does not change
    the model, so a highlighted object at top level is real geometry and
    has to be written; only the display copy of one consumed by a boolean
    is redundant, and nothing in a ColoredBody tells the two apart. See
    docs/rendering.md.
    """
    return [b for b in bodies if getattr(b, "role", "normal") != "background"]


# --- 3MF ---------------------------------------------------------------
# 3MF is an OPC package: a ZIP holding a content-types part, a
# relationships part, and an XML model. Everything written here is the
# core mesh spec plus one solid colour per object from the materials
# extension, which is the whole of what BelfrySCAD emits -- no beam
# lattice, slices, textures, or transforms beyond identity, and no reader.
#
# This replaced lib3mf, which was a conditional dependency: it has no
# aarch64/ARM64 wheels, so .3mf export simply did not exist on Linux ARM
# or Windows on ARM. Verified against lib3mf while both existed --
# identical vertices, triangles and colours read back, and OpenSCAD
# imports the result indistinguishably from lib3mf's own output.

_3MF_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_3MF_MATERIAL = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
_3MF_REL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"

_3MF_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\
<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\
</Types>
"""

_3MF_RELS = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\
<Relationship Type="{_3MF_REL}" Target="/3D/3dmodel.model" Id="rel0"/>\
</Relationships>
"""


def _3mf_num(v):
    """A coordinate as 3MF wants it: a plain decimal.

    The spec's ST_Number forbids exponent notation, so "1e-07" would be
    invalid -- hence fixed-point. Six decimal places is well beyond the
    float32 the mesh arrives as; magnitudes below ~1e-6 flush to zero,
    which no mm-scale model reaches.
    """
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def build_3mf_model_xml(meshes):
    """The 3D/3dmodel.model part for [(verts, tris, rgba-or-None), ...].

    Indented with tabs and spaced before each `/>`, matching what lib3mf
    wrote, so the part reads sensibly when someone unzips a .3mf to look
    at it. Neither costs much once deflated -- see _3MF_COMPRESS_LEVEL.

    Split out from the zip writing so it can be asserted on directly.
    """
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<model unit="millimeter" xml:lang="en-US" xmlns="{_3MF_CORE}" '
           f'xmlns:m="{_3MF_MATERIAL}">', "\t<resources>"]
    next_id = 1
    items = []
    for verts, tris, rgba in meshes:
        colour_attrs = ""
        if rgba is not None:
            cid = next_id
            next_id += 1
            r, g, b, a = (max(0, min(255, int(round(c * 255)))) for c in rgba)
            out.append(f'\t\t<m:colorgroup id="{cid}">')
            out.append(f'\t\t\t<m:color color="#{r:02X}{g:02X}{b:02X}{a:02X}" />')
            out.append("\t\t</m:colorgroup>")
            # One colour for the whole object, so it hangs off the object
            # rather than repeating on every triangle -- which is what
            # lib3mf collapsed our per-triangle properties into anyway.
            colour_attrs = f' pid="{cid}" pindex="0"'
        oid = next_id
        next_id += 1
        out.append(f'\t\t<object id="{oid}" type="model"{colour_attrs}>')
        out.append("\t\t\t<mesh>")
        out.append("\t\t\t\t<vertices>")
        out.extend(f'\t\t\t\t\t<vertex x="{_3mf_num(v[0])}" y="{_3mf_num(v[1])}" '
                   f'z="{_3mf_num(v[2])}" />' for v in verts)
        out.append("\t\t\t\t</vertices>")
        out.append("\t\t\t\t<triangles>")
        out.extend(f'\t\t\t\t\t<triangle v1="{int(t[0])}" v2="{int(t[1])}" '
                   f'v3="{int(t[2])}" />' for t in tris)
        out.append("\t\t\t\t</triangles>")
        out.append("\t\t\t</mesh>")
        out.append("\t\t</object>")
        items.append(oid)
    out.append("\t</resources>")
    out.append("\t<build>")
    out.extend(f'\t\t<item objectid="{i}" />' for i in items)
    out.append("\t</build>")
    out.append("</model>")
    return "\n".join(out) + "\n"


DEFAULT_COLOR = (0.8, 0.8, 0.8, 1.0)


def _manifold_arrays(man):
    import numpy as np

    m = man.to_mesh()
    return (np.asarray(m.vert_properties[:, :3], dtype=np.float32),
            np.asarray(m.tri_verts, dtype=np.int32))


def split_bodies_for_export(bodies, open_parts=None):
    """The implicit top-level union, cut into objects that never overlap.

    Returns [(verts, tris, rgba), ...] for the formats that can hold more
    than one object -- 3MF, OBJ, PLY. Three rules, in order:

    1. **Union, never concatenate.** Top level in OpenSCAD is an implicit
       union, so `cube(100); cube(100, center=true);` is ONE solid. Writing
       the two bodies as they arrive put each cube in the file separately,
       overlapping, with interior faces intact -- real OpenSCAD writes 36
       triangles for that script and this wrote 24. Same reasoning as
       merge_bodies_to_mesh's, which STL has always done.

    2. **One object per colour, and no two objects share volume.** Where
       differently-coloured solids overlap, the LATER one owns the shared
       volume and the earlier is notched around it -- painter's order, so
       `color("red") body(); color("blue") detail();` leaves the detail
       whole. Only the invisible interior is affected: the visible surface
       is identical either way, because whichever solid is outermost at a
       given face is what you see.

    3. **One object per connected component.** After 1 and 2, anything that
       falls into unconnected pieces is written as one object each.

    Bodies Manifold rejects (an open shell is not a solid) can't take part
    in any of that; they keep their own triangles and their own object, and
    their 1-based index lands in `open_parts` for the caller to warn about,
    exactly as merge_bodies_to_mesh does.
    """
    import manifold3d

    bodies = exportable(bodies)
    solids, loose = [], []
    for i, cb in enumerate(bodies):
        if cb.body.is_empty():
            continue
        v, t = body_mesh_arrays(cb)
        if len(t) == 0:
            continue
        man = manifold3d.Manifold(manifold3d.Mesh(v, t))
        if man.is_empty():
            loose.append((v, t.astype("int32"), cb.color or DEFAULT_COLOR))
            if open_parts is not None:
                open_parts.append(i + 1)
        else:
            solids.append((man, cb.color))

    out = []
    for man, color in _claim_by_colour(solids):
        # decompose() is the rule-3 split. A single-component solid comes
        # back as a one-element list, so there is no special case here.
        for part in man.decompose() or [man]:
            if part.is_empty():
                continue
            verts, tris = _manifold_arrays(part)
            if len(tris):
                out.append((verts, tris, color or DEFAULT_COLOR))
    out.extend(loose)
    return out


def _claim_by_colour(solids):
    """[(manifold, colour)] -- `solids` unioned, partitioned by colour so no
    two entries occupy the same space. Later entries win the overlap."""
    import manifold3d

    def add(parts):
        if len(parts) == 1:
            return parts[0]
        return manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)

    if not solids:
        return []
    colors = {c for _m, c in solids}
    if len(colors) == 1:
        # The common case by far, and it needs no per-body subtraction at
        # all: one colour cannot overlap itself into a different answer.
        return [(add([m for m, _c in solids]), solids[0][1])]

    # Reverse order + subtract-what-is-already-claimed is what makes the
    # LATER body win: by the time an earlier one is reached, everything
    # after it has already taken its volume.
    claimed = None
    owned = []
    for man, color in reversed(solids):
        piece = man if claimed is None else man - claimed
        claimed = man if claimed is None else claimed + man
        if not piece.is_empty():
            owned.append((piece, color))
    owned.reverse()

    # Same-coloured pieces merge into one object; distinct colours stay
    # apart. Insertion-ordered so object order still follows the source.
    grouped = {}
    for piece, color in owned:
        grouped.setdefault(color, []).append(piece)
    return [(add(parts), color) for color, parts in grouped.items()]




# Deflate level, chosen rather than inherited. Measured on the 224k-triangle
# Dalek, zip step only:
#
#     level 0   16.80 MB     3 ms      level 6    2.41 MB   314 ms
#     level 1    3.06 MB    71 ms      level 9    2.30 MB  1912 ms
#     level 3    2.85 MB   119 ms
#
# 6 is the knee: 9 costs six times the time for 5% more saving, and 1 --
# what lib3mf used, and the real reason its files came out larger than
# ours rather than anything about the XML -- gives back 27% of the size to
# save a quarter-second on an export nobody runs in a loop.
#
# The tab indentation and the space before each "/>" cost 2MB raw and 43KB
# (1.8%) compressed here. Deflate eats repeated tabs almost entirely, which
# is why the part can be readable when unzipped for nearly nothing. Do not
# "optimise" them away for size; that trade was made deliberately.
_3MF_COMPRESS_LEVEL = 6


def write_3mf(path: str, objects):
    """`objects` is split_bodies_for_export()'s [(verts, tris, rgba)] --
    same input write_obj/write_ply take, so all three multi-object writers
    agree on what "an object" is."""
    import zipfile

    xml = build_3mf_model_xml(objects)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=_3MF_COMPRESS_LEVEL) as z:
        z.writestr("[Content_Types].xml", _3MF_CONTENT_TYPES)
        z.writestr("_rels/.rels", _3MF_RELS)
        z.writestr("3D/3dmodel.model", xml)


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
