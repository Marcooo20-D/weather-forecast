# Repository cleanup plan (review before merging)

This repository powers an automated forecast workflow and a GitHub Pages website. **Do not delete or move files in `outputs/` without validating the deployment and its public links.**

## Verified current structure
- `.github/workflows/main.yml` runs on a two-hour schedule and can also be started manually.
- The workflow runs `weather_ensemble_multi_location.py` and `langit_v65_cinematic_rebuild.py` from the repository root.
- The workflow builds and verifies content in `outputs/`, commits generated results to `main`, and publishes `outputs/` to the `gh-pages` branch.
- `trading-dashboard/` contains JavaScript and HTML dashboard code.
- Multiple `langit_v6*` scripts coexist at the top level. Their continued use should be checked before archiving them.

## Safe sequence
1. Create a backup tag or independent clone before any cleanup.
2. Identify current public URLs and any links into `outputs/`.
3. Add tests and a dependency manifest for the Python scripts.
4. Review versioned `langit_*` files individually; keep the active version used by the workflow.
5. Compare identical-content generated files, but preserve public filenames until routing and deployment are tested.
6. Consider separating `trading-dashboard/` into its own repository **only after reviewing its dependencies**.
7. Reduce tracked generated data only in conjunction with a safe storage/Pages deployment migration.
8. Validate Actions, Pages, forecast output, and historical records before merging structural changes.

## Candidate cleanup, not approved deletion
- Generated `outputs/` files and dated CSV histories: may be required for Pages, accuracy assessment, or forecast history.
- `funcs.txt`, `check_fixes.py`, `build_utils/`, and older `langit_*` scripts: needs reference and maintenance review.
- Same-hash HTML files: identical content does not imply identical public URLs can be removed.

**No production file is removed by this plan.**
