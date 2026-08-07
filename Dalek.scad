// dalek.scad -- Exterminate.
// Front of the model faces -Y.

/* [Resolution] */

// Vertical facets of the skirt and belt
panels = 16; // [8:2:32]

// Curve smoothness: max angle per fragment
facet_angle = 3; // [1:12]

// Curve smoothness: max fragment size
facet_size = 0.6; // [0.2:0.1:2]

/* [Skirt] */

// Height of the skirt, floor to belt
skirt_h = 60; // [30:120]

// Skirt radius at the floor
skirt_r1 = 42; // [20:70]

// Skirt radius at the belt
skirt_r2 = 30; // [15:60]

// Radius of the skirt hemispheres
hemi_r = 5; // [2:0.5:10]

// Rows of hemispheres up the skirt
hemi_rows = 4; // [2:6]

// Height of the base fender
fender_h = 5; // [0:15]

// Radius of the base fender
fender_r = 44; // [20:80]

/* [Body] */

// Height of the belt flange
belt_h = 7; // [2:20]

// Belt radius; should overhang the shoulder drum below it
belt_r = 35; // [20:70]

// Height of the shoulder section
sh_h = 26; // [10:50]

// Shoulder radius at the belt
sh_r1 = 31; // [15:60]

// Shoulder radius at the neck
sh_r2 = 26; // [15:60]

// Height of the neck cage
neck_h = 18; // [8:40]

/* [Head] */

// Radius of the dome
dome_r = 23; // [10:40]

// Eyestalk length, measured from the dome centre, so dome_r of it is
// swallowed before any becomes visible
eye_len = 46; // [25:80]

// Tilt of the eyestalk; 90 is dead level
eye_tilt = 82; // [60:110]

// Small discs ringing the face of the eye
eye_studs = 12; // [0:24]

// Radius of the dome lamp bulbs
lamp_r = 4.2; // [2:0.2:7]

// Height of each lamp up the dome, as a fraction of its radius
lamp_height = 0.70; // [0.3:0.05:0.9]

// Angle of each lamp either side of dead ahead
lamp_splay = 35; // [10:70]

/* [Arms] */

// Where the arms leave the shoulder, as a fraction of its height
arm_height = 0.6; // [0.2:0.05:0.9]

// Splay of each arm away from straight ahead, in degrees
arm_splay = 25; // [0:45]

// Length of the gun stick barrel
gun_len = 36; // [20:60]

// Length of the plunger arm shaft
plunger_len = 30; // [15:50]

/* [Hidden] */

$fa = facet_angle;
$fs = facet_size;

neck_r = dome_r + 1.5; // collar must be wider than the dome it carries

belt_z = skirt_h;
sh_z   = belt_z + belt_h;
neck_z = sh_z + sh_h;
dome_z = neck_z + neck_h;

ring_h = 3;   // thickness of each neck ring

// Seat each lamp on the dome surface at its chosen height, rather than
// guessing a radius that may float off it or sink into it.
lamp_z = dome_r * lamp_height;
lamp_x = sqrt(dome_r * dome_r - lamp_z * lamp_z) - 1;

// ---- base fender, vented with slots on each facet ----
module fender() {
    difference() {
        cylinder(h = fender_h, r = fender_r, $fn = panels);
        for (i = [0 : panels - 1])
            rotate([0, 0, i * 360 / panels + 180 / panels])
                translate([fender_r, 0, fender_h / 2])
                    cube([4, 7, fender_h * 0.55], center = true);
    }
}

// ---- skirt ----
module skirt() {
    cylinder(h = skirt_h, r1 = skirt_r1, r2 = skirt_r2, $fn = panels);
    for (row = [0 : hemi_rows - 1]) {
        z  = skirt_h * (row + 0.7) / (hemi_rows + 0.4);
        rz = skirt_r1 + (skirt_r2 - skirt_r1) * z / skirt_h;
        rf = rz * cos(180 / panels);
        for (i = [0 : panels - 1])
            rotate([0, 0, i * 360 / panels + 180 / panels])
                translate([rf, 0, z])
                    sphere(r = hemi_r);
    }
}

// ---- belt / flange between skirt and shoulders ----
module belt() {
    translate([0, 0, belt_z])
        cylinder(h = belt_h, r = belt_r, $fn = panels);
}

