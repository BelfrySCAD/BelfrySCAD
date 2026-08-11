// multmatrix(): the transform every other transform is made of.
//
// A 4x3 (or 4x4) matrix: the left 3x3 rotates, scales and shears, the
// last column translates. Useful when you have a matrix already, and the
// only way to shear at all.

module marker() {
    cube([20, 20, 4]);
    color("tomato") cube([20, 2, 12]);
}

marker();

// Translation, spelled out: the same as translate([30, 0, 0]).
multmatrix([[1, 0, 0, 30],
            [0, 1, 0,  0],
            [0, 0, 1,  0]]) marker();

// A shear -- x leaning with y. No other transform will do this.
translate([0, 35, 0])
    multmatrix([[1, 0.6, 0, 0],
                [0, 1,   0, 0],
                [0, 0,   1, 0]]) marker();

// Rotation about Z, written out, the same as rotate([0, 0, 30]).
translate([60, 35, 0])
    multmatrix([[cos(30), -sin(30), 0, 0],
                [sin(30),  cos(30), 0, 0],
                [0,        0,       1, 0]]) marker();
