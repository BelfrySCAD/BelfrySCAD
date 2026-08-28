// A gyroid: a surface defined by a formula, not by primitives.
//
// levelset() takes a function of x, y, z and builds the surface where it
// crosses a value. Giving isovalue a [low, high] range instead of a single
// number returns the material *between* the two, so the result is a shell
// of real thickness rather than a zero-thickness surface.
//
// levelset() is a BelfrySCAD extension -- it is not in OpenSCAD.

size = 100;       // overall cube size
wavelength = 67;  // one gyroid cell period
thickness = 0.25; // wall thickness, as isovalue half-range
voxel = 2.0;      // sampling resolution (smaller = smoother and slower)

// The gyroid's defining equation. Angles are in degrees here, so the
// period scales to `wl` by converting the position to degrees first.
function gyroid(x, y, z, wl) =
    let (p = 360/wl * [x,y,z])
    sin(p.x)*cos(p.y) + sin(p.y)*cos(p.z) + sin(p.z)*cos(p.x);

levelset(
    function (x,y,z) gyroid(x, y, z, wavelength),
    bounds = [-[size,size,size]/2, [size,size,size]/2],
    isovalue = [-thickness, thickness],
    edge = voxel);

// `edge` is the one to watch: cost grows with the CUBE of the resolution,
// so halving it makes this roughly eight times slower. The surface is cut
// flat where it meets `bounds`, so the block has clean faces, not torn ones.
