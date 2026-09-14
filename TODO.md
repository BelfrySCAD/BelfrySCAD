# TODO

- NURBS viewer/editor support
- VNF tile texture viewer/editor

- Move BOSL2's `Regressions` job to `belfryscad --test`. It is the last
  thing in that repo still downloading the OpenSCAD 2021.01 AppImage;
  `--docsgen` and `--mdimggen` took over `CheckDocs` and `CheckTutorials`
  in BOSL2 #2034. All 909 of its tests now pass on this evaluator (exit 0,
  ~82s serial, against openscad-test's 8.3s for 226 with 5-way
  parallelism), so the swap is a workflow edit rather than a porting job:
  drop the AppImage, `libfuse2` and `pip install openscad-test`, and run
  `belfryscad --test tests/*.scadtest`. Needs belfryscad with
  openscad_cpp_evaluator >= 1.3.0.

- Accelerate textured extrusions.
