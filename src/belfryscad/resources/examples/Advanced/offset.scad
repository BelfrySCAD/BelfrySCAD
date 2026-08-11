// offset(): grow or shrink a 2D outline.
//
// r rounds the corners it creates, delta keeps them sharp, and
// chamfer=true cuts them off instead. A negative value shrinks.

module blob() {
    polygon([[0,0], [40,0], [40,25], [22,25], [22,12], [0,12]]);
}

blob();
translate([55, 0])  offset(r=5, $fn=24)      blob();
translate([110, 0]) offset(delta=5)          blob();
translate([165, 0]) offset(delta=5, chamfer=true) blob();

// Shrinking, then subtracting, is how you get a wall of known thickness.
translate([0, -40])
    linear_extrude(8)
        difference() {
            blob();
            offset(delta=-2) blob();
        }
