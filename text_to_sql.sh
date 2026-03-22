#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

# Optional local secrets/config. .env is ignored by git.
if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "Could not find python or python3. Set PYTHON_BIN explicitly." >&2
    exit 1
  fi
fi
MODEL="${MODEL:-${OPENAI_MODEL:-gpt-5}}"
PROFILE_MODE="${PROFILE_MODE:-stats}"            # schema | stats
MAX_RETRIES="${MAX_RETRIES:-2}"
MAX_NULL_COLUMNS="${MAX_NULL_COLUMNS:-6}"
MAX_PREVIEW_ROWS="${MAX_PREVIEW_ROWS:-5}"
SHOW_SQL="${SHOW_SQL:-true}"                     # true | false
NO_EXECUTE="${NO_EXECUTE:-false}"                # true | false
OUTPUT="${OUTPUT:-./db/text_to_sql_experiments.csv}"

INCLUDE_BASELINE="${INCLUDE_BASELINE:-true}"     # true | false
INCLUDE_FRAGMENTED="${INCLUDE_FRAGMENTED:-true}" # true | false
BASELINE_DB="${BASELINE_DB:-./db/edu_baseline.duckdb}"
FRAGMENTED_DB="${FRAGMENTED_DB:-./db/edu_fragmented.duckdb}"

# Preferred way to manage larger prompt sets:
#   QUESTIONS_FILE=./questions.json ./text_to_sql.sh
QUESTIONS_FILE="${QUESTIONS_FILE:-questions.json}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  exec "${PYTHON_BIN}" text_to_sql.py --help
fi

if [[ "${SHOW_SQL}" != "true" && "${SHOW_SQL}" != "false" ]]; then
  echo "SHOW_SQL must be true or false." >&2
  exit 1
fi

if [[ "${NO_EXECUTE}" != "true" && "${NO_EXECUTE}" != "false" ]]; then
  echo "NO_EXECUTE must be true or false." >&2
  exit 1
fi

if [[ "${INCLUDE_BASELINE}" != "true" && "${INCLUDE_BASELINE}" != "false" ]]; then
  echo "INCLUDE_BASELINE must be true or false." >&2
  exit 1
fi

if [[ "${INCLUDE_FRAGMENTED}" != "true" && "${INCLUDE_FRAGMENTED}" != "false" ]]; then
  echo "INCLUDE_FRAGMENTED must be true or false." >&2
  exit 1
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Missing OPENAI_API_KEY. Put it in .env or export it before running." >&2
  exit 1
fi

CMD=(
  "${PYTHON_BIN}" text_to_sql.py
  --model "${MODEL}"
  --profile-mode "${PROFILE_MODE}"
  --max-retries "${MAX_RETRIES}"
  --max-null-columns "${MAX_NULL_COLUMNS}"
  --max-preview-rows "${MAX_PREVIEW_ROWS}"
)

if [[ "${INCLUDE_BASELINE}" == "true" ]]; then
  CMD+=(--db "baseline=${BASELINE_DB}")
fi

if [[ "${INCLUDE_FRAGMENTED}" == "true" ]]; then
  CMD+=(--db "fragmented=${FRAGMENTED_DB}")
fi

if [[ "${INCLUDE_BASELINE}" != "true" && "${INCLUDE_FRAGMENTED}" != "true" ]]; then
  echo "Enable at least one target: INCLUDE_BASELINE=true and/or INCLUDE_FRAGMENTED=true." >&2
  exit 1
fi

if [[ -n "${QUESTIONS_FILE}" ]]; then
  CMD+=(--questions-file "${QUESTIONS_FILE}")
fi

if [[ -n "${OUTPUT}" ]]; then
  CMD+=(--output "${OUTPUT}")
fi

if [[ "${SHOW_SQL}" == "true" ]]; then
  CMD+=(--show-sql)
fi

if [[ "${NO_EXECUTE}" == "true" ]]; then
  CMD+=(--no-execute)
fi

if [[ "$#" -gt 0 ]]; then
  for question in "$@"; do
    CMD+=(--question "${question}")
  done
elif [[ -z "${QUESTIONS_FILE}" ]]; then
  CMD+=(--question "What is the average Pell amount and unmet need by term?")
  CMD+=(--question "Show student aid stress across academic terms.")
fi

echo "Running text-to-SQL experiment:"
printf '  %q' "${CMD[@]}"
printf '\n'

"${CMD[@]}"
