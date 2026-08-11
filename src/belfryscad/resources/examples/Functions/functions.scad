// Functions return values; modules make geometry.
//
// A function body is a single expression -- there are no statements in
// one -- which is why let() and the conditional operator do the work.

function mm_per_inch() = 25.4;
function inches(x) = x * mm_per_inch();

function lerp(a, b, t) = a + (b - a) * t;

function bezier(p0, p1, p2, p3, t) =
    let (u = 1 - t)
    u*u*u*p0 + 3*u*u*t*p1 + 3*u*t*t*p2 + t*t*t*p3;

echo(str("half an inch is ", inches(0.5), " mm"));

// Sample the curve and drop a sphere at each point.
for (i = [0 : 20])
    let (t = i / 20)
        translate([bezier(0, 20, 40, 60, t), bezier(0, 40, -40, 0, t), 0])
            sphere(2, $fn=16);
