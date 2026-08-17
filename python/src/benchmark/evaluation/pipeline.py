from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.evaluation.metrics import (
    VARIANT_NAMES,
    build_weight_lookup_from_rows,
    compare_entity_sets,
    compute_fragmentation_score_from_rows,
    fragmentation_level_for_variant,
    read_csv_rows,
    write_csv,
)
from benchmark.evaluation.plots import (
    CAUSE_ORDER,
    plot_cause_breakdown,
    plot_miss_rate_bars,
)
from benchmark.generated_batch import (
    TargetBatchIdentity,
    build_target_batch_identity,
    snapshot_generated_batch,
)
from benchmark.questions import QuestionSpec, slugify
from benchmark.sql_runtime import VariantSqlRuntime, require_matching_cohorts
from benchmark.temporal import (
    TEMPORAL_METRIC_FIELDS,
    compute_temporal_metrics,
)

_ALLOWED_PLOT_FORMATS = {"pdf", "png", "svg"}


@dataclass(frozen=True)
class _EvaluationTarget:
    label: str
    runtime: VariantSqlRuntime
    manifest: dict[str, Any]
    fragmentation_score: float


def evaluate_text_to_sql_outputs(
    *,
    run_dir: Path,
    questions: list[QuestionSpec],
    generated_results_dir: Path,
    output_dir: Path,
    plot_formats: list[str],
    strict: bool,
    variants: list[str] | None = None,
    targets: list[tuple[str, Path]] | None = None,
) -> dict[str, Path]:
    if targets is not None and variants is not None:
        raise ValueError("evaluation accepts either variants or targets, not both")
    selected_variants = list(VARIANT_NAMES if variants is None else variants)
    selected_targets = (
        list(targets)
        if targets is not None
        else [
            (variant, run_dir / "variants" / variant) for variant in selected_variants
        ]
    )
    labels = [label for label, _ in selected_targets]
    if (
        not labels
        or "baseline" not in labels
        or len(labels) != len(set(labels))
        or any(not label.strip() for label in labels)
        or len({slugify(label) for label in labels}) != len(labels)
    ):
        raise ValueError("evaluation targets must be unique and include baseline")
    if targets is None and any(variant not in VARIANT_NAMES for variant in labels):
        raise ValueError("evaluation variants must be known")
    output_dir = _validate_evaluation_output_dir(
        output_dir,
        run_dir=run_dir,
        protected_paths=[
            run_dir / "variants",
            run_dir / "manifests",
            run_dir / "metrics",
            run_dir / "config_snapshot",
            generated_results_dir,
            *[variant_dir for _, variant_dir in selected_targets],
        ],
    )
    # 评估读取生成结果前先锁定同一 cohort，避免跨批次比较产生伪差异。
    return _evaluate_with_frozen_targets(
        questions=questions,
        selected_targets=selected_targets,
        generated_results_dir=generated_results_dir,
        output_dir=output_dir,
        plot_formats=plot_formats,
        strict=strict,
    )


