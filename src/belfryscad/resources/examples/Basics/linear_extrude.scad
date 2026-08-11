// linear_extrude(): push a 2D outline along Z.
//
// twist turns it as it rises, scale tapers it, and slices decides how
// many rings the sides are cut into -- too few and a twist looks faceted.

module star(points=5, r1=18, r2=8) {
    polygon([
        for (i = [0 : 2 * points - 1])
            let (a = i * 180 / points, r = (i % 2 == 0) ? r1 : r2)
            [r * cos(a), r * sin(a)]
    ]);
}

// Straight up.
linear_extrude(height=15) star();

// Twisted, and tapered to a point.
translate([50, 0, 0]) linear_extrude(height=25, twist=120, scale=0.2, slices=48) star();

// center=true straddles Z=0 instead of sitting on it.
translate([100, 0, 0]) linear_extrude(height=15, center=true) star();
