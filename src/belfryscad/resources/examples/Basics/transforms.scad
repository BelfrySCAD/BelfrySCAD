// Moving, turning and scaling.
//
// Transformations apply to the child that follows them, and they nest:
// the rotate() below is applied before the translate() outside it.

for (i = [0 : 5])
    translate([i * 14, 0, 0])
        rotate([0, 0, i * 15])
            scale([1, 1, 1 + i * 0.4])
                cube([10, 10, 4], center=true);

// mirror() reflects across a plane through the origin.
translate([0, 25, 0]) {
    cylinder(h=10, r1=6, r2=0, $fn=32);
    mirror([0, 0, 1]) cylinder(h=10, r1=6, r2=0, $fn=32);
}