def _evaluate_with_frozen_targets(
    *,
    questions: list[QuestionSpec],
    selected_targets: list[tuple[str, Path]],
    generated_results_dir: Path,
    output_dir: Path,
    plot_formats: list[str],
    strict: bool,
) -> dict[str, Path]:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as runtime_stack:
        evaluation_targets: list[_EvaluationTarget] = []
        target_identities = []
        labeled_fingerprints = []

        for label, variant_dir in selected_targets:
            runtime = runtime_stack.enter_context(VariantSqlRuntime(variant_dir))
            identity = build_target_batch_identity(
                fallback_variant=variant_dir.name,
                cohort_fingerprint=runtime.cohort_fingerprint,
                manifest=runtime.manifest,
                variant_dir=variant_dir,
            )
            if label == "baseline" and identity.variant != "baseline":
                raise ValueError(
                    "evaluation baseline target must identify the baseline variant"
                )

            manifest = runtime.manifest
            fragmentation_score = manifest.get("fragmentation_score")
            if fragmentation_score is None:
                fragmentation_score = compute_fragmentation_score_from_rows(
                    runtime.materialized_rows("academic_records"),
                    runtime.materialized_rows("financial_aid_records"),
                )
            evaluation_targets.append(
                _EvaluationTarget(
                    label=label,
                    runtime=runtime,
                    manifest=manifest,
                    fragmentation_score=float(fragmentation_score),
                )
            )
            target_identities.append((label, identity))
            labeled_fingerprints.append((label, runtime.cohort_fingerprint))

        require_matching_cohorts(labeled_fingerprints)
        baseline_runtime = next(
            target.runtime
            for target in evaluation_targets
            if target.label == "baseline"
        )
        baseline_academic_rows = baseline_runtime.materialized_rows("academic_records")
        weight_lookups = {
            question.question_id: build_weight_lookup_from_rows(
                baseline_academic_rows,
                entity_key=question.entity_key,
                weighting_policy=question.weighting_policy,
            )
            for question in questions
        }

        # runtime 与生成结果快照同时存活，原子替换源 run 也不能混入另一批数据。
        with snapshot_generated_batch(
            generated_results_dir,
            snapshot_parent=output_dir.parent,
            question_ids=[question.question_id for question in questions],
            question_specs=questions,
            targets=target_identities,
            strict=strict,
        ) as generated_snapshot:
            staging_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{output_dir.name}.staging-",
                    dir=output_dir.parent,
                )
            )
            try:
                staged_paths = _write_evaluation_output_tree(
                    questions=questions,
                    generated_results_dir=generated_snapshot,
                    output_dir=staging_dir,
                    plot_formats=plot_formats,
                    strict=strict,
                    targets=evaluation_targets,
                    weight_lookups=weight_lookups,
                )
                _ensure_evaluation_targets_unchanged(
                    selected_targets,
                    target_identities,
                )
                relative_paths = {
                    label: path.relative_to(staging_dir)
                    for label, path in staged_paths.items()
                }
                _publish_evaluation_output(staging_dir, output_dir)
                return {
                    label: output_dir / relative_path
                    for label, relative_path in relative_paths.items()
                }
            finally:
                # 只清理本次随机 staging；异常时保留此前已发布的完整批次。
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)


def _ensure_evaluation_targets_unchanged(
    selected_targets: list[tuple[str, Path]],
    expected_identities: list[tuple[str, TargetBatchIdentity]],
) -> None:
    try:
        current_identities = []
        for label, variant_dir in selected_targets:
            # 逐个复验，避免大数据集同时额外持有四个 DuckDB 快照。
            with VariantSqlRuntime(variant_dir) as runtime:
                identity = build_target_batch_identity(
                    fallback_variant=variant_dir.name,
                    cohort_fingerprint=runtime.cohort_fingerprint,
                    manifest=runtime.manifest,
                    variant_dir=variant_dir,
                )
            current_identities.append((label, identity))
    except Exception as error:
        raise RuntimeError("Benchmark targets changed during evaluation") from error

    if current_identities != expected_identities:
        # 生成结果和 target 必须来自同一稳定批次；换代后不得发布旧快照指标。
        raise RuntimeError("Benchmark targets changed during evaluation")


def _validate_evaluation_output_dir(
    output_dir: Path, *, run_dir: Path, protected_paths: list[Path]
) -> Path:
    raw = os.fspath(output_dir)
    if not raw or raw == "." or output_dir == Path("/") or ".." in output_dir.parts:
        raise ValueError("output_dir must be a specific safe directory")
    destination = output_dir.resolve(strict=False)
    canonical_run_dir = run_dir.resolve(strict=False)
    if canonical_run_dir == destination or canonical_run_dir.is_relative_to(
        destination
    ):
        raise ValueError("output_dir must not equal or contain run_dir")
    for protected in protected_paths:
        protected = protected.resolve(strict=False)
        if (
            protected == destination
            or protected.is_relative_to(destination)
            or destination.is_relative_to(protected)
        ):
            raise ValueError(
                "output_dir must not overlap benchmark input or generated results"
            )
    if output_dir.is_symlink():
        raise ValueError("output_dir must not be a symbolic link")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("output_dir must be a directory")
    return destination


def _publish_evaluation_output(staging_dir: Path, output_dir: Path) -> None:
    backup_dir: Path | None = None
    if output_dir.exists():
        # 先在同一父目录预留唯一名称，随后以原子 rename 保存旧的完整目录。
        backup_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{output_dir.name}.backup-",
                dir=output_dir.parent,
            )
        )
        backup_dir.rmdir()
        os.replace(output_dir, backup_dir)

    try:
        os.replace(staging_dir, output_dir)
    except BaseException:
        if backup_dir is not None and backup_dir.exists():
            try:
                os.replace(backup_dir, output_dir)
            except BaseException as restore_error:
                raise RuntimeError(
                    "Evaluation publication and rollback both failed; "
                    f"the previous output remains at {backup_dir}"
                ) from restore_error
        raise

    if backup_dir is not None:
        try:
            shutil.rmtree(backup_dir)
        except OSError:
            # 新目录已经完整发布；旧备份清理失败不应把成功发布误报为失败。
            pass


