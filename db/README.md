# Database Artifacts (`db/`)

This directory is for local DuckDB files produced during load/evaluation workflows.

## Typical Files

- `edu_baseline.duckdb`: clean baseline database loaded from `./out_baseline`
- `edu_fragmented.duckdb`: fragmented target database loaded from `./out`

## Notes

- Treat files in this folder as generated artifacts.
- Baseline and fragmented runs should be loaded into separate DuckDB files when computing fragmentation scores.
- `../db.sh` loads both runs and evaluates `edu_fragmented.duckdb` against `edu_baseline.duckdb`.
