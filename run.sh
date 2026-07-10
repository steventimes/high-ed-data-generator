#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/lib/runtime.sh"

ROOT_DIR="$(hdg_root_dir)"
cd "${ROOT_DIR}"

CARGO_BIN="${CARGO_BIN:-cargo}"
PYTHON_BIN="$(hdg_resolve_python_bin "${ROOT_DIR}")"
RUN_ID="${RUN_ID:-local}"
SCHEMA_CONFIG="${SCHEMA_CONFIG:-configs/schema_registry.yaml}"
EXPERIMENT_CONFIG="${EXPERIMENT_CONFIG:-configs/experiment.yaml}"
RUN_ROOT="${RUN_ROOT:-artifacts/runs}"
AUTO_INSTALL_PY_DEPS="${AUTO_INSTALL_PY_DEPS:-false}"

hdg_require_boolean_flag "AUTO_INSTALL_PY_DEPS" "${AUTO_INSTALL_PY_DEPS}"
hdg_ensure_requirements \
  "${PYTHON_BIN}" \
  "python/requirements.txt" \
  "import duckdb, matplotlib" \
  "${AUTO_INSTALL_PY_DEPS}" \
  "Python benchmark"

echo "Generating benchmark run ${RUN_ID}"
"${CARGO_BIN}" run -p fragmentation-cli -- generate \
  --schema "${SCHEMA_CONFIG}" \
  --experiment "${EXPERIMENT_CONFIG}" \
  --out-root "${RUN_ROOT}" \
  --run-id "${RUN_ID}" \
  --overwrite

echo "Running analysis for ${RUN_ROOT%/}/${RUN_ID}"
PYTHONPATH="${ROOT_DIR}/python/src${PYTHONPATH:+:${PYTHONPATH}}" \
  "${PYTHON_BIN}" -m benchmark.run_analysis \
  --run-dir "${RUN_ROOT%/}/${RUN_ID}"

echo "Done. Artifacts are in ${RUN_ROOT%/}/${RUN_ID}"
