# High-Ed Data Generator

Synthetic higher-ed administrative data generator (Rust) plus a DuckDB connector/scoring toolkit (Python) for fragmentation evaluation.

## What This Repo Contains

- `src/`: Rust generator code for schema-aligned synthetic data.
- `connect/`: Python tools to load generated files into DuckDB and compute query-level fragmentation scores.
- `higher_ed_schema.md`: Source-of-truth schema contract.
- `connector.md`: Fragmentation scoring specification (`JML`, `CCL`, `MNS`, `RTL`, `SBL`, `STL`).
- `run.sh`: Main generation entrypoint with knobs.
- `build.sh`: Rust build helper.
- `db.sh`: Example load + evaluate pipeline script.

## Prerequisites

- Rust toolchain (`cargo`) for generation.
- Python 3.10+ for connector tools.

The repository root now uses a standard `Cargo.toml` workspace file, so `cargo build` and `cargo run`
work directly from the project root.

Shell entrypoints do not auto-install Python packages unless you opt in with
`AUTO_INSTALL_PY_DEPS=true` or `AUTO_INSTALL_TEXT_TO_SQL_DEPS=true`.

## Refactor Verification

Run the refactor-only guard before changing shell entrypoints:

```bash
scripts/verify-refactor.sh
```

It checks the standard `Cargo.toml` workspace file, shared shell runtime usage, strict shell syntax, and explicit opt-in dependency installation behavior.

## Generate Data

Run with defaults (generates both runs):

```bash
./run.sh
```

If you want the script to bootstrap Python requirements on demand:

```bash
AUTO_INSTALL_PY_DEPS=true ./run.sh
```

Default outputs:

- Fragmented run: `./out`
- Clean baseline run: `./out_baseline`

Override knobs inline:

```bash
STUDENTS=1000 \
SCHEMA_VERSION=both \
MISSINGNESS_PATTERN=mar_by_term \
LMS_MISSING_RATE=0.10 \
FIN_MISSING_RATE=0.25 \
./run.sh
```

Disable baseline generation when needed:

```bash
GENERATE_BASELINE=false ./run.sh
```

## Evaluate Fragmentation

1. Install connector dependencies:

```bash
python -m pip install -r connect/requirements.txt
```

Or allow the evaluation shell scripts to install them explicitly:

```bash
AUTO_INSTALL_PY_DEPS=true ./evaluate_text_to_sql.sh
```

2. Run the default load+evaluate pipeline:

```bash
./db.sh
```

This loads:

- `./out_baseline` -> `./db/edu_baseline.duckdb`
- `./out` -> `./db/edu_fragmented.duckdb`

Then evaluates fragmented vs baseline and writes `./result.log`.

If you used custom generation output paths, pass them into `db.sh`:

```bash
FRAGMENTED_OUT_DIR=/path/to/out_fragmented \
BASELINE_OUT_DIR=/path/to/out_baseline \
./db.sh
```

3. (Optional) Run manually with custom paths:

```bash
python connect/load_data.py --input /path/to/out_clean --db /path/to/baseline.duckdb --clear
python connect/load_data.py --input /path/to/out_fragmented --db /path/to/fragmented.duckdb --clear
```

4. Run scoring:

```bash
python connect/evaluate.py \
  --db /path/to/fragmented.duckdb \
  --baseline-db /path/to/baseline.duckdb \
  --frag-level high
```

The default workload expects schema-aligned tables including:

- `sis_enrollments`
- `identity_crosswalk_integration`
- `financial_aid_wide`
- `lms_activity_wide`

## Documentation

- Generator schema: `higher_ed_schema.md`
- Scoring logic spec: `connector.md`
- Connector usage: `connect/README.md`
- Rust module notes: `src/README.md`
- Local DB notes: `db/README.md`