def _write_evaluation_output_tree(
    *,
    questions: list[QuestionSpec],
    generated_results_dir: Path,
    output_dir: Path,
    plot_formats: list[str],
    strict: bool,
    targets: list[_EvaluationTarget],
    weight_lookups: dict[str, dict[str, float]],
) -> dict[str, Path]:
    normalized_plot_formats = [value.strip().casefold() for value in plot_formats]
    invalid_plot_formats = sorted(set(normalized_plot_formats) - _ALLOWED_PLOT_FORMATS)
    if invalid_plot_formats:
        raise ValueError("Unsupported plot format: " + ", ".join(invalid_plot_formats))
    plot_formats = normalized_plot_formats
    metrics_dir = output_dir / "metrics"
    figures_dir = output_dir / "figures"
    reports_dir = output_dir / "reports"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    per_query_metrics_rows: list[dict[str, Any]] = []
    missed_students_rows: list[dict[str, Any]] = []
    extra_students_rows: list[dict[str, Any]] = []
    summary_sections: list[str] = []

    for question in questions:
        question_outputs = evaluate_single_question(
            question=question,
            generated_results_dir=generated_results_dir,
            figures_dir=figures_dir,
            reports_dir=reports_dir,
            plot_formats=plot_formats,
            strict=strict,
            single_query=len(questions) == 1,
            targets=targets,
            weight_lookup=weight_lookups[question.question_id],
        )
        if question_outputs is None:
            continue
        metrics_rows, missed_rows, extra_rows, summary_text = question_outputs
        per_query_metrics_rows.extend(metrics_rows)
        missed_students_rows.extend(missed_rows)
        extra_students_rows.extend(extra_rows)
        summary_sections.append(summary_text)

    if not summary_sections:
        raise ValueError("No generated results were available for evaluation")

    per_query_metrics_path = metrics_dir / "per_query_metrics.csv"
    missed_students_path = metrics_dir / "missed_students.csv"
    extra_students_path = metrics_dir / "extra_students.csv"
    summary_path = reports_dir / "summary.md"

    write_csv(
        per_query_metrics_path,
        per_query_metrics_rows,
        [
            "query_id",
            "question",
            "institution_role",
            "decision_type",
            "variant_name",
            "fragmentation_level",
            "fragmentation_score",
            "baseline_count",
            "returned_count",
            "tp",
            "fn",
            "fp",
            "miss_rate",
            "recall",
            "extra_rate",
            "jaccard",
            "weighted_miss_loss",
            "weighted_extra",
            *TEMPORAL_METRIC_FIELDS,
        ],
    )
    write_csv(
        missed_students_path,
        missed_students_rows,
        [
            "query_id",
            "variant_name",
            "fragmentation_level",
            "student_id",
            "baseline_weight",
            "cause",
        ],
    )
    write_csv(
        extra_students_path,
        extra_students_rows,
        [
            "query_id",
            "variant_name",
            "fragmentation_level",
            "student_id",
            "cause",
        ],
    )
    summary_path.write_text(
        "\n\n".join(summary_sections).strip() + "\n", encoding="utf-8"
    )

    return {
        "per_query_metrics": per_query_metrics_path,
        "missed_students": missed_students_path,
        "extra_students": extra_students_path,
        "summary": summary_path,
    }


