// Recursion, the only kind of loop a function has.
//
// Each call must reduce the problem, and every branch must return -- a
// function that falls off the end yields undef.

function factorial(n) = n <= 1 ? 1 : n * factorial(n - 1);
echo(str("10! = ", factorial(10)));

function sum(v, i=0) = i >= len(v) ? 0 : v[i] + sum(v, i + 1);
echo(str("sum = ", sum([1, 2, 3, 4, 5])));

// A module can recurse too, and that is how you build a tree.
module branch(len, depth) {
    cylinder(h=len, r1=len/12, r2=len/16, $fn=12);
    if (depth > 0)
        translate([0, 0, len])
            for (a = [-35, 35])
                rotate([a, 0, depth * 60])
                    branch(len * 0.72, depth - 1);
}

branch(30, 4);
