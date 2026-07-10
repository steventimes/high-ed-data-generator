#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/lib/runtime.sh"

ROOT_DIR="$(hdg_root_dir)"
cd "${ROOT_DIR}"

PYTHON_BIN="$(hdg_resolve_python_bin "${ROOT_DIR}")"

AUTO_INSTALL_TEXT_TO_SQL_DEPS="${AUTO_INSTALL_TEXT_TO_SQL_DEPS:-false}"
hdg_require_boolean_flag "AUTO_INSTALL_TEXT_TO_SQL_DEPS" "${AUTO_INSTALL_TEXT_TO_SQL_DEPS}"

if [[ "$#" -eq 1 && ( "${1}" == "--help" || "${1}" == "-h" ) ]]; then
  cat <<'EOF'
Usage: ./text_to_sql.sh [OPTIONS]

Runs natural-language text-to-SQL experiments across benchmark variants.

Default behavior with no arguments:
  --questions-file ${QUESTIONS_FILE:-question.json}
  --run-dir ${RUN_DIR:-artifacts/runs/local}
  --variants ${VARIANTS:-baseline,low_fragmentation,medium_fragmentation,high_fragmentation}

Common options passed through to benchmark.text_to_sql.vanna_adapter:
  --questions-file PATH
  --question TEXT
  --run-dir PATH
  --variants CSV
  --target LABEL=VARIANT_DIR
  --model MODEL
  --max-retries N
  --output PATH
  --generated-results-dir PATH
  --print-canonical
  -h, --help

Dependencies are not auto-installed by default. Set AUTO_INSTALL_TEXT_TO_SQL_DEPS=true
to allow installation from python/requirements-text-to-sql.txt.
EOF
  exit 0
fi

NEEDS_TEXT_TO_SQL_DEPS="true"
if [[ "$#" -eq 1 && ( "${1}" == "--help" || "${1}" == "-h" || "${1}" == "--print-canonical" ) ]]; then
  NEEDS_TEXT_TO_SQL_DEPS="false"
fi

if [[ "${NEEDS_TEXT_TO_SQL_DEPS}" == "true" ]]; then
  hdg_ensure_requirements \
    "${PYTHON_BIN}" \
    "python/requirements-text-to-sql.txt" \
    "import openai" \
    "${AUTO_INSTALL_TEXT_TO_SQL_DEPS}" \
    "text-to-SQL"
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
