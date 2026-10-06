// Finding out which part of a model is slow.
//
// profile_time("label") { ... } times everything inside it and prints a
// PROFILE line in the console: the total, split into the time its script
// code ran and the time its geometry took to build. Put
// profile_time("label") in front of an expression to time that instead;
// the assignment still gets the expression's value.
//
// Render, then render again: unchanged geometry is reused the second time,
// and its PROFILE line says "geometry cached" instead of a time.
//
// profile_time() is a BelfrySCAD extension -- OpenSCAD cannot parse it.

// --- Timing an expression: two ways to compute the same number ---

// Recomputes the same values over and over: exponential time.
function fib_slow(n) = n < 2 ? n : fib_slow(n - 1) + fib_slow(n - 2);
// Carries the last two values along: linear time.
function fib_fast(n, a = 0, b = 1) = n == 0 ? a : fib_fast(n - 1, b, a + b);

slow = profile_time("fib_slow(22)") fib_slow(22);
fast = profile_time("fib_fast(22)") fib_fast(22);
echo(slow = slow, fast = fast);

// --- Timing geometry: what smoothness costs ---

// The same plate with the same holes. Only $fn differs -- each hole gets
// 16 sides in the first and 128 in the second, and every extra side is
// more for difference() to cut.
module plate(fn)
    difference() {
        cube([60, 30, 4]);
        for (x = [6 : 6 : 54], y = [7.5, 22.5])
            translate([x, y, -1]) cylinder(h = 6, r = 2, $fn = fn);
    }

profile_time("plate, $fn = 16") plate(16);
translate([70, 0, 0])
    profile_time("plate, $fn = 128") plate(128);

// --- Nesting: each block reports, and the outer one includes the inner ---

translate([0, 40, 0])
    profile_time("all pegs")
        for (i = [0 : 4])
            translate([i * 15, 0, 0])
                profile_time(str("peg ", i))
                    cylinder(h = 5 + 3 * i, r = 4, $fn = 16 * (i + 1));
