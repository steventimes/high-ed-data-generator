"""
evaluate.py
-----------

Run declarative workload queries and compute fragmentation scores using
the Query Receipt Layer (QRL) plus baseline-vs-target comparison.
"""

import argparse
from typing import Optional

from tabulate import tabulate

from fragmentation_scoring import FragmentationScorer, QueryScoreResult
from query_receipt_layer import QueryReceiptLayer
from workload_spec import default_workload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute workload queries and compute fragmentation scores"
    )
    parser.add_argument(
        "--db",
        type=str,
        required=True,
        help="Path to the fragmented/target DuckDB database file",
    )
    parser.add_argument(
        "--baseline-db",
        type=str,
        default=None,
        help="Path to the clean baseline DuckDB database file",
    )
    parser.add_argument(
        "--frag-level",
        type=str,
        default=None,
        help="Optional label for the fragmentation level (e.g. low, med, high)",
    )
    parser.add_argument(
        "--no-result",
        action="store_true",
        help="Do not return query results (faster when only receipts are needed)",
    )
    return parser.parse_args()


def _fmt_metric(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def _summary_row(result: QueryScoreResult):
    metrics = result.metrics
    return [
        result.query_name,
        result.frag_level or "-",
        result.source_fanout,
        f"{result.runtime_ms:.2f}",
        result.row_count,
        _fmt_metric(metrics.get("JML")),
        _fmt_metric(metrics.get("CCL")),
        _fmt_metric(metrics.get("MNS")),
        _fmt_metric(metrics.get("RTL")),
        _fmt_metric(metrics.get("SBL")),
        _fmt_metric(metrics.get("STL")),
        _fmt_metric(metrics.get("accuracy_score")),
        _fmt_metric(metrics.get("efficiency_score")),
        _fmt_metric(metrics.get("fragmentation_score")),
    ]


def _missing_default_workload_tables(qrl: QueryReceiptLayer):
    required = {
        "sis_enrollments",
        "identity_crosswalk_integration",
        "financial_aid_wide",
        "lms_activity_wide",
    }
    missing = []
    for table in sorted(required):
        row = qrl.con.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_name = ?
            LIMIT 1
            """,
            (table,),
        ).fetchone()
        if row is None:
            missing.append(table)
    return missing


def main() -> None:
    args = parse_args()
    if not args.baseline_db:
        raise SystemExit("Missing required argument: --baseline-db")

    target_qrl = QueryReceiptLayer(args.db)
    baseline_qrl = QueryReceiptLayer(args.baseline_db)
    scorer = FragmentationScorer(target_qrl=target_qrl, baseline_qrl=baseline_qrl)
    target_missing_tables = _missing_default_workload_tables(target_qrl)
    if target_missing_tables:
        target_qrl.close()
        baseline_qrl.close()
        raise SystemExit(
            "Missing required tables for target workload: "
            + ", ".join(target_missing_tables)
            + ". Expected schema-aligned outputs including wide bridge tables."
        )
    baseline_missing_tables = _missing_default_workload_tables(baseline_qrl)
    if baseline_missing_tables:
        target_qrl.close()
        baseline_qrl.close()
        raise SystemExit(
            "Missing required tables for baseline workload: "
            + ", ".join(baseline_missing_tables)
            + ". Expected schema-aligned outputs including wide bridge tables."
        )
    query_specs = default_workload()

    summary_rows = []
    results = []
    for spec in query_specs:
        print(f"Executing {spec.name}...")
        try:
            result = scorer.score_query(
                query_spec=spec,
                frag_level=args.frag_level,
                return_result=not args.no_result,
            )
            results.append(result)
        except Exception as exc:
            print(f"  Query {spec.name} failed: {exc}")
            continue

        summary_rows.append(_summary_row(result))
        if result.result_df is not None:
            print(result.result_df.head())

    print("\nSummary of Query Metrics:")
    print(
        tabulate(
            summary_rows,
            headers=[
                "Query",
                "FragLevel",
                "Source Fanout",
                "Runtime(ms)",
                "Rows",
                "JML",
                "CCL",
                "MNS",
                "RTL",
                "SBL",
                "STL",
                "Accuracy",
                "Efficiency",
                "Fragmentation",
            ],
            tablefmt="github",
        )
    )

    workload_score = FragmentationScorer.workload_score(results, query_specs)
    print(f"\nWorkload Fragmentation Score: {_fmt_metric(workload_score)}")

    target_qrl.close()
    baseline_qrl.close()


if __name__ == "__main__":
    main()
