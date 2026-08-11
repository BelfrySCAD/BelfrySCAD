// hull(): the tightest convex shell around its children.
//
// Nothing concave survives it, which is the point: a few small shapes
// become one smooth body.

// A rounded slab, from eight corner spheres.
module rounded_box(size, r) {
    hull()
        for (x = [r, size[0] - r], y = [r, size[1] - r], z = [r, size[2] - r])
            translate([x, y, z]) sphere(r, $fn=24);
}
rounded_box([40, 30, 12], 4);

// A stadium: two circles, one hull.
translate([55, 0, 0]) linear_extrude(8)
    hull() {
        circle(8, $fn=32);
        translate([30, 0]) circle(8, $fn=32);
    }

// Hulling each neighbouring pair, rather than all at once, follows a
// path instead of swallowing it.
translate([0, 45, 0]) {
    pts = [for (i = [0:5]) [i * 18, 12 * sin(i * 60)]];
    for (i = [0 : len(pts) - 2])
        hull() {
            translate(pts[i])     cylinder(h=6, r=4, $fn=24);
            translate(pts[i + 1]) cylinder(h=6, r=4, $fn=24);
        }
}
