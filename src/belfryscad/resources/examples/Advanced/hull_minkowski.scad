// hull() wraps its children in the tightest convex shell.
// minkowski() sweeps one shape over another -- the usual way to round
// edges, and expensive enough to be worth keeping $fn low.

// A rounded slab, from eight spheres at the corners.
module rounded_box(size, r) {
    hull()
        for (x = [r, size[0] - r], y = [r, size[1] - r], z = [r, size[2] - r])
            translate([x, y, z]) sphere(r, $fn=24);
}

rounded_box([40, 30, 12], 4);

// The same idea with minkowski: a flat profile given a rounded edge.
translate([55, 0, 0])
    minkowski() {
        linear_extrude(4) square([30, 20], center=true);
        cylinder(h=2, r=3, $fn=16);
    }
