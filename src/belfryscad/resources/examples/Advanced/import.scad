// import(): bring in a mesh made somewhere else.
//
// STL, OBJ, OFF, 3MF and more. The path is relative to THIS file, so an
// example can ship its own data next to it.
//
// An imported mesh is ordinary geometry: it can be transformed, and it
// can take part in booleans -- provided it is a closed solid. A mesh
// with holes in it will draw, but a boolean on it cannot work.

import("data/knob.stl");

// Transformed like anything else.
translate([45, 0, 0]) rotate([0, 25, 0]) import("data/knob.stl");

// And cut, like anything else -- provided the cutter leaves something
// behind. A box that swallows the whole import gives an empty result,
// which is a silent nothing rather than an error.
translate([90, 0, 0])
    difference() {
        import("data/knob.stl");
        translate([0, 0, -1]) cube([20, 20, 12]);
    }

// convexity= is a preview hint for concave meshes -- it does not change
// the geometry, only how many layers the preview draws through.
translate([135, 0, 0]) import("data/knob.stl", convexity=4);
