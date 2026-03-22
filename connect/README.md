# High-Ed DuckDB Connector and Fragmentation Scoring

This folder contains a Python toolkit for:

- loading generated high-ed CSV/JSON files into DuckDB (`load_data.py`)
- executing SQL through a Query Receipt Layer (`query_receipt_layer.py`)
- scoring query-level fragmentation against a clean baseline (`evaluate.py`)

## Files

- `load_data.py`: Recursively loads `.csv` and `.json` files into DuckDB tables.
- `query_receipt_layer.py`: Executes queries and logs runtime/plan receipts into `receipts`.
- `workload_spec.py`: Declarative workload specs (`QuerySpec` and `JoinSpec`).
- `fragmentation_scoring.py`: Computes `JML`, `CCL`, `MNS`, `RTL`, `SBL`, `STL`, then component and final scores.
- `evaluate.py`: Runs the default workload on target + baseline DBs and prints score summaries.
- `requirements.txt`: Python dependencies.

## Install

```bash
python -m pip install -r connect/requirements.txt
```

## Load Data

Load one dataset run into one DuckDB file:

```bash
python connect/load_data.py --input ./out_baseline --db ./db/edu_baseline.duckdb --clear
python connect/load_data.py --input ./out --db ./db/edu_fragmented.duckdb --clear
```

Important behavior:

- The loader skips `metadata.json`.
- Tables are named by file stem (`sis_enrollments`, `identity_crosswalk_integration`, `financial_aid`, `financial_aid_wide`, `lms_activity`, `lms_activity_wide`, ...).
- v1 scoring assumes one run per DuckDB file. Do not mix baseline and fragmented runs into one DB.

Default workload assumptions:

- `q1` reads `sis_enrollments` (schema-core fields).
- Cross-system bridge queries (`q2`, `q3`) use `identity_crosswalk_integration` plus wide tables (`financial_aid_wide`, `lms_activity_wide`) so joins can be evaluated on integration identifiers (`erp_person_id`, `sis_user_id`).

## Run Fragmentation Evaluation

```bash
python connect/evaluate.py \
  --db ./db/edu_fragmented.duckdb \
  --baseline-db ./db/edu_baseline.duckdb \
  --frag-level generated
```

Optional:

- `--no-result`: Skip returning result DataFrames (faster when only scoring is needed).
- Use `../db.sh` for the full default pipeline (load baseline + fragmented + evaluate).

Validation behavior:

- `evaluate.py` checks that required default-workload tables exist before running:
  `sis_enrollments`, `identity_crosswalk_integration`, `financial_aid_wide`, `lms_activity_wide`.

## Stored Outputs

The target database gets two receipt tables:

- `receipts`: Raw execution receipts from `QueryReceiptLayer`.
- `fragmentation_receipts`: Per-query summary columns plus a JSON payload with:
  join diagnostics, null rates, baseline comparisons, primitive metrics, and final score.

## Scoring Formula (default)

- `accuracy_score = 0.5*JML + 0.3*CCL + 0.2*MNS`
- `efficiency_score = 0.5*RTL + 0.3*SBL + 0.2*STL`
- `fragmentation_score = 0.6*accuracy_score + 0.4*efficiency_score`

Efficiency metrics are renormalized across available terms if scanned bytes are unavailable in the current DuckDB plan JSON.
