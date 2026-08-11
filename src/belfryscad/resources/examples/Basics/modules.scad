// Modules: your own shapes, with parameters and defaults.

module rounded_plate(size=[40, 25, 4], r=5) {
    hull()
        for (x = [r - size[0]/2, size[0]/2 - r],
             y = [r - size[1]/2, size[1]/2 - r])
            translate([x, y, 0])
                cylinder(h=size[2], r=r, $fn=32);
}

module bolt_holes(size, inset=6, d=4) {
    for (x = [inset - size[0]/2, size[0]/2 - inset],
         y = [inset - size[1]/2, size[1]/2 - inset])
        translate([x, y, -1])
            cylinder(h=size[2] + 2, d=d, $fn=24);
}

plate = [60, 40, 5];
difference() {
    rounded_plate(plate, r=8);
    bolt_holes(plate);
}
