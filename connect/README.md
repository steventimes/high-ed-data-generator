# High‑Ed DuckDB Connector and Query Evaluation Layer

This folder contains a small Python toolkit for loading the synthetic
higher‑education datasets produced by the
[`high‑ed‑data‑generator`](https://github.com/steventimes/high-ed-data-generator)
project into a DuckDB database and for evaluating SQL workloads with a
Query Receipt Layer (QRL).  The code is designed to be self‑contained
and does not require the generator’s Rust sources.  It expects that
you have already run the generator’s `run.sh` script and produced
output files (CSV and JSON) under a single output directory.

## Contents

* `load_data.py` – Command‑line script that walks the generator’s
  output directory, infers table schemas and loads the files into a
  DuckDB database.  Both CSV and JSON input are supported.  The
  script creates one table per file (e.g. `students_master`,
  `identity_crosswalk`, `sis_enrollments`, `registrar_course_enrollments`,
  `lms_activity`, `financial_aid`, `advising_holds`) and appends data
  across terms.  If a composite key column contains a delimiter such
  as `|`, it will be split into separate columns.

* `query_receipt_layer.py` – Defines a `QueryReceiptLayer` class
  which wraps DuckDB query execution.  It captures simple metrics
  about each query (execution time, source fan‑out and the raw
  `EXPLAIN ANALYZE` JSON plan) and writes a structured receipt to
  a `receipts` table inside the same database.  The class can be
  reused to run arbitrary workloads and inspect the resulting
  receipts.

* `evaluate.py` – Example script that demonstrates how to execute a
  small workload of SQL queries against a database loaded with
  generator outputs.  The script uses `QueryReceiptLayer` to log
  receipts and prints a summary table of the collected metrics.

* `requirements.txt` – Lists the minimal Python dependencies.

## Installation

1. Install Python 3.9+ and [pip](https://pip.pypa.io).  The only
   required dependencies are `duckdb`, `pandas` and `tabulate`.

   ```bash
   cd high_ed_duckdb
   python -m pip install -r requirements.txt
   ```

2. Ensure that you have generated data using the high‑ed data
   generator.  For example:

   ```bash
   # Clone the generator if you don’t have it
   git clone https://github.com/steventimes/high-ed-data-generator.git
   cd high-ed-data-generator
   # Produce a low‑fragmentation dataset
   STUDENTS=500 LMS_MISSING_RATE=0.0 FIN_MISSING_RATE=0.0 \
   HOLD_RATE=0.0 CROSSWALK_MISMATCH_RATE=0.0 ./run.sh
   # outputs will appear in ./out by default
   ```

## Loading Data

Use the `load_data.py` script to import the generator’s outputs into a
DuckDB database.  You can specify the output directory and database
file with command‑line flags.  By default the script will append data
into existing tables; passing `--clear` will drop and recreate
existing tables.

```bash
python load_data.py \
  --input /path/to/high-ed-data-generator/out \
  --db /path/to/edu.duckdb \
  --clear
```

Once complete you can open the database with the DuckDB CLI or use
Python to query the tables.

## Running Evaluation Workloads

The `evaluate.py` script provides a template for running a set of
SQL workloads across different fragmentation levels.  It accepts a
database path and will execute a list of example queries, logging
receipts into a `receipts` table and printing a summary of the
metrics.

```bash
python evaluate.py --db /path/to/edu.duckdb
```

You can customise the queries or fragmentation levels by editing
`evaluate.py`.  Each call to the `execute()` method of
`QueryReceiptLayer` records a JSON plan, runtime and source fan‑out.
For more advanced analysis (join fractions, schema drift, etc.) you
may extend the implementation accordingly.

## Notes

* The scripts are intentionally conservative about schema inference.
  Column types are inferred using pandas and DuckDB’s automatic
  casting.  If you need stricter types or indexing, adjust the
  `load_data.py` script.
* DuckDB JSON profiling is enabled via `EXPLAIN ANALYZE FORMAT JSON`.
  For metrics such as join cardinality or bytes scanned you may need
  to parse deeper into the plan; see the DuckDB documentation for
  details.  The generator’s `run.sh` script exposes knobs for
  controlling fragmentation levels through environment variables
  (e.g. `LMS_MISSING_RATE` and `FIN_MISSING_RATE` control missing
  percentages【942396643357201†L12-L18】).
