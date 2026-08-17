from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from benchmark.evaluation.metrics import (
    VARIANT_NAMES,
    compare_entity_sets,
    compute_fragmentation_score,
    write_csv,
)
from benchmark.questions import QuestionSpec
from benchmark.sql_runtime import VariantSqlRuntime, require_matching_cohorts
from benchmark.temporal import (
    TEMPORAL_METRIC_FIELDS,
    TemporalMetrics,
    compute_temporal_metrics,
)

_RunSourceIdentity = tuple[tuple[int, int], tuple[tuple[str, str], ...]]


def analyze_run(
    run_dir: Path,
    questions: list[QuestionSpec],
) -> dict[str, Path]:
    for question in questions:
        if question.reference_sql is None:
            raise ValueError(
                f"Question {question.question_id} does not define reference_sql"
            )

    source_identity = _capture_run_source_identity(run_dir)
    baseline_rows_by_question: dict[str, list[dict[str, Any]]] = {}
    result_chunks: dict[tuple[str, str], list[dict[str, Any]]] = {}
    metrics_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    variant_dirs = {
        variant: run_dir / "variants" / variant for variant in VARIANT_NAMES
    }

    # 四个连接先共同物化，再执行任何查询；覆盖运行根时不会混入另一个批次。
    with ExitStack() as stack:
        runtimes = {
            variant: stack.enter_context(VariantSqlRuntime(variant_dir))
            for variant, variant_dir in variant_dirs.items()
        }
        require_matching_cohorts(
            [
                (variant, runtime.cohort_fingerprint)
                for variant, runtime in runtimes.items()
            ]
        )
        _ensure_run_source_unchanged(run_dir, source_identity)

        baseline_dir = variant_dirs["baseline"]
        runtime = runtimes["baseline"]
        manifest = runtime.manifest
        fragmentation_score = _fragmentation_score(manifest, baseline_dir)
        for question in questions:
            rows = runtime.execute(
                question.reference_sql,
                required_columns=(question.entity_key,),
            )
            baseline_rows_by_question[question.question_id] = rows
            result_chunks[(question.question_id, "baseline")] = _tag_rows_in_place(
                question.question_id,
                "baseline",
                rows,
            )
            metrics_by_key[(question.question_id, "baseline")] = _metrics_row(
                question,
                "baseline",
                fragmentation_score,
                rows,
                rows,
                compute_temporal_metrics(
                    manifest=manifest,
                    temporal_evaluation=question.temporal_evaluation,
                    entity_key=question.entity_key,
                    execute=runtime.execute,
                ),
            )

        for variant in VARIANT_NAMES:
            if variant == "baseline":
                continue
            variant_dir = variant_dirs[variant]
            runtime = runtimes[variant]
            manifest = runtime.manifest
            fragmentation_score = _fragmentation_score(manifest, variant_dir)
            for question in questions:
                rows = runtime.execute(
                    question.reference_sql,
                    required_columns=(question.entity_key,),
                )
                result_chunks[(question.question_id, variant)] = _tag_rows_in_place(
                    question.question_id,
                    variant,
                    rows,
                )
                metrics_by_key[(question.question_id, variant)] = _metrics_row(
                    question,
                    variant,
                    fragmentation_score,
                    baseline_rows_by_question[question.question_id],
                    rows,
                    compute_temporal_metrics(
                        manifest=manifest,
                        temporal_evaluation=question.temporal_evaluation,
                        entity_key=question.entity_key,
                        execute=runtime.execute,
                    ),
                )
        _ensure_run_source_unchanged(run_dir, source_identity)

    metrics_rows: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    # 按原有顺序重组结果，确保已有消费者无需感知连接复用策略的变化。
    for question in questions:
        for variant in VARIANT_NAMES:
            key = (question.question_id, variant)
            metrics_rows.append(metrics_by_key[key])
            result_rows.extend(result_chunks.pop(key))

    metrics_dir = run_dir / "metrics"
    metrics_path = metrics_dir / "reference_query_metrics.csv"
    results_path = metrics_dir / "reference_query_results.csv"
    result_fields = ["query_id", "variant"]
    for question in questions:
        if question.entity_key not in result_fields:
            result_fields.append(question.entity_key)
    for row in result_rows:
        for key in row:
            if key not in result_fields:
                result_fields.append(key)

    # 查询完成后再次绑定源目录；旧快照绝不能发布到刚覆盖的新运行目录。
    _ensure_run_source_unchanged(run_dir, source_identity)
    _write_reference_outputs(
        run_dir=run_dir,
        source_identity=source_identity,
        metrics_path=metrics_path,
        metrics_rows=metrics_rows,
        metrics_fields=[
            "query_id",
            "variant",
            "fragmentation_score",
            "baseline_count",
            "returned_count",
            "missed_count",
            "extra_count",
            "miss_rate",
            "jaccard",
            *TEMPORAL_METRIC_FIELDS,
        ],
        results_path=results_path,
        result_rows=result_rows,
        result_fields=result_fields,
    )
    return {
        "reference_query_metrics": metrics_path,
        "reference_query_results": results_path,
    }


