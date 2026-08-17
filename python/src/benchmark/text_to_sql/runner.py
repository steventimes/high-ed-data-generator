from __future__ import annotations

import shutil
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb

from benchmark.evaluation.metrics import VARIANT_NAMES, write_csv
from benchmark.generated_batch import (
    build_target_batch_identity,
    write_generated_batch_contract,
)
from benchmark.questions import QuestionSpec, slugify, validate_questions
from benchmark.sql_runtime import (
    SqlValidationError,
    VariantSqlRuntime,
    require_matching_cohorts,
    run_sql_on_variant,
    validate_read_only_sql,
)
from benchmark.text_to_sql.prompts import build_schema_context


@dataclass(frozen=True)
class TextToSqlTarget:
    label: str
    variant_dir: Path


@dataclass(frozen=True)
class TextToSqlResult:
    question_id: str
    question: str
    entity_key: str
    target_label: str
    variant_dir: str
    model: str
    generated_sql: str | None
    success: bool
    error: str | None
    reference_count: int
    generated_count: int | None
    missing_count: int | None
    extra_count: int | None
    matches_reference_ids: bool | None
    missing_entity_ids: tuple[str, ...] = ()
    extra_entity_ids: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "entity_key": self.entity_key,
            "target_label": self.target_label,
            "variant_dir": self.variant_dir,
            "model": self.model,
            "generated_sql": self.generated_sql or "",
            "success": self.success,
            "error": self.error or "",
            "reference_count": self.reference_count,
            "generated_count": self.generated_count,
            "missing_count": self.missing_count,
            "extra_count": self.extra_count,
            "matches_reference_ids": self.matches_reference_ids,
            "missing_entity_ids": ";".join(self.missing_entity_ids),
            "extra_entity_ids": ";".join(self.extra_entity_ids),
        }


class SqlGenerator(Protocol):
    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str: ...

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str: ...


class _HiddenResultMismatch(ValueError):
    """候选行动集不匹配；异常文本不得携带任何 reference 真值。"""


def run_text_to_sql_experiment(
    *,
    questions: list[QuestionSpec],
    targets: list[TextToSqlTarget],
    generator: SqlGenerator,
    model: str,
    max_retries: int,
    generated_results_dir: Path | None = None,
) -> list[TextToSqlResult]:
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    validate_questions(questions)
    _validate_targets(targets)
    if generated_results_dir is not None:
        require_publishable_targets(targets)
    schema_context = build_schema_context()

    with ExitStack() as stack:
        # 每个部门/变体只注册一次 CSV；模型修复重试复用同一受控连接。
        runtimes = {
            target.label: stack.enter_context(VariantSqlRuntime(target.variant_dir))
            for target in targets
        }
        # 在清理旧结果和执行任何查询前，确认所有目标属于同一生成批次。
        require_matching_cohorts(
            [
                (target.label, runtimes[target.label].cohort_fingerprint)
                for target in targets
            ]
        )
        return _run_text_to_sql_with_runtimes(
            questions=questions,
            targets=targets,
            generator=generator,
            model=model,
            max_retries=max_retries,
            generated_results_dir=generated_results_dir,
            schema_context=schema_context,
            runtimes=runtimes,
        )


def _run_text_to_sql_with_runtimes(
    *,
    questions: list[QuestionSpec],
    targets: list[TextToSqlTarget],
    generator: SqlGenerator,
    model: str,
    max_retries: int,
    generated_results_dir: Path | None,
    schema_context: str,
    runtimes: dict[str, VariantSqlRuntime],
) -> list[TextToSqlResult]:
    if generated_results_dir is None:
        return _execute_text_to_sql_batch(
            questions=questions,
            targets=targets,
            generator=generator,
            model=model,
            max_retries=max_retries,
            output_dir=None,
            schema_context=schema_context,
            runtimes=runtimes,
        )

    destination = _validate_generated_results_directory(
        generated_results_dir, targets=targets
    )
    staging_dir = _create_generation_staging_directory(destination)
    try:
        results = _execute_text_to_sql_batch(
            questions=questions,
            targets=targets,
            generator=generator,
            model=model,
            max_retries=max_retries,
            output_dir=staging_dir,
            schema_context=schema_context,
            runtimes=runtimes,
        )
        if all(result.success for result in results):
            write_generated_batch_contract(
                staging_dir,
                question_ids=[question.question_id for question in questions],
                question_specs=questions,
                targets=[
                    (
                        target.label,
                        build_target_batch_identity(
                            fallback_variant=target.variant_dir.name,
                            cohort_fingerprint=getattr(
                                runtimes[target.label], "cohort_fingerprint", None
                            ),
                            manifest=getattr(runtimes[target.label], "manifest", {}),
                            variant_dir=target.variant_dir,
                        ),
                    )
                    for target in targets
                ],
                result_files=list(staging_dir.glob("*.csv")),
            )
            # 只有整批结果均满足契约后才切换目录，评估端永远看不到半批数据。
            _publish_generation_directory(staging_dir, destination)
            staging_dir = None
        return results
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)


