// projection(): flatten a 3D solid back to 2D.
//
// cut=false (the default) casts the whole shape's shadow onto the XY
// plane. cut=true takes the actual slice sitting at Z=0 -- so it only
// gives you something if there is material there, and an empty result
// is a silent nothing rather than an error.

module knobbly() {
    difference() {
        sphere(18, $fn=48);
        translate([0, 0, 10]) cube([40, 40, 20], center=true);
        cylinder(h=50, r=6, center=true, $fn=32);
    }
}

knobbly();

// The outline of everything, seen from above.
translate([50, 0, 0]) linear_extrude(1) projection() knobbly();

// The cross-section at Z=0: the sphere's widest ring, with the bore
// through the middle of it.
translate([100, 0, 0]) linear_extrude(1) projection(cut=true)
    difference() {
        sphere(18, $fn=48);
        cylinder(h=50, r=6, center=true, $fn=32);
    }

// To slice somewhere else, move the shape rather than the plane. Lifting
// it by 6 brings a higher cross-section down to Z=0.
translate([150, 0, 0]) linear_extrude(1)
    projection(cut=true) translate([0, 0, 6]) knobbly();
