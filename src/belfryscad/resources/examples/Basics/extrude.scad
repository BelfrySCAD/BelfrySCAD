// Turning 2D outlines into 3D.
//
// linear_extrude() pushes a shape along Z, optionally twisting or
// tapering. rotate_extrude() spins one around the Z axis.

module star(points=5, r1=18, r2=8) {
    polygon([
        for (i = [0 : 2 * points - 1])
            let (a = i * 180 / points, r = (i % 2 == 0) ? r1 : r2)
            [r * cos(a), r * sin(a)]
    ]);
}

linear_extrude(height=20, twist=90, scale=0.4, slices=40) star();

// A profile spun into a bowl.
translate([55, 0, 0])
    rotate_extrude($fn=64)
        translate([14, 0])
            difference() {
                circle(10, $fn=32);
                translate([-12, 0]) square([24, 24], center=true);
            }
