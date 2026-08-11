// color(): how a shape is drawn.
//
// Colour is a preview and export property, not a geometric one -- it
// changes nothing about the solid, and a boolean takes the colour of its
// first child.

color("tomato")    cube([20, 20, 8]);
color("steelblue") translate([25, 0, 0]) cube([20, 20, 8]);

// By name, by RGB, and with an alpha for transparency.
color([0.2, 0.8, 0.4])      translate([0, 25, 0]) cube([20, 20, 8]);
color([0.2, 0.8, 0.4, 0.35]) translate([25, 25, 0]) cube([20, 20, 8]);

// A colour applies to everything below it unless a child overrides it.
translate([0, 50, 0])
    color("gold") {
        cube([20, 20, 8]);
        color("purple") translate([25, 0, 0]) cube([20, 20, 8]);
    }
