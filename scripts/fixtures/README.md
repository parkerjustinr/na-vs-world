# Test fixtures (synthetic)

Hand-written responses in the shape of Leaguepedia Cargo API responses, used by
`test_pull_leaguepedia.py` to test parsing offline. **The results are made up**
(only the 2026-07-11 LYON vs Hanwha Life Esports series follows the real
2-3 score in `data/counter.json`). Never write fixture output to `data/`.

Each `<name>.json` answers one named query in the script (`tournaments`,
`rosters`, `league_rosters_2025`, `matches`, `games`). `cargofields.json` lists
the fields each table reports.
