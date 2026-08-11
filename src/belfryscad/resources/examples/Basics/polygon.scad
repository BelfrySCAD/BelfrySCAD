// polygon(): a 2D shape from a list of points.
//
// Points go round the outline in order. A second list of paths turns
// some of them into holes -- each path names its own points by index.

// A simple outline.
polygon([[0,0], [30,0], [30,12], [18,12], [18,25], [0,25]]);

// With a hole: two paths, the first the outside, the second the hole.
translate([45, 0]) {
    outer = [[0,0], [30,0], [30,25], [0,25]];
    hole  = [[8,8], [22,8], [22,17], [8,17]];
    polygon(points=concat(outer, hole),
            paths=[[0,1,2,3], [4,5,6,7]]);
}

// A 2D shape has no thickness. Extrude it to see it as a solid.
translate([0, -40]) linear_extrude(5)
    polygon([[0,0], [30,0], [15,25]]);
