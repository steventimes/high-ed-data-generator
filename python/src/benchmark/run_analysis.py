from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from benchmark.duckdb_runner import EXPECTED_COLUMNS, run_canonical_query
from benchmark.evaluation.metrics import (
    VARIANT_NAMES,
    compute_fragmentation_score,
    missed_students_vs_baseline,
    write_csv,
)
from benchmark.reporting.tables import write_results_tables


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze a generated fragmentation benchmark run.")
    parser.add_argument("--run-dir", required=True, type=Path)
    return parser.parse_args()


def analyze_run(run_dir: Path) -> dict[str, Path]:
    metrics_dir = run_dir / "metrics"
    reports_dir = run_dir / "reports"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    query_rows_by_variant: dict[str, list[dict[str, Any]]] = {}
    combined_query_rows: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []

    for variant in VARIANT_NAMES:
        variant_dir = run_dir / "variants" / variant
        query_rows = run_canonical_query(variant_dir)
        query_rows_by_variant[variant] = query_rows
        for row in query_rows:
            combined_query_rows.append({"variant": variant, **row})

        score_rows.append(
            {
                "variant": variant,
                "fragmentation_score": compute_fragmentation_score(
                    variant_dir / "academic_records.csv",
                    variant_dir / "financial_aid_records.csv",
                ),
            }
        )

    baseline_rows = query_rows_by_variant["baseline"]
    missed_rows: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    for score_row in score_rows:
        variant = str(score_row["variant"])
        missed = missed_students_vs_baseline(baseline_rows, query_rows_by_variant[variant])
        missed_rows.append(
            {
                "variant": variant,
                "baseline_identified_count": len(baseline_rows),
                "variant_identified_count": len(query_rows_by_variant[variant]),
                "missed_count": len(missed),
                "missed_student_ids": ";".join(missed),
            }
        )
        table_rows.append(
            {
                "variant": variant,
                "fragmentation_score": score_row["fragmentation_score"],
                "identified_student_count": len(query_rows_by_variant[variant]),
                "missed_count": len(missed),
            }
        )

    write_csv(
        metrics_dir / "fragmentation_scores.csv",
        score_rows,
        ["variant", "fragmentation_score"],
    )
    write_csv(
        metrics_dir / "query_results.csv",
        combined_query_rows,
        ["variant", *EXPECTED_COLUMNS],
    )
    write_csv(
        metrics_dir / "missed_students_vs_baseline.csv",
        missed_rows,
        [
            "variant",
            "baseline_identified_count",
            "variant_identified_count",
            "missed_count",
            "missed_student_ids",
        ],
    )
    write_results_tables(
        table_rows,
        reports_dir / "results_table.md",
        reports_dir / "results_table.tex",
    )

    return {
        "fragmentation_scores": metrics_dir / "fragmentation_scores.csv",
        "query_results": metrics_dir / "query_results.csv",
        "missed_students_vs_baseline": metrics_dir / "missed_students_vs_baseline.csv",
        "markdown_table": reports_dir / "results_table.md",
        "latex_table": reports_dir / "results_table.tex",
    }


def main() -> None:
    args = parse_args()
    outputs = analyze_run(args.run_dir)
    for label, path in outputs.items():
        print(f"{label}: {path}")


if __name__ == "__main__":
    main()
