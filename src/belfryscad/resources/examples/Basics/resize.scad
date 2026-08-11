// resize(): scale a shape to a measured size.
//
// scale() multiplies; resize() sets the bounding box outright, which is
// the one you want when the answer has to come out a particular width.

sphere(10, $fn=48);

// Stretched to 40 x 20 x 10, whatever it started as.
translate([45, 0, 0]) resize([40, 20, 10]) sphere(10, $fn=48);

// A zero means "leave this axis alone".
translate([100, 0, 0]) resize([40, 0, 0]) sphere(10, $fn=48);

// auto=true scales the untouched axes to match, keeping the proportions.
translate([150, 0, 0]) resize([40, 0, 0], auto=true) sphere(10, $fn=48);
