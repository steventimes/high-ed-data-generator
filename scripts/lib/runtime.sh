#!/usr/bin/env bash

set -euo pipefail

hdg_root_dir() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
  printf '%s\n' "${script_dir}"
}

hdg_resolve_python_bin() {
  local root_dir="${1}"
  local python_bin="${PYTHON_BIN:-}"

  if [[ -n "${python_bin}" ]]; then
    printf '%s\n' "${python_bin}"
    return 0
  fi

  if [[ -x "${root_dir}/.venv/bin/python" ]]; then
    printf '%s\n' "${root_dir}/.venv/bin/python"
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return 0
  fi

  if command -v python.exe >/dev/null 2>&1; then
    printf '%s\n' "python.exe"
    return 0
  fi

  echo "Could not find python, python3, or python.exe. Set PYTHON_BIN explicitly." >&2
  return 1
}

hdg_require_boolean_flag() {
  local name="${1}"
  local value="${2}"
  if [[ "${value}" != "true" && "${value}" != "false" ]]; then
    echo "${name} must be true or false." >&2
    return 1
  fi
}

hdg_ensure_requirements() {
  local python_bin="${1}"
  local requirements_file="${2}"
  local import_check="${3}"
  local auto_install="${4}"
  local label="${5}"

  if "${python_bin}" -c "${import_check}" >/dev/null 2>&1; then
    return 0
  fi

  if [[ "${auto_install}" == "true" ]]; then
    if ! "${python_bin}" -m pip --version >/dev/null 2>&1; then
      echo "Cannot auto-install ${label} dependencies because pip is unavailable for ${python_bin}." >&2
      echo "Install pip first or set PYTHON_BIN to an interpreter with pip support." >&2
      return 1
    fi

    echo "Installing ${label} dependencies with ${python_bin}"
    "${python_bin}" -m pip install -r "${requirements_file}"
    return 0
  fi

  echo "Missing ${label} dependencies for ${python_bin}." >&2
  echo "Install them manually with: ${python_bin} -m pip install -r ${requirements_file}" >&2
  echo "Or rerun with AUTO_INSTALL_PY_DEPS=true / AUTO_INSTALL_TEXT_TO_SQL_DEPS=true." >&2
  return 1
}