def _capture_run_source_identity(run_dir: Path) -> _RunSourceIdentity:
    try:
        canonical_run = run_dir.resolve(strict=True)
        run_stat = canonical_run.stat()
        files: list[tuple[str, str]] = []
        for directory_name in ("variants", "manifests", "config_snapshot"):
            directory = canonical_run / directory_name
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                files.append(
                    (path.relative_to(canonical_run).as_posix(), digest.hexdigest())
                )
    except OSError as error:
        raise RuntimeError("Benchmark run changed during analysis") from error
    return ((run_stat.st_dev, run_stat.st_ino), tuple(files))


def _ensure_run_source_unchanged(
    run_dir: Path,
    expected: _RunSourceIdentity,
) -> None:
    current = _capture_run_source_identity(run_dir)
    if current != expected:
        raise RuntimeError("Benchmark run changed during analysis")


def _write_reference_outputs(
    *,
    run_dir: Path,
    source_identity: _RunSourceIdentity,
    metrics_path: Path,
    metrics_rows: list[dict[str, Any]],
    metrics_fields: list[str],
    results_path: Path,
    result_rows: list[dict[str, Any]],
    result_fields: list[str],
) -> None:
    run_fd = _open_verified_run_directory(run_dir, source_identity)
    metrics_fd: int | None = None
    created_metrics = False
    staging_dir = Path(
        tempfile.mkdtemp(prefix=".reference-analysis-", dir=run_dir.parent)
    )
    staged_metrics = staging_dir / metrics_path.name
    staged_results = staging_dir / results_path.name
    try:
        try:
            os.mkdir("metrics", mode=0o775, dir_fd=run_fd)
            created_metrics = True
        except FileExistsError:
            pass
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        metrics_fd = os.open("metrics", directory_flags, dir_fd=run_fd)

        # 先完整生成两个文件，序列化失败时正式目录不会出现半批结果。
        write_csv(staged_metrics, metrics_rows, metrics_fields)
        write_csv(staged_results, result_rows, result_fields)
        _preserve_existing_mode_at(staged_metrics, metrics_path.name, metrics_fd)
        _preserve_existing_mode_at(staged_results, results_path.name, metrics_fd)
        _publish_csv_batch_at(
            [
                (staged_metrics, metrics_path.name),
                (staged_results, results_path.name),
            ],
            directory_fd=metrics_fd,
            verify_source=lambda: _verify_open_run_source(
                run_dir,
                run_fd,
                source_identity,
            ),
        )
    finally:
        if metrics_fd is not None:
            os.close(metrics_fd)
        if created_metrics:
            try:
                os.rmdir("metrics", dir_fd=run_fd)
            except OSError:
                pass
        os.close(run_fd)
        # 暂存目录在 run 外部且名称随机；源目录换代时也不会误删新运行内容。
        shutil.rmtree(staging_dir, ignore_errors=True)


