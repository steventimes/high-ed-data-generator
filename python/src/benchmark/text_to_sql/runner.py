from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from benchmark.evaluation.metrics import VARIANT_NAMES, write_csv
from benchmark.questions import QuestionSpec, slugify, validate_questions
from benchmark.sql_runtime import (
    VariantSqlRuntime,
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
    schema_context = build_schema_context()

    with ExitStack() as stack:
        # 每个部门/变体只注册一次 CSV；模型修复重试复用同一受控连接。
        runtimes = {
            target.label: stack.enter_context(VariantSqlRuntime(target.variant_dir))
            for target in targets
        }
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
    results = []
    if generated_results_dir is not None:
        # 整批执行前清空所有目标，避免中途异常让后续问题误用上一轮结果。
        for question in questions:
            for target in targets:
                _generated_result_path(
                    generated_results_dir,
                    question.question_id,
                    target.label,
                ).unlink(missing_ok=True)

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
                    generated_results_dir,
                    question.question_id,
                    target.label,
                )
                if generated_results_dir is not None
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
        except Exception as error:  # noqa: BLE001
            last_error = str(error)
    return generated_sql, last_error or "SQL generation failed"


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
            parts = []
            if missing:
                parts.append(f"missing={len(missing)} sample={','.join(missing[:5])}")
            if extra:
                parts.append(f"extra={len(extra)} sample={','.join(extra[:5])}")
            mismatches.append(f"{target.label}: {'; '.join(parts)}")
    if mismatches:
        raise ValueError(
            "Generated SQL changes the benchmark action set across variants. "
            "Missing joined aid rows must not become positive matches. "
            + " | ".join(mismatches)
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
        identifiers.add(str(value).strip())
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
