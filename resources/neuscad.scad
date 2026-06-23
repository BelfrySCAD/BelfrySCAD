$fn = 288;
difference() {
    cube(100, center=true);
    sphere(d=100*0.9*sqrt(2));
}
color("green") sphere(d=100);