def _execute_text_to_sql_batch(
    *,
    questions: list[QuestionSpec],
    targets: list[TextToSqlTarget],
    generator: SqlGenerator,
    model: str,
    max_retries: int,
    output_dir: Path | None,
    schema_context: str,
    runtimes: dict[str, VariantSqlRuntime],
) -> list[TextToSqlResult]:
    results = []
    for question in questions:
        reference_sql = _reference_sql(question)
        reference_rows = {
            target.label: runtimes[target.label].execute(
                reference_sql,
                required_columns=(question.entity_key,),
            )
            for target in targets
        }
        reference_ids = {
            target.label: entity_ids(reference_rows[target.label], question.entity_key)
            for target in targets
        }
        generated_sql, generation_error = generate_question_sql(
            question=question,
            generator=generator,
            schema_context=schema_context,
            targets=targets,
            reference_ids_by_target=reference_ids,
            max_retries=max_retries,
            runtimes=runtimes,
        )

        for target in targets:
            if generated_sql is None or generation_error is not None:
                results.append(
                    _failed_result(
                        question=question,
                        target=target,
                        model=model,
                        generated_sql=generated_sql,
                        error=generation_error or "SQL generation failed",
                        reference_count=len(reference_rows[target.label]),
                    )
                )
                continue
            output_csv = (
                _generated_result_path(
                    output_dir,
                    question.question_id,
                    target.label,
                )
                if output_dir is not None
                else None
            )
            results.append(
                run_generated_sql_against_target(
                    question=question,
                    target=target,
                    model=model,
                    generated_sql=generated_sql,
                    reference_rows=reference_rows[target.label],
                    reference_ids=reference_ids[target.label],
                    output_csv=output_csv,
                    runtime=runtimes[target.label],
                )
            )
    return results


def _validate_generated_results_directory(
    directory: Path,
    *,
    targets: list[TextToSqlTarget],
    run_dirs: list[Path] | None = None,
) -> Path:
    if not directory.name or directory.name in {".", ".."} or ".." in directory.parts:
        raise ValueError("generated_results_dir must name a dedicated directory")
    if directory.is_symlink():
        raise ValueError("generated_results_dir must not be a symbolic link")
    destination = directory.resolve(strict=False)
    if destination.parent == destination or destination == Path.cwd().resolve():
        raise ValueError("generated_results_dir must not be a filesystem root or cwd")
    target_dirs = {target.variant_dir.resolve(strict=False) for target in targets}
    run_roots = {run_dir.resolve(strict=False) for run_dir in run_dirs or []}
    run_roots.update(
        target_dir.parent.parent
        for target_dir in target_dirs
        if target_dir.parent.name == "variants"
    )
    protected_paths = set(target_dirs)
    for run_root in run_roots:
        protected_paths.update(
            {
                run_root / "variants",
                run_root / "manifests",
                run_root / "config_snapshot",
            }
        )
        metrics_root = run_root / "metrics"
        if metrics_root == destination or metrics_root.is_relative_to(destination):
            raise ValueError(
                "generated_results_dir must not equal or contain the metrics root"
            )
    for protected in protected_paths:
        if (
            protected == destination
            or protected.is_relative_to(destination)
            or destination.is_relative_to(protected)
        ):
            raise ValueError(
                "generated_results_dir must not overlap benchmark input trees"
            )
    if destination.exists() and not destination.is_dir():
        raise ValueError("generated_results_dir must be a directory")
    return destination