def evaluate_single_question(
    *,
    question: QuestionSpec,
    generated_results_dir: Path,
    figures_dir: Path,
    reports_dir: Path,
    plot_formats: list[str],
    strict: bool,
    single_query: bool,
    targets: list[_EvaluationTarget],
    weight_lookup: dict[str, float],
) -> (
    tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str] | None
):
    baseline_variant = "baseline"
    baseline_path = generated_result_path(
        generated_results_dir, question.question_id, baseline_variant
    )
    if not baseline_path.exists():
        return handle_missing(
            strict,
            f"Missing baseline generated result for {question.question_id}: {baseline_path}",
        )

    baseline_rows = read_csv_rows(baseline_path)

    metrics_rows: list[dict[str, Any]] = []
    missed_rows: list[dict[str, Any]] = []
    extra_rows: list[dict[str, Any]] = []
    cause_plot_rows: list[dict[str, Any]] = []

    for target in targets:
        variant = target.label
        result_path = generated_result_path(
            generated_results_dir, question.question_id, variant
        )
        if not result_path.exists():
            if strict:
                raise FileNotFoundError(
                    f"Missing generated result for {question.question_id}: {result_path}"
                )
            print(f"Warning: skipping missing generated result {result_path}")
            continue

        observed_rows = read_csv_rows(result_path)
        metrics, missing_ids, extra_ids = compare_entity_sets(
            baseline_rows,
            observed_rows,
            entity_key=question.entity_key,
            weight_lookup=weight_lookup or None,
        )

        fragmentation_level = fragmentation_level_for_variant(variant)
        # manifest、表和分数来自同一个仍存活的 runtime，不再按路径二次打开。
        manifest = target.manifest
        temporal_metrics = compute_temporal_metrics(
            manifest=manifest,
            temporal_evaluation=question.temporal_evaluation,
            entity_key=question.entity_key,
            execute=(
                target.runtime.execute
                if question.temporal_evaluation is not None
                else None
            ),
        )
        fragmentation_score = target.fragmentation_score

        cause_lookup = assign_missing_causes(
            missing_ids,
            manifest,
            remediated_causes=question.remediated_causes,
        )
        extra_cause_lookup = assign_extra_causes(
            extra_ids,
            manifest,
            remediated_causes=question.remediated_causes,
        )
        cause_plot_row = {
            "fragmentation_level": fragmentation_level,
            **{cause: 0 for cause in CAUSE_ORDER},
        }
        for cause in cause_lookup.values():
            cause_plot_row[cause] += 1
        cause_plot_rows.append(cause_plot_row)

        metrics_rows.append(
            {
                "query_id": question.question_id,
                "question": question.question,
                "institution_role": question.institution_role or "",
                "decision_type": question.decision_type or "",
                "variant_name": variant,
                "fragmentation_level": fragmentation_level,
                "fragmentation_score": fragmentation_score,
                "baseline_count": metrics.baseline_count,
                "returned_count": metrics.returned_count,
                "tp": metrics.tp,
                "fn": metrics.fn,
                "fp": metrics.fp,
                "miss_rate": metrics.miss_rate,
                "recall": metrics.recall,
                "extra_rate": metrics.extra_rate,
                "jaccard": metrics.jaccard,
                "weighted_miss_loss": metrics.weighted_miss_loss,
                "weighted_extra": metrics.weighted_extra,
                **temporal_metrics.to_record(),
            }
        )

        for student_id in missing_ids:
            missed_rows.append(
                {
                    "query_id": question.question_id,
                    "variant_name": variant,
                    "fragmentation_level": fragmentation_level,
                    "student_id": student_id,
                    "baseline_weight": weight_lookup.get(student_id, 1.0)
                    if weight_lookup
                    else "",
                    "cause": cause_lookup.get(student_id, ""),
                }
            )
        for student_id in extra_ids:
            extra_rows.append(
                {
                    "query_id": question.question_id,
                    "variant_name": variant,
                    "fragmentation_level": fragmentation_level,
                    "student_id": student_id,
                    "cause": extra_cause_lookup.get(student_id, ""),
                }
            )

    if not metrics_rows:
        return None

    for plot_format in plot_formats:
        plot_miss_rate_bars(
            metrics_rows,
            figures_dir
            / f"{slugify(question.question_id)}__main_miss_rate.{plot_format}",
            title=question.display_title,
        )

    if any(row.get(cause, 0) for row in cause_plot_rows for cause in CAUSE_ORDER):
        for plot_format in plot_formats:
            plot_cause_breakdown(
                cause_plot_rows,
                figures_dir
                / f"{slugify(question.question_id)}__cause_breakdown.{plot_format}",
                title=f"{question.display_title}: Why Students Were Missed",
            )

    summary_text = build_question_summary(question, metrics_rows)
    summary_path = reports_dir / f"{slugify(question.question_id)}__summary.md"
    caption_path = (
        reports_dir / f"{slugify(question.question_id)}__presentation_caption.txt"
    )
    summary_path.write_text(summary_text + "\n", encoding="utf-8")
    caption_path.write_text(
        build_presentation_caption(question, metrics_rows) + "\n", encoding="utf-8"
    )

    maybe_write_generic_plot_aliases(
        question=question,
        figures_dir=figures_dir,
        plot_formats=plot_formats,
        single_query=single_query,
    )

    return metrics_rows, missed_rows, extra_rows, summary_text


