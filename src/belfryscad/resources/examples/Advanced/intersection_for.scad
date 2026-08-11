// intersection_for(): intersect every pass of a loop.
//
// A plain for() unions its passes. This one keeps only what they all
// have in common -- and it exists because you cannot write
// intersection() { for (...) ... }, which would intersect the single
// union that the for produced.

// Rotating a slot around the origin leaves the shape common to them all.
intersection_for (a = [0 : 30 : 179])
    rotate([0, 0, a])
        cube([60, 18, 10], center=true);

// The same idea in 3D: spheres from several directions, whittling a
// solid down to their shared core.
translate([80, 0, 0])
    intersection_for (v = [[1,0,0], [0,1,0], [0,0,1], [1,1,1]])
        translate(15 * v / norm(v))
            sphere(20, $fn=32);
