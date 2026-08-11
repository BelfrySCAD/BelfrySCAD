// minkowski(): sweep one shape over another.
//
// The usual reason is rounding an edge, and the usual mistake is a
// costly one: the work grows with the facet count of BOTH shapes, so
// keep $fn low on the sweeping shape.

// A plate with its top and side edges rounded, by sweeping a small
// cylinder over a slab.
minkowski() {
    cube([40, 25, 6], center=true);
    cylinder(h=3, r=4, $fn=16);
}

// Sweeping a sphere rounds every edge at once, and costs the most.
translate([70, 0, 0])
    minkowski() {
        cube([30, 20, 6], center=true);
        sphere(3, $fn=12);
    }

// For a 2D outline, offset() does the same job in one cheap step --
// prefer it over minkowski() with a circle.
translate([130, 0, 0]) linear_extrude(6)
    offset(r=4, $fn=24) square([30, 20], center=true);
