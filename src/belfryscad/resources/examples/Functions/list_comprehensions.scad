// Building lists: for, if and each inside [ ].

squares = [for (i = [1 : 6]) i * i];
echo(squares);

// `if` filters; there is no else needed to skip a value.
evens = [for (i = [1 : 20]) if (i % 2 == 0) i];
echo(evens);

// `each` flattens one level, so a nested generator does not nest the list.
pairs = [for (i = [1 : 3]) each [i, -i]];
echo(pairs);

// The usual reason to want them: generating a polygon.
function ring(n, r) = [for (i = [0 : n - 1]) let (a = i * 360 / n)
                       [r * cos(a), r * sin(a)]];

linear_extrude(4) polygon(ring(7, 20));
translate([50, 0, 0]) linear_extrude(4) polygon(ring(24, 20));