def _open_verified_run_directory(
    run_dir: Path,
    expected: _RunSourceIdentity,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        canonical = run_dir.resolve(strict=True)
        descriptor = os.open(canonical, flags)
    except OSError as error:
        raise RuntimeError("Benchmark run changed during analysis") from error
    actual_stat = os.fstat(descriptor)
    if (actual_stat.st_dev, actual_stat.st_ino) != expected[0]:
        os.close(descriptor)
        raise RuntimeError("Benchmark run changed during analysis")
    return descriptor


def _verify_open_run_source(
    run_dir: Path,
    run_fd: int,
    expected: _RunSourceIdentity,
) -> None:
    opened_stat = os.fstat(run_fd)
    if (opened_stat.st_dev, opened_stat.st_ino) != expected[0]:
        raise RuntimeError("Benchmark run changed during analysis")
    _ensure_run_source_unchanged(run_dir, expected)


def _preserve_existing_mode_at(
    staged_path: Path,
    target_name: str,
    directory_fd: int,
) -> None:
    try:
        target_stat = os.stat(
            target_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if not stat.S_ISREG(target_stat.st_mode):
        raise ValueError(
            f"Reference output target must be a regular file: {target_name}"
        )
    staged_path.chmod(stat.S_IMODE(target_stat.st_mode))


def _publish_csv_batch_at(
    outputs: list[tuple[Path, str]],
    *,
    directory_fd: int,
    verify_source: Any,
) -> None:
    transaction = uuid.uuid4().hex
    prepared: list[tuple[str, str | None]] = []
    try:
        for index, (staged_path, target_name) in enumerate(outputs):
            backup_name: str | None = None
            try:
                os.stat(target_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                backup_name = f".{target_name}.reference-backup-{transaction}-{index}"
                os.replace(
                    target_name,
                    backup_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            prepared.append((target_name, backup_name))
            os.replace(staged_path, target_name, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        verify_source()
    except BaseException:
        rollback_errors: list[OSError] = []
        # 从后向前撤销，确保第二个发布失败时第一个也恢复到原批次。
        for target_name, backup_name in reversed(prepared):
            try:
                try:
                    os.unlink(target_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                if backup_name is not None:
                    os.replace(
                        backup_name,
                        target_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
            except OSError as error:
                rollback_errors.append(error)
        os.fsync(directory_fd)
        if rollback_errors:
            raise RuntimeError(
                "Reference output publication failed and rollback was incomplete: "
                + "; ".join(str(error) for error in rollback_errors)
            )
        raise
    else:
        for _target_name, backup_name in prepared:
            if backup_name is not None:
                os.unlink(backup_name, dir_fd=directory_fd)
        os.fsync(directory_fd)


def _tag_rows_in_place(
    question_id: str,
    variant: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # 查询结果由当前分析流程独占，原地补标签可避免复制整批行字典。
    for row in rows:
        row["query_id"] = question_id
        row["variant"] = variant
    return rows


def _metrics_row(
    question: QuestionSpec,
    variant: str,
    fragmentation_score: float,
    baseline_rows: list[dict[str, Any]],
    observed_rows: list[dict[str, Any]],
    temporal_metrics: TemporalMetrics,
) -> dict[str, Any]:
    metrics, missing, extra = compare_entity_sets(
        baseline_rows,
        observed_rows,
        entity_key=question.entity_key,
    )
    return {
        "query_id": question.question_id,
        "variant": variant,
        "fragmentation_score": fragmentation_score,
        "baseline_count": metrics.baseline_count,
        "returned_count": metrics.returned_count,
        "missed_count": len(missing),
        "extra_count": len(extra),
        "miss_rate": metrics.miss_rate,
        "jaccard": metrics.jaccard,
        **temporal_metrics.to_record(),
    }


def _fragmentation_score(manifest: dict[str, Any], variant_dir: Path) -> float:
    if manifest.get("fragmentation_score") is not None:
        return float(manifest["fragmentation_score"])
    return compute_fragmentation_score(
        variant_dir / "academic_records.csv",
        variant_dir / "financial_aid_records.csv",
    )
