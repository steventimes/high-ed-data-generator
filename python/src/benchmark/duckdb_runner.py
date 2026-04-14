from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import duckdb

CANONICAL_SQL = """
WITH joined_records AS (
  SELECT a.student_id,
         a.gpa,
         a.enrollment_status,
         a.semester,
         f.student_id AS aid_student_id,
         f.aid_amount,
         f.aid_status,
         f.disbursement_date
  FROM academic_records a
  LEFT JOIN financial_aid_records f
    ON a.student_id = f.student_id
)
SELECT student_id,
       gpa,
       enrollment_status,
       semester,
       aid_amount,
       aid_status,
       disbursement_date
FROM joined_records
WHERE gpa < 2.5
  AND aid_student_id IS NOT NULL
  AND aid_amount IS NOT NULL
  AND aid_status IS NOT NULL
  AND aid_status <> 'active';
""".strip()

EXPECTED_COLUMNS = [
    "student_id",
    "gpa",
    "enrollment_status",
    "semester",
    "aid_amount",
    "aid_status",
    "disbursement_date",
]


def canonical_sql() -> str:
    if "INNER JOIN" in CANONICAL_SQL.upper():
        raise ValueError("Canonical query must not contain INNER JOIN")
    return CANONICAL_SQL


def run_canonical_query(variant_dir: Path | str, output_csv: Path | str | None = None) -> list[dict[str, Any]]:
    return run_sql_on_variant(variant_dir, canonical_sql(), output_csv=output_csv)


def run_sql_on_variant(
    variant_dir: Path | str,
    sql: str,
    output_csv: Path | str | None = None,
) -> list[dict[str, Any]]:
    normalized = sql.strip().rstrip(";")
    lowered = normalized.lower()
    if not (lowered.startswith("select") or lowered.startswith("with ")):
        raise ValueError("Only SELECT statements or CTE SELECT statements are allowed in benchmark query execution")

    variant_path = Path(variant_dir)
    academic_csv = variant_path / "academic_records.csv"
    aid_csv = variant_path / "financial_aid_records.csv"
    if not academic_csv.exists():
        raise FileNotFoundError(f"Missing academic CSV: {academic_csv}")
    if not aid_csv.exists():
        raise FileNotFoundError(f"Missing financial aid CSV: {aid_csv}")

    with duckdb.connect(database=":memory:") as connection:
        register_csv_views(connection, academic_csv, aid_csv)
        result = connection.execute(normalized).fetchall()
        columns = [description[0] for description in connection.description]

    rows = [dict(zip(columns, row)) for row in result]
    if output_csv is not None:
        write_rows(Path(output_csv), rows, columns)
    return rows


def register_csv_views(connection: duckdb.DuckDBPyConnection, academic_csv: Path, aid_csv: Path) -> None:
    academic_path = sql_literal(academic_csv)
    aid_path = sql_literal(aid_csv)
    connection.execute(
        f"""
        CREATE VIEW academic_records AS
        SELECT *
        FROM read_csv_auto('{academic_path}', header = true, nullstr = '', all_varchar = false);
        """
    )
    connection.execute(
        f"""
        CREATE VIEW financial_aid_records AS
        SELECT *
        FROM read_csv_auto('{aid_path}', header = true, nullstr = '', all_varchar = false);
        """
    )


def write_rows(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = columns or (list(rows[0].keys()) if rows else EXPECTED_COLUMNS)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: format_value(row.get(key)) for key in fieldnames})


def sql_literal(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def format_value(value: Any) -> str:
    return "" if value is None else str(value)
