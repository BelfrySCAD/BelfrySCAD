Build, commit, and run BelfrySCAD locally. Steps:

1. Review all files changed since the last commit (`git diff`). For each changed file under `docs/` or `src/`, check that the corresponding documentation in `docs/` accurately reflects the current code. Update any docs that are stale or missing coverage for the changes.

2. Stage and commit all changes. Follow the project's commit conventions: short imperative subject line summarizing what changed, optional body explaining why. Include the `Co-Authored-By` trailer.

3. Run `uv run python -m briefcase update macOS app -r` to sync source and requirements into the build tree.

4. Run `uv run python -m briefcase build macOS app` to compile the app bundle.

5. Run `uv run python -m briefcase run macOS app` to launch the app.

If any step fails, stop and report the error before continuing.
