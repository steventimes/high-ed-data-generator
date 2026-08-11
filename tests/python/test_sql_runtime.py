from __future__ import annotations

import csv
from pathlib import Path

import pytest
from benchmark.sql_runtime import (
    VariantSqlRuntime,
    extract_sql,
    validate_read_only_sql,
)


def test_sql_validation_allows_ctes_over_benchmark_tables() -> None:
    sql = """
    WITH at_risk AS (
        SELECT student_id FROM academic_records WHERE gpa < 2.5
    )
    SELECT a.student_id
    FROM at_risk AS a
    LEFT JOIN financial_aid_records AS f USING (student_id)
    """
    assert validate_read_only_sql(sql).startswith("WITH at_risk")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2;",
        "DELETE FROM academic_records;",
        "SELECT * FROM read_csv_auto('/etc/passwd');",
        "SELECT * FROM information_schema.tables;",
        "SELECT * FROM unrelated_table;",
    ],
)
def test_sql_validation_rejects_statements_outside_the_benchmark_sandbox(
    sql: str,
) -> None:
    with pytest.raises(ValueError):
        validate_read_only_sql(sql)


def test_extract_sql_strips_fence_then_applies_the_same_sandbox() -> None:
    assert (
        extract_sql("```sql\nSELECT student_id FROM academic_records;\n```")
        == "SELECT student_id FROM academic_records;"
    )

    with pytest.raises(ValueError):
        extract_sql("```sql\nSELECT * FROM read_json('/tmp/private.json');\n```")


def test_sql_validation_rejects_volatile_queries() -> None:
    with pytest.raises(ValueError, match="deterministic"):
        validate_read_only_sql(
            "SELECT student_id FROM academic_records WHERE random() > 0.5;"
        )


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT a.student_id FROM academic_records AS a "
            "CROSS JOIN financial_aid_records AS f"
        ),
        (
            "WITH RECURSIVE walk(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM walk"
            ") SELECT value FROM walk"
        ),
    ],
)
def test_sql_validation_rejects_unbounded_query_shapes(sql: str) -> None:
    with pytest.raises(ValueError, match="resource"):
        validate_read_only_sql(sql)


def test_variant_runtime_reuses_a_typed_dataset_until_closed(tmp_path: Path) -> None:
    with (tmp_path / "academic_records.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["student_id", "gpa", "enrollment_status", "semester"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "student_id": "S0001",
                "gpa": "",
                "enrollment_status": "full_time",
                "semester": "Fall 2024",
            }
        )
    with (tmp_path / "financial_aid_records.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "student_id",
                "aid_amount",
                "aid_status",
                "disbursement_date",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "student_id": "S0001",
                "aid_amount": "",
                "aid_status": "",
                "disbursement_date": "",
            }
        )

    with VariantSqlRuntime(tmp_path) as runtime:
        assert runtime.execute(
            "SELECT student_id FROM academic_records",
            required_columns=("student_id",),
        ) == [{"student_id": "S0001"}]
        types = runtime.execute(
            "SELECT typeof(gpa) AS gpa_type, "
            "(SELECT typeof(aid_amount) FROM financial_aid_records LIMIT 1) "
            "AS aid_type, "
            "(SELECT typeof(disbursement_date) FROM financial_aid_records LIMIT 1) "
            "AS date_type FROM academic_records"
        )
        assert types == [
            {"gpa_type": "DOUBLE", "aid_type": "DOUBLE", "date_type": "DATE"}
        ]

    with pytest.raises(RuntimeError, match="closed"):
        runtime.execute("SELECT student_id FROM academic_records")


def test_identity_crosswalk_restores_department_local_student_ids(
    tmp_path: Path,
) -> None:
    for filename, fieldnames, rows in [
        (
            "academic_records.csv",
            ["student_id", "gpa", "enrollment_status", "semester"],
            [
                {
                    "student_id": "S0001",
                    "gpa": "2.10",
                    "enrollment_status": "full_time",
                    "semester": "Fall 2024",
                }
            ],
        ),
        (
            "financial_aid_records.csv",
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [
                {
                    "student_id": "financial-aid::S0001",
                    "aid_amount": "1200.00",
                    "aid_status": "suspended",
                    "disbursement_date": "2024-09-01",
                }
            ],
        ),
        (
            "identity_crosswalk.csv",
            ["canonical_student_id", "financial_aid_student_id"],
            [
                {
                    "canonical_student_id": "S0001",
                    "financial_aid_student_id": "financial-aid::S0001",
                }
            ],
        ),
    ]:
        with (tmp_path / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    with VariantSqlRuntime(tmp_path) as runtime:
        direct = runtime.execute(
            "SELECT a.student_id FROM academic_records AS a "
            "JOIN financial_aid_records AS f ON a.student_id = f.student_id"
        )
        resolved = runtime.execute(
            "SELECT a.student_id FROM academic_records AS a "
            "JOIN identity_crosswalk AS x "
            "ON a.student_id = x.canonical_student_id "
            "JOIN financial_aid_records AS f "
            "ON x.financial_aid_student_id = f.student_id"
        )

    assert direct == []
    assert resolved == [{"student_id": "S0001"}]
