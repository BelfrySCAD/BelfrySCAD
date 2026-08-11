// Modules that operate on whatever you hand them.
//
// children() stands for the block passed to the module; $children is how
// many there are. This is how BOSL2-style layout modules work.

module ring_of(n=6, r=30) {
    for (i = [0 : n - 1])
        rotate([0, 0, i * 360 / n])
            translate([r, 0, 0])
                children();
}

// Every child is placed at every position.
ring_of(8, 30) {
    cube(8, center=true);
    translate([0, 0, 6]) sphere(3, $fn=24);
}

// Picking children by index: the first is the base, the rest stack up.
module stacked() {
    children(0);
    for (i = [1 : $children - 1])
        translate([0, 0, i * 6]) children(i);
}

translate([0, 0, 20]) stacked() {
    cylinder(h=4, r=10, $fn=32);
    cylinder(h=4, r=7,  $fn=32);
    cylinder(h=4, r=4,  $fn=32);
}