def _create_generation_staging_directory(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        )
    )


_FileIdentity = tuple[int, int]


def _file_identity(path: Path) -> _FileIdentity | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino


def _remove_owned_directory(path: Path, identity: _FileIdentity) -> None:
    # 路径名可能已被并发发布者复用；只有 dev/inode 仍属于本事务时才允许删除。
    if _file_identity(path) != identity:
        raise RuntimeError("directory ownership changed before cleanup")
    shutil.rmtree(path)


def _rollback_generation_publish(
    *,
    destination: Path,
    backup: Path,
    staged_identity: _FileIdentity,
    previous_identity: _FileIdentity | None,
) -> None:
    current_identity = _file_identity(destination)
    if current_identity == staged_identity:
        _remove_owned_directory(destination, staged_identity)
        current_identity = _file_identity(destination)
    elif previous_identity is not None and current_identity == previous_identity:
        # 旧批次已经在目标路径，无需再从备份恢复。
        if _file_identity(backup) is not None:
            raise RuntimeError("backup ownership is ambiguous during rollback")
        return

    if current_identity is not None:
        raise RuntimeError("destination ownership changed during rollback")
    if previous_identity is None:
        return
    if _file_identity(backup) != previous_identity:
        raise RuntimeError("backup ownership changed during rollback")

    backup.replace(destination)
    if _file_identity(destination) != previous_identity:
        raise RuntimeError("restored directory ownership could not be verified")


def _publish_generation_directory(staging_dir: Path, destination: Path) -> None:
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
    staged_identity = _file_identity(staging_dir)
    if staged_identity is None:
        raise ValueError("generation staging directory does not exist")
    previous_identity = _file_identity(destination)
    try:
        if previous_identity is not None:
            destination.replace(backup)
            if _file_identity(backup) != previous_identity:
                raise RuntimeError("backup ownership changed during publish")
        staging_dir.replace(destination)
        if _file_identity(destination) != staged_identity:
            raise RuntimeError("destination ownership changed during publish")
    except BaseException:
        try:
            # rename 成功后仍可能抛错；按事务保存的身份决定是否有权回滚。
            _rollback_generation_publish(
                destination=destination,
                backup=backup,
                staged_identity=staged_identity,
                previous_identity=previous_identity,
            )
        except BaseException as rollback_error:
            raise RuntimeError(
                "Failed to publish generated results because directory ownership "
                "changed during rollback"
            ) from rollback_error
        raise
    else:
        if previous_identity is not None:
            _remove_owned_directory(backup, previous_identity)


def generate_question_sql(
    *,
    question: QuestionSpec,
    generator: SqlGenerator,
    schema_context: str,
    targets: list[TextToSqlTarget],
    reference_ids_by_target: dict[str, set[str]],
    max_retries: int,
    runtimes: dict[str, VariantSqlRuntime],
) -> tuple[str | None, str | None]:
    generated_sql: str | None = None
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        try:
            if attempt == 0 or generated_sql is None:
                generated_sql = generator.generate_sql(
                    question=question,
                    schema_context=schema_context,
                )
            else:
                generated_sql = generator.repair_sql(
                    question=question,
                    schema_context=schema_context,
                    previous_sql=generated_sql,
                    error=last_error or "Previous SQL did not satisfy the benchmark",
                )
            validate_generated_sql_against_targets(
                question=question,
                generated_sql=generated_sql,
                targets=targets,
                reference_ids_by_target=reference_ids_by_target,
                runtimes=runtimes,
            )
            return generated_sql, None
        except _HiddenResultMismatch as error:
            # reference 行动集属于评测金标，不能成为下一轮模型的修复 oracle。
            return generated_sql, str(error)
        except Exception as error:  # noqa: BLE001
            if generated_sql is None:
                # 模型自身失败时下一轮重新生成，不会把该文本回传到 prompt。
                last_error = str(error)
                continue
            repair_feedback = _safe_repair_feedback(error)
            if repair_feedback is None:
                return generated_sql, "Generated SQL validation failed"
            last_error = repair_feedback
    return generated_sql, last_error or "SQL generation failed"


