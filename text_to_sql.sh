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

AUTO_INSTALL_TEXT_TO_SQL_DEPS="${AUTO_INSTALL_TEXT_TO_SQL_DEPS:-true}"
if [[ "${AUTO_INSTALL_TEXT_TO_SQL_DEPS}" != "true" && "${AUTO_INSTALL_TEXT_TO_SQL_DEPS}" != "false" ]]; then
  echo "AUTO_INSTALL_TEXT_TO_SQL_DEPS must be true or false." >&2
  exit 1
fi

NEEDS_TEXT_TO_SQL_DEPS="true"
if [[ "$#" -eq 1 && ( "${1}" == "--help" || "${1}" == "-h" || "${1}" == "--print-canonical" ) ]]; then
  NEEDS_TEXT_TO_SQL_DEPS="false"
fi

if [[ "${AUTO_INSTALL_TEXT_TO_SQL_DEPS}" == "true" && "${NEEDS_TEXT_TO_SQL_DEPS}" == "true" ]]; then
  if ! "${PYTHON_BIN}" -c "import openai" >/dev/null 2>&1; then
    echo "Installing text-to-SQL dependencies with ${PYTHON_BIN}"
    "${PYTHON_BIN}" -m pip install -r python/requirements-text-to-sql.txt
  fi
fi

TEXT_TO_SQL_ARGS=("$@")
if [[ "$#" -eq 0 ]]; then
  QUESTIONS_FILE="${QUESTIONS_FILE:-question.json}"
  RUN_DIR="${RUN_DIR:-artifacts/runs/local}"
  VARIANTS="${VARIANTS:-baseline,low_fragmentation,medium_fragmentation,high_fragmentation}"
  TEXT_TO_SQL_ARGS=(
    --questions-file "${QUESTIONS_FILE}"
    --run-dir "${RUN_DIR}"
    --variants "${VARIANTS}"
  )
fi

PYTHONPATH="${ROOT_DIR}/python/src${PYTHONPATH:+:${PYTHONPATH}}" \
  "${PYTHON_BIN}" -m benchmark.text_to_sql.vanna_adapter "${TEXT_TO_SQL_ARGS[@]}"
