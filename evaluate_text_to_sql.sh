#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "Could not find python or python3. Set PYTHON_BIN explicitly." >&2
    exit 1
  fi
fi

AUTO_INSTALL_PY_DEPS="${AUTO_INSTALL_PY_DEPS:-true}"
if [[ "${AUTO_INSTALL_PY_DEPS}" != "true" && "${AUTO_INSTALL_PY_DEPS}" != "false" ]]; then
  echo "AUTO_INSTALL_PY_DEPS must be true or false." >&2
  exit 1
fi

if [[ "${AUTO_INSTALL_PY_DEPS}" == "true" ]]; then
  if ! "${PYTHON_BIN}" -c "import duckdb, matplotlib" >/dev/null 2>&1; then
    echo "Installing Python benchmark dependencies with ${PYTHON_BIN}"
    "${PYTHON_BIN}" -m pip install -r python/requirements.txt
  fi
fi

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