def _safe_repair_feedback(error: Exception) -> str | None:
    """只允许结构类错误进入模型 prompt，绝不携带数据行值。"""
    message = str(error)
    if isinstance(error, SqlValidationError):
        return message
    if isinstance(error, TimeoutError):
        return "Generated SQL exceeded the benchmark query timeout"
    if isinstance(error, duckdb.BinderException):
        return "Generated SQL does not bind to the benchmark schema"
    if isinstance(error, duckdb.Error):
        return "Generated SQL failed during benchmark execution"
    if not isinstance(error, ValueError):
        return None
    if message.startswith("SQL result contains duplicate ") and ":" in message:
        return "Generated SQL returned duplicate entity rows"
    if message.startswith("SQL result contains duplicate column names"):
        return "Generated SQL returned duplicate output columns"
    if message.startswith(
        (
            "SQL result is missing required columns:",
            "Generated result must include ",
        )
    ):
        return "Generated SQL result is missing the required entity column"
    if message.startswith("Generated result contains an empty "):
        return "Generated SQL returned an empty entity identifier"
    if message.startswith("SQL result exceeds maximum of "):
        return "Generated SQL returned too many rows"
    return None


def validate_generated_sql_against_targets(
    *,
    question: QuestionSpec,
    generated_sql: str,
    targets: list[TextToSqlTarget],
    reference_ids_by_target: dict[str, set[str]],
    runtimes: dict[str, VariantSqlRuntime] | None = None,
) -> None:
    validated_sql = validate_read_only_sql(generated_sql)
    mismatches = []
    for target in targets:
        generated_ids = entity_ids(
            _execute_target_sql(
                target=target,
                sql=validated_sql,
                required_columns=(question.entity_key,),
                runtime=runtimes.get(target.label) if runtimes else None,
            ),
            question.entity_key,
        )
        expected_ids = reference_ids_by_target[target.label]
        missing = sorted(expected_ids - generated_ids)
        extra = sorted(generated_ids - expected_ids)
        if missing or extra:
            mismatches.append(target.label)
    if mismatches:
        raise _HiddenResultMismatch(
            "Generated SQL does not match the hidden benchmark action set"
        )


def run_generated_sql_against_target(
    *,
    question: QuestionSpec,
    target: TextToSqlTarget,
    model: str,
    generated_sql: str,
    reference_rows: list[dict[str, Any]],
    reference_ids: set[str],
    output_csv: Path | None,
    runtime: VariantSqlRuntime | None = None,
) -> TextToSqlResult:
    try:
        generated_rows = _execute_target_sql(
            target=target,
            sql=generated_sql,
            output_csv=output_csv,
            required_columns=(question.entity_key,),
            runtime=runtime,
        )
        generated_ids = entity_ids(generated_rows, question.entity_key)
        missing = tuple(sorted(reference_ids - generated_ids))
        extra = tuple(sorted(generated_ids - reference_ids))
        matches_reference = not missing and not extra
        if not matches_reference and output_csv is not None:
            output_csv.unlink(missing_ok=True)
        mismatch_error = (
            None
            if matches_reference
            else "Final generated result does not match the reference entity set"
        )
        return TextToSqlResult(
            question_id=question.question_id,
            question=question.question,
            entity_key=question.entity_key,
            target_label=target.label,
            variant_dir=str(target.variant_dir),
            model=model,
            generated_sql=generated_sql,
            success=matches_reference,
            error=mismatch_error,
            reference_count=len(reference_rows),
            generated_count=len(generated_rows),
            missing_count=len(missing),
            extra_count=len(extra),
            matches_reference_ids=matches_reference,
            missing_entity_ids=missing,
            extra_entity_ids=extra,
        )
    except Exception as error:  # noqa: BLE001
        if output_csv is not None:
            # SQL 已执行但结果契约校验失败时，不留下可被后续评估误读的半成品。
            output_csv.unlink(missing_ok=True)
        return _failed_result(
            question=question,
            target=target,
            model=model,
            generated_sql=generated_sql,
            error=str(error),
            reference_count=len(reference_rows),
        )


