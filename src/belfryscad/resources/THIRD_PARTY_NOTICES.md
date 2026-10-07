# Third-party notices

BelfrySCAD is MIT licensed (see LICENSE). Parts of it are derived from the
following third-party works, under the licenses reproduced here.

## BOSL2

<https://github.com/BelfrySCAD/BOSL2>

Ported from BOSL2, so the data viewers draw exactly what the library builds:

- `nurbs.py` -- NURBS curve evaluation and interpolation, from `nurbs.scad`.
- `window/data_viewer_grid.py` -- the grid-to-surface faceting, from
  `vnf_vertex_array()` in `vnf.scad`, and NURBS patch sampling, from `nurbs.scad`.
- `vnf_tile.py` -- the texture tile seam check, from BOSL2's texture code.

```
BSD 2-Clause License

Copyright (c) 2017-2019, Revar Desmera
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
