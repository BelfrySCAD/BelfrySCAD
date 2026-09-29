$fn = 288;
difference() {
    cube(100, center=true);
    sphere(d=100*0.9*sqrt(2));
}
color("green") sphere(d=100);
color("lightblue")
    rotate([90,0,0])
        linear_extrude(height=50, center=false)
            text("B", size=50, font="Georgia", halign="center", valign="center");
