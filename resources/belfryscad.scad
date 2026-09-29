$fn = 288;

color("yellow")
    difference() {
        cube(100, center=true);
        sphere(d=100*0.9*sqrt(2));
    }

color("cyan")
    sphere(d=100);

color("xkcd:light periwinkle")
    translate([0,-35,0])
    rotate([90,0,0])
        linear_extrude(height=20, center=false)
            text("B", size=50,
                font="Superclarendon",
                halign="center",
                valign="center");
