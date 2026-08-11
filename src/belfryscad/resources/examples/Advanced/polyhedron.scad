// polyhedron(): every face spelled out by hand.
//
// Faces are listed clockwise seen from OUTSIDE the solid. Get one
// backwards and the mesh is inside out; leave a gap and it is not a
// solid at all. Faces may have more than three points, and need not be
// convex -- they are triangulated for you.

points = [
    [-15, -15, 0], [15, -15, 0], [15, 15, 0], [-15, 15, 0],   // base
    [0, 0, 25],                                                // apex
];

faces = [
    [0, 3, 2, 1],      // the base, one four-sided face
    [0, 1, 4],
    [1, 2, 4],
    [2, 3, 4],
    [3, 0, 4],
];

polyhedron(points=points, faces=faces);

// The same shape built from a VNF-style list, offset alongside.
translate([45, 0, 0])
    linear_extrude(height=25, scale=0)
        square(30, center=true);
