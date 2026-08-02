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


def write_3mf(path: str, bodies):
    import lib3mf
    import numpy as np

    _FA3 = type(lib3mf.Position().Coordinates)
    _UI3 = type(lib3mf.Triangle().Indices)

    def _identity_transform():
        t = lib3mf.Transform()
        _col = type(t.Fields[0])
        t.Fields[0] = _col(1, 0, 0)
        t.Fields[1] = _col(0, 1, 0)
        t.Fields[2] = _col(0, 0, 1)
        t.Fields[3] = _col(0, 0, 0)
        return t

    wrapper = lib3mf.Wrapper()
    model = wrapper.CreateModel()

    for colored_body in bodies:
        if colored_body.body.is_empty():
            continue

        mesh3d = colored_body.body.to_mesh()
        verts = np.asarray(mesh3d.vert_properties[:, :3], dtype=np.float32)
        tris = np.asarray(mesh3d.tri_verts, dtype=np.int32)
        if len(tris) == 0:
            continue

        mesh_obj = model.AddMeshObject()

        positions = []
        for v in verts:
            p = lib3mf.Position()
            p.Coordinates = _FA3(float(v[0]), float(v[1]), float(v[2]))
            positions.append(p)

        triangles = []
        for t in tris:
            tri = lib3mf.Triangle()
            tri.Indices = _UI3(int(t[0]), int(t[1]), int(t[2]))
            triangles.append(tri)

        mesh_obj.SetGeometry(positions, triangles)

        rgba = colored_body.color or (0.8, 0.8, 0.8, 1.0)
        cg = model.AddColorGroup()
        c = lib3mf.Color()
        c.Red   = max(0, min(255, int(rgba[0] * 255)))
        c.Green = max(0, min(255, int(rgba[1] * 255)))
        c.Blue  = max(0, min(255, int(rgba[2] * 255)))
        c.Alpha = max(0, min(255, int(rgba[3] * 255)))
        color_id = cg.AddColor(c)
        cg_uid = cg.GetUniqueResourceID()

        props = []
        for _ in range(len(tris)):
            tp = lib3mf.TriangleProperties()
            tp.ResourceID = cg_uid
            tp.PropertyIDs = _UI3(color_id, color_id, color_id)
            props.append(tp)
        mesh_obj.SetAllTriangleProperties(props)

        model.AddBuildItem(mesh_obj, _identity_transform())

    writer = model.QueryWriter("3mf")
    writer.WriteToFile(path)


def merge_bodies_to_mesh(bodies):
    """STL/OBJ are triangle soups -- merging the already-final body meshes is
    plain index-offset concatenation (no CSG needed). Returns None if every
    body is empty."""
    import numpy as np
    from types import SimpleNamespace

    parts_v, parts_t, voff = [], [], 0
    for b in bodies:
        if b.body.is_empty():
            continue
        m = b.body.to_mesh()
        v = np.asarray(m.vert_properties[:, :3], dtype=np.float32)
        t = np.asarray(m.tri_verts, dtype=np.int64) + voff
        parts_v.append(v)
        parts_t.append(t)
        voff += len(v)
    if not parts_v:
        return None
    return SimpleNamespace(vert_properties=np.vstack(parts_v), tri_verts=np.vstack(parts_t))
