// The three boolean operations, side by side.
//
// union() fuses, difference() subtracts every later child from the first,
// and intersection() keeps only what all children share.

module demo(label) {
    color("black")
        translate([0, -10, -6])
            linear_extrude(0.01)
                text(label, size=3, halign="center");
    children();
}

translate([-30, 0, 0]) demo("union")
    union() {
        cube(12, center=true);
        sphere(8, $fn=48);
    }

demo("difference")
    difference() {
        cube(12, center=true);
        sphere(8, $fn=48);
    }

translate([30, 0, 0]) demo("intersection")
    intersection() {
        cube(12, center=true);
        sphere(8, $fn=48);
    }