def maybe_write_generic_plot_aliases(
    *,
    question: QuestionSpec,
    figures_dir: Path,
    plot_formats: list[str],
    single_query: bool,
) -> None:
    if not single_query:
        return
    for plot_format in plot_formats:
        source = (
            figures_dir
            / f"{slugify(question.question_id)}__main_miss_rate.{plot_format}"
        )
        if source.exists():
            target = figures_dir / f"main_miss_rate.{plot_format}"
            target.write_bytes(source.read_bytes())


def generated_result_path(
    generated_results_dir: Path, question_id: str, variant: str
) -> Path:
    return generated_results_dir / f"{slugify(question_id)}__{slugify(variant)}.csv"


def assign_missing_causes(
    missing_ids: list[str],
    manifest: dict[str, Any],
    *,
    remediated_causes: frozenset[str] = frozenset(),
) -> dict[str, str]:
    selected = manifest.get("selected_row_ids", {})
    candidate_causes = [
        ("missing_record", normalize_ids(selected.get("drop_row", []))),
        (
            "publication_delay",
            normalize_ids(selected.get("publication_delay", [])),
        ),
        (
            "null_critical_field",
            normalize_ids(selected.get("null_aid_amount", []))
            | normalize_ids(selected.get("null_aid_status", [])),
        ),
        (
            "identity_mismatch",
            normalize_ids(selected.get("identifier_mismatch", [])),
        ),
        (
            "semantic_drift",
            normalize_ids(selected.get("aid_status_code_drift", [])),
        ),
    ]

    cause_lookup: dict[str, str] = {}
    for student_id in missing_ids:
        normalized = student_id.upper()
        cause_lookup[student_id] = next(
            (
                cause
                for cause, selected_ids in candidate_causes
                if cause not in remediated_causes and normalized in selected_ids
            ),
            "unknown",
        )
    return cause_lookup


def assign_extra_causes(
    extra_ids: list[str],
    manifest: dict[str, Any],
    *,
    remediated_causes: frozenset[str] = frozenset(),
) -> dict[str, str]:
    selected = manifest.get("selected_row_ids", {})
    semantic_drift_ids = normalize_ids(selected.get("aid_status_code_drift", []))
    semantic_drift_is_active = "semantic_drift" not in remediated_causes
    return {
        student_id: (
            "semantic_drift"
            if semantic_drift_is_active and student_id.upper() in semantic_drift_ids
            else "unknown"
        )
        for student_id in extra_ids
    }


def normalize_ids(values: list[Any]) -> set[str]:
    return {str(value).strip().upper() for value in values if str(value).strip()}


def build_question_summary(
    question: QuestionSpec, metrics_rows: list[dict[str, Any]]
) -> str:
    high_row = choose_summary_row(metrics_rows)
    miss_rate = format_percent(high_row.get("miss_rate"))
    text = (
        f"## {question.display_title}\n\n"
        f"Under {high_row['fragmentation_level']} fragmentation, the system missed "
        f"{int(high_row['fn'])} of {int(high_row['baseline_count'])} students that the baseline query would have "
        f"flagged, for a miss rate of {miss_rate}. This means the institution would fail to identify roughly "
        f"{miss_rate} of the students it intended to act on under clean data."
    )
    if high_row.get("weighted_miss_loss") not in (None, ""):
        weighted = format_percent(high_row.get("weighted_miss_loss"))
        text += (
            f"\n\nThe weighted intervention loss was {weighted}, indicating that fragmentation "
            "removed a disproportionate share of higher-priority students from the action set."
        )
    return text


def build_presentation_caption(
    question: QuestionSpec, metrics_rows: list[dict[str, Any]]
) -> str:
    summary_row = choose_summary_row(metrics_rows)
    return (
        f"For {question.display_title.lower()}, {summary_row['fragmentation_level']} fragmentation misses "
        f"{int(summary_row['fn'])} of {int(summary_row['baseline_count'])} students "
        f"({format_percent(summary_row.get('miss_rate'))})."
    )


def choose_summary_row(metrics_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_level = {row["fragmentation_level"]: row for row in metrics_rows}
    for preferred in ("high", "medium", "low", "baseline"):
        if preferred in by_level:
            return by_level[preferred]
    return metrics_rows[-1]


def format_percent(value: Any) -> str:
    if value in (None, ""):
        return "NA"
    return f"{float(value) * 100:.1f}%"


def handle_missing(strict: bool, message: str) -> None:
    if strict:
        raise FileNotFoundError(message)
    print(f"Warning: {message}")
