from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmark.evaluation.metrics import (
    VARIANT_NAMES,
    build_weight_lookup,
    compare_entity_sets,
    compute_fragmentation_score,
    fragmentation_level_for_variant,
    read_csv_rows,
    write_csv,
)
from benchmark.evaluation.plots import (
    CAUSE_ORDER,
    plot_cause_breakdown,
    plot_miss_rate_bars,
)
from benchmark.questions import QuestionSpec, slugify

_ALLOWED_PLOT_FORMATS = {"pdf", "png", "svg"}


def evaluate_text_to_sql_outputs(
    *,
    run_dir: Path,
    questions: list[QuestionSpec],
    generated_results_dir: Path,
    output_dir: Path,
    plot_formats: list[str],
    strict: bool,
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
            run_dir=run_dir,
            question=question,
            generated_results_dir=generated_results_dir,
            figures_dir=figures_dir,
            reports_dir=reports_dir,
            plot_formats=plot_formats,
            strict=strict,
            single_query=len(questions) == 1,
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
    run_dir: Path,
    question: QuestionSpec,
    generated_results_dir: Path,
    figures_dir: Path,
    reports_dir: Path,
    plot_formats: list[str],
    strict: bool,
    single_query: bool,
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
    academic_csv = run_dir / "variants" / baseline_variant / "academic_records.csv"
    weight_lookup = build_weight_lookup(
        academic_csv=academic_csv,
        entity_key=question.entity_key,
        weighting_policy=question.weighting_policy,
    )

    metrics_rows: list[dict[str, Any]] = []
    missed_rows: list[dict[str, Any]] = []
    extra_rows: list[dict[str, Any]] = []
    cause_plot_rows: list[dict[str, Any]] = []

    for variant in VARIANT_NAMES:
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
        manifest = load_variant_manifest(run_dir, variant)
        fragmentation_score = manifest.get("fragmentation_score")
        if fragmentation_score is None:
            fragmentation_score = compute_fragmentation_score(
                run_dir / "variants" / variant / "academic_records.csv",
                run_dir / "variants" / variant / "financial_aid_records.csv",
            )

        cause_lookup = assign_missing_causes(missing_ids, manifest)
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


def load_variant_manifest(run_dir: Path, variant: str) -> dict[str, Any]:
    manifest_path = run_dir / "manifests" / f"{variant}_manifest.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def assign_missing_causes(
    missing_ids: list[str], manifest: dict[str, Any]
) -> dict[str, str]:
    selected = manifest.get("selected_row_ids", {})
    missing_record_ids = normalize_ids(selected.get("drop_row", []))
    null_field_ids = normalize_ids(selected.get("null_aid_amount", [])) | normalize_ids(
        selected.get("null_aid_status", [])
    )
    identity_mismatch_ids = normalize_ids(selected.get("identifier_mismatch", []))

    cause_lookup: dict[str, str] = {}
    for student_id in missing_ids:
        normalized = student_id.upper()
        if normalized in missing_record_ids:
            cause_lookup[student_id] = "missing_record"
        elif normalized in null_field_ids:
            cause_lookup[student_id] = "null_critical_field"
        elif normalized in identity_mismatch_ids:
            cause_lookup[student_id] = "identity_mismatch"
        else:
            cause_lookup[student_id] = "unknown"
    return cause_lookup


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