def _execute_target_sql(
    *,
    target: TextToSqlTarget,
    sql: str,
    output_csv: Path | None = None,
    required_columns: tuple[str, ...] = (),
    runtime: VariantSqlRuntime | None = None,
) -> list[dict[str, Any]]:
    if runtime is not None:
        return runtime.execute(
            sql,
            output_csv=output_csv,
            required_columns=required_columns,
        )
    return run_sql_on_variant(
        target.variant_dir,
        sql,
        output_csv=output_csv,
        required_columns=required_columns,
    )


def resolve_targets(
    *,
    run_dir: Path,
    variants: list[str] | None = None,
    explicit_targets: list[str] | None = None,
) -> list[TextToSqlTarget]:
    if explicit_targets:
        targets = [parse_target_spec(value) for value in explicit_targets]
    else:
        targets = [
            TextToSqlTarget(label=variant, variant_dir=run_dir / "variants" / variant)
            for variant in (variants or list(VARIANT_NAMES))
        ]
    _validate_targets(targets)
    return targets


def parse_target_spec(value: str) -> TextToSqlTarget:
    label, separator, path = value.partition("=")
    if not separator or not label.strip() or not path.strip():
        raise ValueError("target must use label=variant_dir")
    return TextToSqlTarget(label=label.strip(), variant_dir=Path(path.strip()))


def entity_ids(rows: list[dict[str, Any]], entity_key: str) -> set[str]:
    identifiers = set()
    for row in rows:
        if entity_key not in row:
            raise ValueError(f"Generated result must include {entity_key}")
        value = row[entity_key]
        if value is None or not str(value).strip():
            raise ValueError(f"Generated result contains an empty {entity_key}")
        identifier = str(value).strip()
        if identifier in identifiers:
            # 行数和行动集大小必须使用同一口径；重复实体通常意味着连接放大。
            raise ValueError(
                f"SQL result contains duplicate {entity_key}: {identifier}"
            )
        identifiers.add(identifier)
    return identifiers


def write_results(path: Path, results: list[TextToSqlResult]) -> None:
    rows = [result.to_record() for result in results]
    fieldnames = list(TextToSqlResult.__dataclass_fields__)
    write_csv(path, rows, fieldnames)


def _generated_result_path(
    directory: Path, question_id: str, target_label: str
) -> Path:
    return directory / f"{slugify(question_id)}__{slugify(target_label)}.csv"


def _reference_sql(question: QuestionSpec) -> str:
    if question.reference_sql is None:
        raise ValueError(
            f"Question {question.question_id} does not define reference_sql"
        )
    return validate_read_only_sql(question.reference_sql)


def require_publishable_targets(targets: list[TextToSqlTarget]) -> None:
    """保证落盘批次能被同一套 evaluation 入口完整消费。"""
    baseline_targets = [target for target in targets if target.label == "baseline"]
    if not baseline_targets:
        raise ValueError("Published text-to-SQL targets must include baseline")
    baseline = baseline_targets[0]
    if baseline.variant_dir.resolve(strict=False).name != "baseline":
        # evaluation 以 baseline 标签作为比较真值，不能允许别的变体冒充。
        raise ValueError(
            "Published text-to-SQL baseline label must point to the actual baseline"
        )


def _validate_targets(targets: list[TextToSqlTarget]) -> None:
    if not targets:
        raise ValueError("At least one target variant is required")
    labels = [target.label for target in targets]
    if len(labels) != len(set(labels)):
        raise ValueError("Target labels must be unique")
    filename_labels = [slugify(label) for label in labels]
    if len(filename_labels) != len(set(filename_labels)):
        raise ValueError("Target labels collide after filename normalization")
    missing = [
        str(target.variant_dir) for target in targets if not target.variant_dir.is_dir()
    ]
    if missing:
        raise FileNotFoundError("Missing variant directories: " + ", ".join(missing))


def _failed_result(
    *,
    question: QuestionSpec,
    target: TextToSqlTarget,
    model: str,
    generated_sql: str | None,
    error: str,
    reference_count: int,
) -> TextToSqlResult:
    return TextToSqlResult(
        question_id=question.question_id,
        question=question.question,
        entity_key=question.entity_key,
        target_label=target.label,
        variant_dir=str(target.variant_dir),
        model=model,
        generated_sql=generated_sql,
        success=False,
        error=error,
        reference_count=reference_count,
        generated_count=None,
        missing_count=None,
        extra_count=None,
        matches_reference_ids=None,
    )
