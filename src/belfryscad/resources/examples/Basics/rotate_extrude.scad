// rotate_extrude(): spin a 2D profile around the Z axis.
//
// The profile must sit at positive X -- it is swept around X=0, so
// anything to the left of the axis would pass through itself.

// A bowl: a circle with its inner half cut away, swept round.
rotate_extrude($fn=64)
    translate([18, 0])
        difference() {
            circle(10, $fn=32);
            translate([-12, 0]) square([24, 24], center=true);
        }

// angle= sweeps part of the way, which is how you get a pipe elbow.
translate([60, 0, 0])
    rotate_extrude(angle=120, $fn=64)
        translate([20, 0])
            circle(5, $fn=24);
