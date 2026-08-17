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
if [[ "${RUN_DIR+x}" == "x" ]]; then
  BENCHMARK_RUN_DIR="${RUN_DIR}"
else
  BENCHMARK_RUN_DIR="artifacts/runs/local"
fi
REGISTRY_PATH="${REGISTRY_PATH:-python/src/benchmark/query_registry.json}"
BENCHMARK_PYTHONPATH="${ROOT_DIR}/python/src${PYTHONPATH:+:${PYTHONPATH}}"
STAGING_RUN_DIR=""

cleanup_staging() {
  local exit_code=$?
  trap - EXIT
  if [[ -n "${STAGING_RUN_DIR}" ]]; then
    PYTHONPATH="${BENCHMARK_PYTHONPATH}" \
      "${BENCHMARK_PYTHON}" -m benchmark.run_transaction cleanup \
      --run-dir "${BENCHMARK_RUN_DIR}" \
      --staging-dir "${STAGING_RUN_DIR}" \
      || echo "warning: failed to clean pipeline staging directory" >&2
  fi
  exit "${exit_code}"
}

trap cleanup_staging EXIT

# 覆盖运行目录前先完成只读预检，配置错误不会破坏上一轮可用产物。
if ! PYTHONPATH="${BENCHMARK_PYTHONPATH}" \
  "${BENCHMARK_PYTHON}" -c \
  'from pathlib import Path; import sys; import benchmark.cli; from benchmark.preflight import preflight_registry; preflight_registry(Path(sys.argv[1]))' \
  "${REGISTRY_PATH}"
then
  echo "Python/registry preflight failed; generation was not started." >&2
  exit 1
fi

# Rust 生成和 Python 分析共用同级暂存目录，任一步失败都不会触碰旧批次。
STAGING_RUN_DIR="$(
  PYTHONPATH="${BENCHMARK_PYTHONPATH}" \
    "${BENCHMARK_PYTHON}" -m benchmark.run_transaction prepare \
    --run-dir "${BENCHMARK_RUN_DIR}"
)"

# 数据生成规模会快速放大，默认使用 release profile 避免 debug 构建拖慢主链路。
cargo run --quiet --release -- \
  --config "${CONFIG_PATH}" \
  --output "${STAGING_RUN_DIR}" \
  --overwrite

# 统一从注册表执行参考 SQL，生成的数据和分析结果因此共享同一问题定义。
PYTHONPATH="${BENCHMARK_PYTHONPATH}" \
  "${BENCHMARK_PYTHON}" -m benchmark analyze \
  --run-dir "${STAGING_RUN_DIR}" \
  --registry "${REGISTRY_PATH}"

PYTHONPATH="${BENCHMARK_PYTHONPATH}" \
  "${BENCHMARK_PYTHON}" -m benchmark.run_transaction publish \
  --run-dir "${BENCHMARK_RUN_DIR}" \
  --staging-dir "${STAGING_RUN_DIR}"
STAGING_RUN_DIR=""

echo "Benchmark artifacts: ${BENCHMARK_RUN_DIR}"
