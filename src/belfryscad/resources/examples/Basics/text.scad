// text(): letterforms as a 2D shape.
//
// It is 2D, so it needs extruding to become a solid. size is the cap
// height in mm; halign/valign decide what the origin means.

linear_extrude(3) text("BelfrySCAD", size=10);

translate([0, -20, 0]) linear_extrude(3)
    text("centered", size=10, halign="center", valign="center");

// Cut lettering into a face rather than raising it.
translate([0, -50, 0])
    difference() {
        cube([90, 22, 6]);
        translate([45, 11, 4])
            linear_extrude(3)
                text("engraved", size=8, halign="center", valign="center");
    }

// spacing stretches the gaps; font picks a family and style.
translate([0, -75, 0]) linear_extrude(3)
    text("s p a c e d", size=8, spacing=1.4);
