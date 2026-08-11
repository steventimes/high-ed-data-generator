from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmark.evaluation.metrics import (
    VARIANT_NAMES,
    compare_entity_sets,
    compute_fragmentation_score,
    write_csv,
)
from benchmark.questions import QuestionSpec
from benchmark.sql_runtime import VariantSqlRuntime


def analyze_run(
    run_dir: Path,
    questions: list[QuestionSpec],
) -> dict[str, Path]:
    for question in questions:
        if question.reference_sql is None:
            raise ValueError(
                f"Question {question.question_id} does not define reference_sql"
            )

    fragmentation_scores = {
        variant: _fragmentation_score(run_dir, variant) for variant in VARIANT_NAMES
    }
    baseline_rows_by_question: dict[str, list[dict[str, Any]]] = {}
    result_chunks: dict[tuple[str, str], list[dict[str, Any]]] = {}
    metrics_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    # 基线只读取一次，随后逐个处理碎片版本，避免同时驻留四个 DuckDB 连接。
    with VariantSqlRuntime(run_dir / "variants" / "baseline") as runtime:
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
                fragmentation_scores["baseline"],
                rows,
                rows,
            )

    for variant in VARIANT_NAMES:
        if variant == "baseline":
            continue
        with VariantSqlRuntime(run_dir / "variants" / variant) as runtime:
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
                    fragmentation_scores[variant],
                    baseline_rows_by_question[question.question_id],
                    rows,
                )

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
    write_csv(
        metrics_path,
        metrics_rows,
        [
            "query_id",
            "variant",
            "fragmentation_score",
            "baseline_count",
            "returned_count",
            "missed_count",
            "extra_count",
            "miss_rate",
            "jaccard",
        ],
    )
    result_fields = ["query_id", "variant"]
    for question in questions:
        if question.entity_key not in result_fields:
            result_fields.append(question.entity_key)
    for row in result_rows:
        for key in row:
            if key not in result_fields:
                result_fields.append(key)
    write_csv(results_path, result_rows, result_fields)
    return {
        "reference_query_metrics": metrics_path,
        "reference_query_results": results_path,
    }


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
    }


def _fragmentation_score(run_dir: Path, variant: str) -> float:
    manifest_path = run_dir / "manifests" / f"{variant}_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("fragmentation_score") is not None:
            return float(manifest["fragmentation_score"])
    variant_dir = run_dir / "variants" / variant
    return compute_fragmentation_score(
        variant_dir / "academic_records.csv",
        variant_dir / "financial_aid_records.csv",
    )
