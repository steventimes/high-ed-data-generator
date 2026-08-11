from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from benchmark.cli import _filter_questions
from benchmark.questions import QuestionSpec


def test_module_cli_exposes_all_pipeline_stages() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "python" / "src")

    completed = subprocess.run(
        [sys.executable, "-m", "benchmark", "--help"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "{analyze,text-to-sql,evaluate}" in completed.stdout


def test_module_entrypoint_can_be_imported_without_running_cli() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "python" / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import benchmark.__main__; print('imported')",
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "imported"


def test_query_filter_rejects_partially_unknown_ids() -> None:
    questions = [QuestionSpec(question_id="known", question="Known question?")]

    with pytest.raises(ValueError, match="unknown"):
        _filter_questions(questions, ["known", "typo"])


def test_run_script_uses_the_release_generator(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo_args = tmp_path / "cargo-args.txt"
    fake_cargo = bin_dir / "cargo"
    fake_cargo.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "${CARGO_ARGS_FILE}"\n',
        encoding="utf-8",
    )
    fake_cargo.chmod(0o755)
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "PYTHON_BIN": str(fake_python),
            "CARGO_ARGS_FILE": str(cargo_args),
            "CONFIG_PATH": str(root / "configs" / "benchmark.yaml"),
            "RUN_DIR": str(tmp_path / "run"),
            "REGISTRY_PATH": str(root / "configs" / "query_registry.json"),
        }
    )
    completed = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--release" in cargo_args.read_text(encoding="utf-8").splitlines()
