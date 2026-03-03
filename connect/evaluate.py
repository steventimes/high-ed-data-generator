"""
evaluate.py
-----------

Example script for running a small workload of SQL queries against a
DuckDB database populated with the synthetic high‑ed data.  It uses
the `QueryReceiptLayer` to execute each query, capturing runtime and
plan metadata, and prints a summary table of the collected receipts.

You can customise the workload by editing the `QUERIES` list below
or by adding new items.  Each entry must have a unique name and
contain a valid SQL statement that references tables loaded by
`load_data.py`.

Usage:

    python evaluate.py --db /path/to/edu.duckdb

The script will log receipts into the `receipts` table of the
database and emit a summary of metrics to stdout.  To inspect the
receipts afterwards, open the database using DuckDB CLI or any
compatible tool.
"""

import argparse
from typing import List, Tuple

from tabulate import tabulate

from query_receipt_layer import QueryReceiptLayer


# Define workload queries.  Each tuple contains (query_name, sql).
# You can modify or extend this list to suit your own analysis.  The
# default workload covers a few basic joins across the generated
# tables.  Note that DuckDB will infer null values for missing fields.
QUERIES: List[Tuple[str, str]] = [
    (
        "q1_enrollment_status",
        """
        SELECT term_code, enrollment_status, COUNT(*) AS cnt
        FROM sis_enrollments
        GROUP BY term_code, enrollment_status
        ORDER BY term_code, enrollment_status
        """,
    ),
    (
        "q2_login_average",
        """
        SELECT s.student_id, AVG(l.login_count) AS avg_logins
        FROM identity_crosswalk AS cw
        JOIN lms_activity AS l ON cw.moodle_user_key = l.moodle_user_key
        JOIN sis_enrollments AS s ON cw.student_id = s.student_id AND s.term_code = l.term_code
        GROUP BY s.student_id
        LIMIT 10
        """,
    ),
    (
        "q3_loans_gpa",
        """
        SELECT s.term_code, AVG(f.loans) AS avg_loans, AVG(s.term_gpa) AS avg_term_gpa
        FROM financial_aid AS f
        JOIN identity_crosswalk AS cw ON f.workday_person_id = cw.workday_person_id
        JOIN sis_enrollments AS s ON cw.student_id = s.student_id AND f.term_code = s.term_code
        GROUP BY s.term_code
        ORDER BY s.term_code
        """,
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute workload queries and record receipts")
    parser.add_argument(
        "--db",
        type=str,
        required=True,
        help="Path to the DuckDB database file",
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


def main() -> None:
    args = parse_args()
    qrl = QueryReceiptLayer(args.db)
    summary_rows = []
    for name, sql in QUERIES:
        print(f"Executing {name}...")
        result_df = None
        try:
            result_df = qrl.execute(
                query_name=name,
                sql=sql,
                frag_level=args.frag_level,
                return_result=not args.no_result,
            )
        except Exception as exc:
            print(f"  Query {name} failed: {exc}")
            continue
        # Fetch the latest receipt for this query
        receipt_row = qrl.con.execute(
            f"SELECT source_fanout, runtime_ms, row_count, receipt FROM receipts WHERE query_name=? ORDER BY timestamp DESC LIMIT 1",
            (name,),
        ).fetchone()
        if receipt_row:
            source_fanout, runtime_ms, row_count, receipt_json = receipt_row
            summary_rows.append(
                [
                    name,
                    args.frag_level or "-",
                    source_fanout,
                    f"{runtime_ms:.2f} ms",
                    row_count,
                ]
            )
        # Optionally print result preview
        if result_df is not None:
            print(result_df.head())
    # Print summary table
    print("\nSummary of Query Metrics:")
    print(
        tabulate(
            summary_rows,
            headers=["Query", "FragLevel", "Source Fan‑out", "Runtime", "Rows"],
            tablefmt="github",
        )
    )
    qrl.close()


if __name__ == "__main__":
    main()