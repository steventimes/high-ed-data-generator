#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

fail() {
  echo "verify-refactor: $*" >&2
  exit 1
}

[[ -f "Cargo.toml" ]] || fail "expected standard Cargo.toml at repository root"
[[ ! -f "cargo.toml" ]] || fail "legacy lowercase cargo.toml should not exist"
[[ -f "scripts/lib/runtime.sh" ]] || fail "expected shared shell runtime at scripts/lib/runtime.sh"

bash -n scripts/lib/runtime.sh run.sh text_to_sql.sh evaluate_text_to_sql.sh build.sh

for entrypoint in run.sh text_to_sql.sh evaluate_text_to_sql.sh; do
  grep -q 'scripts/lib/runtime.sh' "${entrypoint}"     || fail "${entrypoint} must source scripts/lib/runtime.sh"
  grep -q 'hdg_resolve_python_bin' "${entrypoint}"     || fail "${entrypoint} must use shared Python resolver"
  if grep -q 'pip install' "${entrypoint}"; then
    fail "${entrypoint} must not call pip install directly; use hdg_ensure_requirements"
  fi
  if grep -q 'AUTO_INSTALL_.*:-true' "${entrypoint}"; then
    fail "${entrypoint} must not default auto-install flags to true"
  fi
done

grep -q 'AUTO_INSTALL_PY_DEPS="${AUTO_INSTALL_PY_DEPS:-false}"' run.sh   || fail "run.sh must default AUTO_INSTALL_PY_DEPS=false"
grep -q 'AUTO_INSTALL_PY_DEPS="${AUTO_INSTALL_PY_DEPS:-false}"' evaluate_text_to_sql.sh   || fail "evaluate_text_to_sql.sh must default AUTO_INSTALL_PY_DEPS=false"
grep -q 'AUTO_INSTALL_TEXT_TO_SQL_DEPS="${AUTO_INSTALL_TEXT_TO_SQL_DEPS:-false}"' text_to_sql.sh   || fail "text_to_sql.sh must default AUTO_INSTALL_TEXT_TO_SQL_DEPS=false"

grep -q 'hdg_require_boolean_flag' scripts/lib/runtime.sh   || fail "runtime.sh must validate boolean flags"
grep -q 'hdg_ensure_requirements' scripts/lib/runtime.sh   || fail "runtime.sh must centralize dependency checks"
grep -q 'hdg_resolve_python_bin' scripts/lib/runtime.sh   || fail "runtime.sh must centralize Python resolution"

python3 - <<'PYVERIFY'
from pathlib import Path

runtime = Path('scripts/lib/runtime.sh').read_text(encoding='utf-8')
pip_install_commands = [
    line.strip()
    for line in runtime.splitlines()
    if line.strip().startswith('\"${python_bin}\" -m pip install')
]
if len(pip_install_commands) != 1:
    raise SystemExit('runtime.sh should contain exactly one centralized pip install command path')

text_to_sql = Path('text_to_sql.sh').read_text(encoding='utf-8')
help_gate = '"${1}" == "--help"' in text_to_sql and 'NEEDS_TEXT_TO_SQL_DEPS="false"' in text_to_sql
if not help_gate:
    raise SystemExit('text_to_sql.sh should allow help/canonical paths without text-to-SQL dependencies')

for path in ['run.sh', 'text_to_sql.sh', 'evaluate_text_to_sql.sh']:
    content = Path(path).read_text(encoding='utf-8')
    if 'set -euo pipefail' not in content:
        raise SystemExit(f'{path} must keep strict shell mode')
PYVERIFY

echo "Refactor checks passed."
