from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest
from benchmark.evaluation.metrics import (
    compare_entity_sets,
    compute_fragmentation_score,
    missed_students_vs_baseline,
)
from benchmark.evaluation.pipeline import assign_missing_causes
from benchmark.questions import QuestionSpec, load_questions, resolve_questions
from benchmark.sql_runtime import (
    extract_sql,
    infer_missing_initial_cte_name,
    normalize_generated_sql,
    run_sql_on_variant,
)
from benchmark.text_to_sql.openai_client import load_dotenv
from benchmark.text_to_sql.prompts import build_schema_context, build_system_prompt
from benchmark.text_to_sql.runner import (
    TextToSqlTarget,
    validate_generated_sql_against_targets,
)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_toy_variant(path: Path) -> None:
    write_csv(
        path / "academic_records.csv",
        ["student_id", "gpa", "enrollment_status", "semester"],
        [
            {
                "student_id": "S0001",
                "gpa": "2.40",
                "enrollment_status": "full_time",
                "semester": "Fall 2024",
            },
            {
                "student_id": "S0002",
                "gpa": "2.10",
                "enrollment_status": "part_time",
                "semester": "Fall 2024",
            },
            {
                "student_id": "S0003",
                "gpa": "3.20",
                "enrollment_status": "full_time",
                "semester": "Fall 2024",
            },
        ],
    )
    write_csv(
        path / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        [
            {
                "student_id": "S0001",
                "aid_amount": "1000.00",
                "aid_status": "suspended",
                "disbursement_date": "2024-09-01",
            },
            {
                "student_id": "S0003",
                "aid_amount": "0.00",
                "aid_status": "none",
                "disbursement_date": "2024-09-02",
            },
        ],
    )


def test_query_runtime_preserves_missing_rows_without_classifying_them(
    tmp_path: Path,
) -> None:
    variant = tmp_path / "variant"
    write_toy_variant(variant)
    rows = run_sql_on_variant(
        variant,
        """
        SELECT a.student_id
        FROM academic_records AS a
        LEFT JOIN financial_aid_records AS f USING (student_id)
        WHERE a.gpa < 2.5
          AND f.aid_status IS NOT NULL
          AND f.aid_status <> 'active';
        """,
    )
    assert [row["student_id"] for row in rows] == ["S0001"]


def test_fragmentation_score_uses_all_academic_students(tmp_path: Path) -> None:
    variant = tmp_path / "variant"
    write_toy_variant(variant)
    score = compute_fragmentation_score(
        variant / "academic_records.csv",
        variant / "financial_aid_records.csv",
    )
    assert abs(score - (2.0 / 3.0)) < 1e-9


def test_missed_students_vs_baseline_is_set_difference() -> None:
    baseline = [{"student_id": "S0001"}, {"student_id": "S0002"}]
    variant = [{"student_id": "S0002"}, {"student_id": "S0003"}]
    assert missed_students_vs_baseline(baseline, variant) == ["S0001"]


