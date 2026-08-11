#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  BENCHMARK_PYTHON="${PYTHON_BIN}"
elif [[ -x ".venv/bin/python" ]]; then
  BENCHMARK_PYTHON=".venv/bin/python"
else
  BENCHMARK_PYTHON="python3"
fi

CONFIG_PATH="${CONFIG_PATH:-configs/benchmark.yaml}"
RUN_DIR="${RUN_DIR:-artifacts/runs/local}"
REGISTRY_PATH="${REGISTRY_PATH:-configs/query_registry.json}"

if ! "${BENCHMARK_PYTHON}" -c "import duckdb, sqlglot" >/dev/null 2>&1; then
  echo "Missing Python dependencies. Install the project with: python -m pip install -e ." >&2
  exit 1
fi

# 数据生成规模会快速放大，默认使用 release profile 避免 debug 构建拖慢主链路。
cargo run --quiet --release -- \
  --config "${CONFIG_PATH}" \
  --output "${RUN_DIR}" \
  --overwrite

# 统一从注册表执行参考 SQL，生成的数据和分析结果因此共享同一问题定义。
PYTHONPATH="${ROOT_DIR}/python/src${PYTHONPATH:+:${PYTHONPATH}}" \
  "${BENCHMARK_PYTHON}" -m benchmark analyze \
  --run-dir "${RUN_DIR}" \
  --registry "${REGISTRY_PATH}"

echo "Benchmark artifacts: ${RUN_DIR}"
