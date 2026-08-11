// surface(): a height map as a solid.
//
// Reads a grid of numbers -- one per sample, rows on their own lines --
// and raises the surface to match, skirting it down to Z=0 to make a
// closed solid. It also reads images, taking brightness as height.
//
// The path is relative to this file.

surface(file="data/heightmap.dat");

// center=true puts the middle at the origin rather than a corner.
translate([0, 40, 0]) surface(file="data/heightmap.dat", center=true);

// invert=true flips high for low, turning the hill into a basin.
translate([40, 0, 0]) surface(file="data/heightmap.dat", invert=true);

// It is a solid, so it can be cut like one.
translate([40, 40, 0])
    intersection() {
        surface(file="data/heightmap.dat", center=true);
        cylinder(h=40, r=9, center=true, $fn=48);
    }
