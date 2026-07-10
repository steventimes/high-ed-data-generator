#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/lib/runtime.sh"

ROOT_DIR="$(hdg_root_dir)"
cd "${ROOT_DIR}"

PYTHON_BIN="$(hdg_resolve_python_bin "${ROOT_DIR}")"

AUTO_INSTALL_PY_DEPS="${AUTO_INSTALL_PY_DEPS:-false}"
hdg_require_boolean_flag "AUTO_INSTALL_PY_DEPS" "${AUTO_INSTALL_PY_DEPS}"

hdg_ensure_requirements \
  "${PYTHON_BIN}" \
  "python/requirements.txt" \
  "import duckdb, matplotlib" \
  "${AUTO_INSTALL_PY_DEPS}" \
  "Python benchmark"

EVAL_ARGS=("$@")
if [[ "$#" -eq 0 ]]; then
  QUESTIONS_FILE="${QUESTIONS_FILE:-question.json}"
  RUN_DIR="${RUN_DIR:-artifacts/runs/local}"
  OUTPUT_DIR="${OUTPUT_DIR:-${RUN_DIR}/evaluation/text_to_sql}"
  EVAL_ARGS=(
    --questions-file "${QUESTIONS_FILE}"
    --run-dir "${RUN_DIR}"
    --output-dir "${OUTPUT_DIR}"
  )
fi

PYTHONPATH="${ROOT_DIR}/python/src${PYTHONPATH:+:${PYTHONPATH}}" \
  "${PYTHON_BIN}" -m benchmark.evaluation.run_text_to_sql "${EVAL_ARGS[@]}"
