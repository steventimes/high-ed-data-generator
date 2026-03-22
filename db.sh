#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
FRAGMENTED_OUT_DIR="${FRAGMENTED_OUT_DIR:-./out}"
BASELINE_OUT_DIR="${BASELINE_OUT_DIR:-./out_baseline}"
FRAGMENTED_DB="${FRAGMENTED_DB:-./db/edu_fragmented.duckdb}"
BASELINE_DB="${BASELINE_DB:-./db/edu_baseline.duckdb}"
FRAG_LEVEL="${FRAG_LEVEL:-generated}"

BASELINE_LOAD_LOG="${BASELINE_LOAD_LOG:-./db_baseline.log}"
FRAGMENTED_LOAD_LOG="${FRAGMENTED_LOAD_LOG:-./db_fragmented.log}"
EVAL_LOG="${EVAL_LOG:-./result.log}"

if [[ "${FRAGMENTED_DB%/}" == "${BASELINE_DB%/}" ]]; then
  echo "FRAGMENTED_DB and BASELINE_DB must be different files." >&2
  exit 1
fi

if [[ ! -d "${FRAGMENTED_OUT_DIR}" ]]; then
  echo "Fragmented output directory not found: ${FRAGMENTED_OUT_DIR}" >&2
  exit 1
fi

if [[ ! -d "${BASELINE_OUT_DIR}" ]]; then
  echo "Baseline output directory not found: ${BASELINE_OUT_DIR}" >&2
  exit 1
fi

mkdir -p "$(dirname "${FRAGMENTED_DB}")" "$(dirname "${BASELINE_DB}")"

echo "Loading baseline dataset -> ${BASELINE_DB}"
"${PYTHON_BIN}" connect/load_data.py --input "${BASELINE_OUT_DIR}" --db "${BASELINE_DB}" --clear > "${BASELINE_LOAD_LOG}"

echo "Loading fragmented dataset -> ${FRAGMENTED_DB}"
"${PYTHON_BIN}" connect/load_data.py --input "${FRAGMENTED_OUT_DIR}" --db "${FRAGMENTED_DB}" --clear > "${FRAGMENTED_LOAD_LOG}"

echo "Running fragmentation evaluation -> ${EVAL_LOG}"
"${PYTHON_BIN}" connect/evaluate.py --db "${FRAGMENTED_DB}" --baseline-db "${BASELINE_DB}" --frag-level "${FRAG_LEVEL}" > "${EVAL_LOG}"

echo "Done."
