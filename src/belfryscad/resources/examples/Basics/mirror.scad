// mirror(): reflect across a plane through the origin.
//
// The argument is the plane's normal, not an axis to flip about. The
// shape is reflected, not moved -- put it away from the origin first if
// you want to see both halves.

module wedge() {
    linear_extrude(6) polygon([[0,0], [25,0], [25,10], [10,18]]);
}

wedge();
color("steelblue") mirror([1, 0, 0]) wedge();

// Mirroring twice about different planes gets you a rotation, not a
// third copy -- reflections compose.
translate([0, 35, 0]) {
    wedge();
    color("tomato") mirror([0, 1, 0]) wedge();
    color("gold")   mirror([1, 0, 0]) mirror([0, 1, 0]) wedge();
}