def test_question_json_loader_accepts_query_registry_metadata(tmp_path: Path) -> None:
    questions_file = tmp_path / "question.json"
    questions_file.write_text(
        """
        {
          "queries": [
            {
              "question_id": "target",
              "question": "Which students are at risk?",
              "institution_role": "retention_advisor",
              "decision_type": "flag_for_outreach",
              "reference_sql": "SELECT student_id FROM academic_records;",
              "weighting_policy": {
                "type": "gpa_band",
                "default_weight": 1.0,
                "bands": [{"max_gpa": 2.0, "weight": 3.0}]
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    questions = load_questions(questions_file)
    assert questions[0].question_id == "target"
    assert questions[0].institution_role == "retention_advisor"
    assert questions[0].reference_sql == "SELECT student_id FROM academic_records;"
    assert questions[0].weighting_policy is not None
    assert questions[0].weighting_policy.bands[0].weight == 3.0


def test_natural_language_questions_can_be_enriched_from_separate_registry(
    tmp_path: Path,
) -> None:
    registry_file = tmp_path / "query_registry.json"
    registry_file.write_text(
        """
        {
          "queries": [
            {
              "question_id": "target",
              "question": "Which students are at risk?",
              "reference_sql": "SELECT student_id FROM academic_records;",
              "institution_role": "retention_advisor"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    enriched = resolve_questions(
        registry_path=registry_file,
        inline_questions=["Which students are at risk?"],
    )
    assert enriched[0].question_id == "target"
    assert enriched[0].reference_sql == "SELECT student_id FROM academic_records;"
    assert enriched[0].institution_role == "retention_advisor"


def test_extract_sql_removes_markdown_fence() -> None:
    sql = extract_sql("```sql\nSELECT student_id FROM academic_records;\n```")
    assert sql == "SELECT student_id FROM academic_records;"


def test_extract_sql_preserves_with_cte() -> None:
    sql = extract_sql(
        "WITH flagged AS (SELECT student_id FROM academic_records) SELECT student_id FROM flagged;"
    )
    assert sql.startswith("WITH flagged AS (")


def test_dotenv_loader_reads_openai_key_without_extra_dependency(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local secrets\nOPENAI_API_KEY='test-key'\nOPENAI_MODEL=gpt-test\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    load_dotenv(env_file)
    assert "OPENAI_API_KEY" in os.environ
    assert os.environ["OPENAI_MODEL"] == "gpt-test"


def test_system_prompt_contains_benchmark_semantics() -> None:
    prompt = build_system_prompt(
        schema_context=build_schema_context(),
        question=QuestionSpec(
            question_id="baseline",
            question="Which students are at risk?",
            institution_role="financial_aid_admin",
            decision_type="manual_review",
            reference_sql=(
                "SELECT a.student_id FROM academic_records AS a "
                "JOIN identity_crosswalk AS x "
                "ON a.student_id = x.canonical_student_id"
            ),
        ),
    )
    assert "GPA below 2.5" in prompt
    assert "active, suspended, none" in prompt
    assert "Use a LEFT JOIN" in prompt
    assert "identity_crosswalk" in prompt
    assert "canonical_student_id" in prompt
    assert "resolve department-local identifiers" in prompt
    assert "Institution role: financial_aid_admin" in prompt


def test_missing_initial_cte_is_inferred_and_prefixed() -> None:
    broken_sql = """SELECT DISTINCT student_id
FROM academic_records
WHERE gpa < 2.5
), fa_agg AS (
  SELECT student_id, COUNT(*) AS total_rows
  FROM financial_aid_records
  GROUP BY student_id
)
SELECT ar.student_id
FROM at_risk ar
LEFT JOIN fa_agg f ON f.student_id = ar.student_id;"""
    assert infer_missing_initial_cte_name(broken_sql) == "at_risk"
    repaired = normalize_generated_sql(broken_sql)
    assert repaired.startswith("WITH at_risk AS (")


def test_compare_entity_sets_computes_weighted_miss_loss() -> None:
    metrics, missing_ids, extra_ids = compare_entity_sets(
        baseline_rows=[{"student_id": "S0001"}, {"student_id": "S0002"}],
        observed_rows=[{"student_id": "S0002"}],
        entity_key="student_id",
        weight_lookup={"S0001": 3.0, "S0002": 1.0},
    )
    assert missing_ids == ["S0001"]
    assert extra_ids == []
    assert metrics.miss_rate == 0.5
    assert metrics.weighted_miss_loss == 0.75


def test_entity_comparison_rejects_rows_without_the_configured_key() -> None:
    with pytest.raises(ValueError, match="student_id"):
        compare_entity_sets(
            baseline_rows=[{"gpa": 2.1}],
            observed_rows=[],
            entity_key="student_id",
        )


def test_assign_missing_causes_uses_manifest_row_ids() -> None:
    causes = assign_missing_causes(
        ["S0001", "S0002", "S0003", "S0004"],
        {
            "selected_row_ids": {
                "drop_row": ["S0001"],
                "null_aid_status": ["S0002"],
                "identifier_mismatch": ["S0004"],
            }
        },
    )
    assert causes == {
        "S0001": "missing_record",
        "S0002": "null_critical_field",
        "S0003": "unknown",
        "S0004": "identity_mismatch",
    }


def test_generated_sql_validation_rejects_null_join_as_positive_match(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    fragmented = tmp_path / "low_fragmentation"
    write_csv(
        baseline / "academic_records.csv",
        ["student_id", "gpa", "enrollment_status", "semester"],
        [
            {
                "student_id": "S0001",
                "gpa": "2.10",
                "enrollment_status": "full_time",
                "semester": "Fall 2024",
            },
            {
                "student_id": "S0002",
                "gpa": "2.20",
                "enrollment_status": "part_time",
                "semester": "Fall 2024",
            },
        ],
    )
    write_csv(
        fragmented / "academic_records.csv",
        ["student_id", "gpa", "enrollment_status", "semester"],
        [
            {
                "student_id": "S0001",
                "gpa": "2.10",
                "enrollment_status": "full_time",
                "semester": "Fall 2024",
            },
            {
                "student_id": "S0002",
                "gpa": "2.20",
                "enrollment_status": "part_time",
                "semester": "Fall 2024",
            },
        ],
    )
    write_csv(
        baseline / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        [
            {
                "student_id": "S0001",
                "aid_amount": "1000.00",
                "aid_status": "suspended",
                "disbursement_date": "2024-09-01",
            },
            {
                "student_id": "S0002",
                "aid_amount": "0.00",
                "aid_status": "none",
                "disbursement_date": "2024-09-02",
            },
        ],
    )
    write_csv(
        fragmented / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        [
            {
                "student_id": "S0001",
                "aid_amount": "1000.00",
                "aid_status": "suspended",
                "disbursement_date": "2024-09-01",
            },
        ],
    )

    bad_sql = """
SELECT DISTINCT a.student_id
FROM academic_records a
LEFT JOIN financial_aid_records f ON a.student_id = f.student_id
WHERE a.gpa < 2.5
  AND (f.aid_status <> 'active' OR f.aid_status IS NULL)
"""
    with pytest.raises(ValueError):
        validate_generated_sql_against_targets(
            question=QuestionSpec(
                question_id="at_risk_financial_disruption",
                question="Which students are academically at risk and have a disruption in their financial aid?",
            ),
            generated_sql=bad_sql,
            targets=[
                TextToSqlTarget(label="baseline", variant_dir=baseline),
                TextToSqlTarget(label="low_fragmentation", variant_dir=fragmented),
            ],
            reference_ids_by_target={
                "baseline": {"S0001", "S0002"},
                "low_fragmentation": {"S0001"},
            },
        )