// ---- shoulders: tapered drum with horizontal slats ----
module slat(z, r) {
    translate([0, 0, z])
        rotate_extrude()
            translate([r, 0])
                circle(r = 2.2);
}

module shoulders() {
    translate([0, 0, sh_z])
        cylinder(h = sh_h, r1 = sh_r1, r2 = sh_r2, $fn = panels);
    for (i = [0 : 2]) {
        z = sh_z + sh_h * (i + 0.8) / 4;
        r = sh_r1 + (sh_r2 - sh_r1) * (z - sh_z) / sh_h;
        slat(z, r + 0.5);
    }
}

// ---- neck: three rings, vertical struts, and inset cross-rails
//      so the two openings read as mesh rather than a picket fence ----
module neck() {
    ring_gap = (neck_h - ring_h) / 2;   // pitch between ring bottoms

    for (i = [0 : 2])
        translate([0, 0, neck_z + i * ring_gap])
            cylinder(h = ring_h, r = neck_r);

    for (i = [0 : 11])
        rotate([0, 0, i * 30])
            translate([neck_r - 1, 0, neck_z + neck_h / 2])
                cube([2.2, 2.2, neck_h], center = true);

    // one rail across the middle of each opening, set in from the rings
    for (i = [0 : 1])
        translate([0, 0, neck_z + i * ring_gap + (ring_gap + ring_h) / 2 - 0.6])
            difference() {
                cylinder(h = 1.2, r = neck_r - 0.8);
                translate([0, 0, -1])
                    cylinder(h = 3.2, r = neck_r - 2.6);
            }
}

// ---- dome, eyestalk, lamps ----
module eyestalk() {
    translate([0, 0, dome_z + dome_r * 0.40])
        rotate([eye_tilt, 0, 0]) {
            cylinder(h = eye_len, r = 2);
            translate([0, 0, eye_len]) cylinder(h = 6, r = 6);
            translate([0, 0, eye_len + 6]) sphere(r = 3.2);   // lens
            for (i = [0 : eye_studs - 1])
                rotate([0, 0, i * 360 / eye_studs])
                    translate([4.4, 0, eye_len + 6])
                        cylinder(h = 1.2, r = 0.9);
        }
}

module lamp() {
    cylinder(h = lamp_r * 1.8, r = lamp_r * 0.5);
    translate([0, 0, lamp_r * 1.8]) sphere(r = lamp_r);
}

module dome() {
    // Cut exactly at the equator: the cube spans -2r..0, never above it,
    // and the dome sits 0.5 into the collar so the join isn't coplanar.
    translate([0, 0, dome_z - 0.5])
        difference() {
            sphere(r = dome_r);
            translate([0, 0, -dome_r])
                cube([2 * dome_r + 2, 2 * dome_r + 2, 2 * dome_r], center = true);
        }
    eyestalk();
    for (s = [-1, 1])
        rotate([0, 0, s * lamp_splay - 90])
            translate([lamp_x, 0, dome_z + lamp_z])
                rotate([0, 30, 0]) lamp();
}

// ---- arms ----
module gun() {
    cylinder(h = gun_len, r = 1.6);
    for (i = [0 : 5])
        translate([0, 0, 8 + i * 5])
            rotate_extrude() translate([3, 0]) circle(r = 1);
}

module plunger() {
    cylinder(h = plunger_len, r = 2.2);
    translate([0, 0, plunger_len])
        difference() {
            cylinder(h = 8, r1 = 3.5, r2 = 7);
            translate([0, 0, 2]) cylinder(h = 8, r1 = 2, r2 = 6);
        }
}

// part < 0 -> plunger arm, otherwise the gun stick.
module arm(part) {
    z = sh_z + sh_h * arm_height;
    r = sh_r1 + (sh_r2 - sh_r1) * arm_height;
    translate([0, 0, z])
        rotate([0, 0, part < 0 ? -arm_splay : arm_splay])
            translate([0, -r + 2, 0])
                rotate([90, 0, 0]) {
                    cylinder(h = 6, r = 5);   // shoulder ball joint
                    if (part < 0) plunger(); else gun();
                }
}

// ---- assembly ----
fender();
skirt();
belt();
shoulders();
neck();
dome();
arm(-1);
arm(1);
