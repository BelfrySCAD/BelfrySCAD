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


def write_obj(path: str, mesh):
    import numpy as np

    verts = np.asarray(mesh.vert_properties[:, :3], dtype=np.float32)
    tris = np.asarray(mesh.tri_verts, dtype=np.int32)

    with open(path, "w", encoding="utf-8") as f:
        for v in verts:
            f.write(f"v {v[0]:.6g} {v[1]:.6g} {v[2]:.6g}\n")
        f.write("\n")
        for tri in tris:
            f.write(f"f {tri[0]+1} {tri[1]+1} {tri[2]+1}\n")


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

    Split out from the zip writing so it can be asserted on directly.
    """
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<model unit="millimeter" xml:lang="en-US" xmlns="{_3MF_CORE}" '
           f'xmlns:m="{_3MF_MATERIAL}">', "<resources>"]
    next_id = 1
    items = []
    for verts, tris, rgba in meshes:
        colour_attrs = ""
        if rgba is not None:
            cid = next_id
            next_id += 1
            r, g, b, a = (max(0, min(255, int(round(c * 255)))) for c in rgba)
            out.append(f'<m:colorgroup id="{cid}">'
                       f'<m:color color="#{r:02X}{g:02X}{b:02X}{a:02X}"/></m:colorgroup>')
            # One colour for the whole object, so it hangs off the object
            # rather than repeating on every triangle -- which is what
            # lib3mf collapsed our per-triangle properties into anyway.
            colour_attrs = f' pid="{cid}" pindex="0"'
        oid = next_id
        next_id += 1
        out.append(f'<object id="{oid}" type="model"{colour_attrs}><mesh><vertices>')
        out.extend(f'<vertex x="{_3mf_num(v[0])}" y="{_3mf_num(v[1])}" '
                   f'z="{_3mf_num(v[2])}"/>' for v in verts)
        out.append("</vertices><triangles>")
        out.extend(f'<triangle v1="{int(t[0])}" v2="{int(t[1])}" v3="{int(t[2])}"/>'
                   for t in tris)
        out.append("</triangles></mesh></object>")
        items.append(oid)
    out.append("</resources><build>")
    out.extend(f'<item objectid="{i}"/>' for i in items)
    out.append("</build></model>")
    return "\n".join(out)


def bodies_to_3mf_meshes(bodies):
    """[(verts, tris, rgba)] for every non-empty body worth writing."""
    import numpy as np

    meshes = []
    for cb in bodies:
        if cb.body.is_empty():
            continue
        m = cb.body.to_mesh()
        verts = np.asarray(m.vert_properties[:, :3], dtype=np.float32)
        tris = np.asarray(m.tri_verts, dtype=np.int32)
        if len(tris) == 0:
            continue
        meshes.append((verts, tris, cb.color or (0.8, 0.8, 0.8, 1.0)))
    return meshes


# Deflate level, chosen rather than inherited. Measured on the 224k-triangle
# Dalek, zip step only:
#
#     level 0   14.77 MB     2 ms      level 6    2.37 MB   284 ms
#     level 1    2.99 MB    65 ms      level 9    2.27 MB  1503 ms
#     level 3    2.82 MB   116 ms
#
# 6 is the knee: 9 costs 5x the time for 4% more saving, and 1 -- which is
# what lib3mf used, and the real reason its files came out larger than
# ours rather than anything about the XML -- gives back 26% of the size to
# save 0.2s on an export nobody does in a loop.
_3MF_COMPRESS_LEVEL = 6


def write_3mf(path: str, bodies):
    import zipfile

    xml = build_3mf_model_xml(bodies_to_3mf_meshes(exportable(bodies)))
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
