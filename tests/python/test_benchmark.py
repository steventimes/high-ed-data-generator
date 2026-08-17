from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from benchmark import generated_batch
from benchmark.evaluation import pipeline as evaluation_pipeline
from benchmark.evaluation.metrics import (
    VARIANT_NAMES,
    compare_entity_sets,
    compute_fragmentation_score,
    missed_students_vs_baseline,
)
from benchmark.evaluation.pipeline import (
    assign_extra_causes,
    assign_missing_causes,
    evaluate_text_to_sql_outputs,
)
from benchmark.generated_batch import (
    TargetBatchIdentity,
    validate_generated_batch_contract,
    write_generated_batch_contract,
)
from benchmark.questions import (
    DEFAULT_REGISTRY,
    QuestionSpec,
    TemporalEvaluation,
    WeightBand,
    WeightingPolicy,
    load_questions,
    resolve_questions,
    validate_questions,
)
from benchmark.sql_runtime import (
    extract_sql,
    infer_missing_initial_cte_name,
    normalize_generated_sql,
    run_sql_on_variant,
)
from benchmark.text_to_sql import runner
from benchmark.text_to_sql.openai_client import OpenAiSqlGenerator, load_dotenv
from benchmark.text_to_sql.prompts import build_schema_context, build_system_prompt
from benchmark.text_to_sql.runner import (
    TextToSqlTarget,
    run_text_to_sql_experiment,
    validate_generated_sql_against_targets,
)

# 相关回归按职责集中；来源标记用于快速定位历史测试语义。

# --- 基准指标与查询行为 ---


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
    assert "financial_aid_publication_events" in prompt
    assert "benchmark_temporal_snapshots" in prompt
    assert "minimum event_time_watermark" in prompt
    assert "published_at cutoff" in prompt
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


def test_fragmentation_score_treats_department_local_status_as_semantic_loss(
    tmp_path: Path,
) -> None:
    academic = tmp_path / "academic.csv"
    aid = tmp_path / "aid.csv"
    write_csv(
        academic,
        ["student_id", "gpa"],
        [{"student_id": "S0001", "gpa": "2.0"}],
    )
    write_csv(
        aid,
        ["student_id", "aid_amount", "aid_status"],
        [
            {
                "student_id": "S0001",
                "aid_amount": "100.00",
                "aid_status": "financial-aid::active",
            }
        ],
    )

    assert compute_fragmentation_score(academic, aid) == pytest.approx(2 / 3)


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


def test_missing_cause_attribution_skips_governed_remediations() -> None:
    manifest = {
        "selected_row_ids": {
            "drop_row": ["S0001"],
            "null_aid_status": ["S0002"],
            "identifier_mismatch": ["S0003"],
            "publication_delay": ["S0003", "S0004"],
            "aid_status_code_drift": ["S0004", "S0005"],
        }
    }
    causes = assign_missing_causes(
        ["S0001", "S0002", "S0003", "S0004", "S0005"],
        manifest,
        remediated_causes=frozenset(
            {"publication_delay", "identity_mismatch", "semantic_drift"}
        ),
    )

    assert causes == {
        "S0001": "missing_record",
        "S0002": "null_critical_field",
        "S0003": "unknown",
        "S0004": "unknown",
        "S0005": "unknown",
    }


def test_sql_substrings_do_not_imply_cause_remediation() -> None:
    manifest = {
        "selected_row_ids": {
            "publication_delay": ["S0001"],
            "identifier_mismatch": ["S0002"],
            "aid_status_code_drift": ["S0003"],
        }
    }
    # 这些名称即使出现在无关 CTE 中，也不能替代 QuestionSpec 的显式元数据。
    unrelated_sql = (
        "WITH mentioned AS (SELECT * FROM financial_aid_late_arrivals) "
        "SELECT * FROM mentioned"
    )
    causes = assign_missing_causes(
        ["S0001", "S0002", "S0003"], manifest, remediated_causes=frozenset()
    )
    assert unrelated_sql
    assert causes == {
        "S0001": "publication_delay",
        "S0002": "identity_mismatch",
        "S0003": "semantic_drift",
    }


def test_extra_cause_attribution_marks_raw_status_drift() -> None:
    causes = assign_extra_causes(
        ["S0001", "S0002"],
        {"selected_row_ids": {"aid_status_code_drift": ["s0001"]}},
    )

    assert causes == {"S0001": "semantic_drift", "S0002": "unknown"}


def test_extra_cause_attribution_honors_explicit_governed_remediation() -> None:
    manifest = {
        "selected_row_ids": {
            "aid_status_code_drift": ["S0001"],
        }
    }

    causes = assign_extra_causes(
        ["S0001"],
        manifest,
        remediated_causes=frozenset({"semantic_drift"}),
    )

    assert causes == {"S0001": "unknown"}


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


# --- 问题注册表与元数据契约 ---


def write_registry(path: Path, query: dict[str, object]) -> None:
    path.write_text(json.dumps({"queries": [query]}), encoding="utf-8")


def test_registry_is_the_single_source_for_default_questions(tmp_path: Path) -> None:
    registry = tmp_path / "queries.json"
    write_registry(
        registry,
        {
            "question_id": "target",
            "question": "Which students are at risk?",
            "reference_sql": "SELECT student_id FROM academic_records;",
            "institution_role": "retention_advisor",
        },
    )

    questions = load_questions(registry)

    assert [question.question_id for question in questions] == ["target"]
    assert questions[0].reference_sql == "SELECT student_id FROM academic_records;"


def test_inline_question_reuses_matching_registry_metadata(tmp_path: Path) -> None:
    registry = tmp_path / "queries.json"
    write_registry(
        registry,
        {
            "question_id": "target",
            "question": "Which students are at risk?",
            "reference_sql": "SELECT student_id FROM academic_records;",
            "decision_type": "manual_review",
        },
    )

    questions = resolve_questions(
        registry_path=registry,
        inline_questions=["  which students are AT risk?  "],
    )

    assert questions[0].question_id == "target"
    assert questions[0].decision_type == "manual_review"


def test_inline_question_copies_explicit_temporal_evaluation(tmp_path: Path) -> None:
    registry = tmp_path / "queries.json"
    current_sql = "SELECT student_id FROM financial_aid_records;"
    replay_sql = (
        "SELECT student_id FROM financial_aid_records "
        "UNION SELECT student_id FROM financial_aid_late_arrivals;"
    )
    write_registry(
        registry,
        {
            "question_id": "target",
            "question": "Which students require action after replay?",
            "reference_sql": replay_sql,
            "temporal_evaluation": {
                "current_reference_sql": current_sql,
                "replay_reference_sql": replay_sql,
                "snapshot": "replayed",
            },
            "remediated_causes": [
                "publication_delay",
                "identity_mismatch",
                "semantic_drift",
            ],
        },
    )

    registered = load_questions(registry)[0]
    resolved = resolve_questions(
        registry_path=registry,
        inline_questions=["  which students require ACTION after replay?  "],
    )[0]

    expected = TemporalEvaluation(
        current_reference_sql=current_sql,
        replay_reference_sql=replay_sql,
        snapshot="replayed",
    )
    assert registered.temporal_evaluation == expected
    assert resolved.temporal_evaluation == expected
    assert registered.remediated_causes == frozenset(
        {"publication_delay", "identity_mismatch", "semantic_drift"}
    )
    assert resolved.remediated_causes == registered.remediated_causes


def test_temporal_snapshot_must_match_question_reference_sql(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "queries.json"
    write_registry(
        registry,
        {
            "question_id": "target",
            "question": "Which students require action?",
            "reference_sql": "SELECT student_id FROM academic_records;",
            "temporal_evaluation": {
                "current_reference_sql": (
                    "SELECT student_id FROM financial_aid_records;"
                ),
                "replay_reference_sql": (
                    "SELECT student_id FROM financial_aid_late_arrivals;"
                ),
                "snapshot": "replayed",
            },
        },
    )

    with pytest.raises(ValueError, match="must match replay_reference_sql"):
        load_questions(registry)


def test_registry_rejects_unknown_remediated_causes(tmp_path: Path) -> None:
    registry = tmp_path / "queries.json"
    write_registry(
        registry,
        {
            "question_id": "target",
            "question": "Which students require action?",
            "reference_sql": "SELECT student_id FROM academic_records;",
            "remediated_causes": ["publication_delay", "typo_cause"],
        },
    )

    with pytest.raises(ValueError, match="unsupported remediated cause"):
        load_questions(registry)


def test_registry_rejects_non_positive_weights(tmp_path: Path) -> None:
    registry = tmp_path / "queries.json"
    write_registry(
        registry,
        {
            "question_id": "target",
            "question": "Which students are at risk?",
            "weighting_policy": {
                "type": "gpa_band",
                "default_weight": 0,
                "bands": [{"max_gpa": 2.5, "weight": 1}],
            },
        },
    )

    with pytest.raises(ValueError, match="default_weight"):
        load_questions(registry)


def test_registry_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "queries.json"
    registry.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "question_id": "target",
                        "question": "Which students are at risk?",
                        "reference_sqp": "SELECT student_id FROM academic_records;",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown fields"):
        load_questions(registry)


def test_registry_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    registry = tmp_path / "queries.json"
    registry.write_text(
        """{
          "queries": [{
            "question_id": "target",
            "question": "Which students require action?",
            "reference_sql": "SELECT student_id FROM academic_records;",
            "reference_sql": "SELECT student_id FROM financial_aid_records;"
          }]
        }""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate JSON key: reference_sql"):
        load_questions(registry)


def test_validate_questions_rejects_invalid_manual_temporal_metadata() -> None:
    current_sql = "SELECT student_id FROM financial_aid_records;"
    replay_sql = "SELECT student_id FROM financial_aid_late_arrivals;"
    question = QuestionSpec(
        question_id="manual",
        question="Which students require action?",
        reference_sql=current_sql,
        temporal_evaluation=TemporalEvaluation(
            current_reference_sql=current_sql,
            replay_reference_sql=replay_sql,
            snapshot="replayed",
        ),
    )

    with pytest.raises(ValueError, match="reference_sql must match"):
        validate_questions([question])


def test_validate_questions_rejects_manual_unknown_remediated_cause() -> None:
    question = QuestionSpec(
        question_id="manual",
        question="Which students require action?",
        reference_sql="SELECT student_id FROM academic_records;",
        remediated_causes=frozenset({"typo_cause"}),
    )

    with pytest.raises(ValueError, match="unsupported remediated cause"):
        validate_questions([question])


def test_registry_rejects_non_finite_weights(tmp_path: Path) -> None:
    registry = tmp_path / "queries.json"

    write_registry(
        registry,
        {
            "question_id": "target",
            "question": "Which students are at risk?",
            "weighting_policy": {"default_weight": float("nan")},
        },
    )
    with pytest.raises(ValueError, match="finite"):
        load_questions(registry)


def test_question_ids_must_not_collide_after_filename_normalization(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "queries.json"
    registry.write_text(
        json.dumps(
            {
                "queries": [
                    {"question_id": "alpha-beta", "question": "Question one?"},
                    {"question_id": "alpha beta", "question": "Question two?"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="filename"):
        load_questions(registry)


def test_inline_questions_must_not_overwrite_each_others_results(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "queries.json"
    write_registry(
        registry,
        {
            "question_id": "registered",
            "question": "Registered question?",
        },
    )

    with pytest.raises(ValueError):
        resolve_questions(
            registry_path=registry,
            inline_questions=["Alpha-beta?", "Alpha beta?"],
        )


def test_inline_question_requires_a_registered_reference_oracle(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "queries.json"
    write_registry(
        registry,
        {
            "question_id": "registered",
            "question": "Registered question?",
        },
    )

    with pytest.raises(ValueError, match="not registered"):
        resolve_questions(
            registry_path=registry,
            inline_questions=["An unknown institutional question?"],
        )


def test_default_registry_includes_an_identity_resolution_control() -> None:
    questions = load_questions(DEFAULT_REGISTRY)
    controls = [
        question
        for question in questions
        if question.question_id == "at_risk_financial_disruption_resolved"
    ]

    assert len(controls) == 1
    assert "identity_crosswalk" in (controls[0].reference_sql or "")


def test_default_registry_includes_a_fully_governed_recovery_control() -> None:
    questions = load_questions(DEFAULT_REGISTRY)
    controls = [
        question
        for question in questions
        if question.question_id == "at_risk_financial_disruption_governed"
    ]

    assert len(controls) == 1
    sql = controls[0].reference_sql or ""
    assert "financial_aid_late_arrivals" in sql
    assert "identity_crosswalk" in sql
    assert "aid_status_crosswalk" in sql
    assert controls[0].remediated_causes == frozenset(
        {"publication_delay", "identity_mismatch", "semantic_drift"}
    )
    temporal = controls[0].temporal_evaluation
    assert temporal is not None
    assert temporal.snapshot == "replayed"
    assert temporal.replay_reference_sql == controls[0].reference_sql
    assert "financial_aid_late_arrivals" not in temporal.current_reference_sql
    assert "benchmark_temporal_snapshots" in temporal.current_reference_sql
    assert "benchmark_temporal_snapshots" in temporal.replay_reference_sql
    assert "financial_aid_publication_events" in temporal.current_reference_sql
    assert "financial_aid_publication_events" in temporal.replay_reference_sql


# --- 问题批次语义哈希 ---


def _write_contract(tmp_path: Path, question: QuestionSpec) -> TargetBatchIdentity:
    result = tmp_path / "risk__baseline.csv"
    result.write_text("student_id\nS0001\n", encoding="utf-8")
    identity = TargetBatchIdentity("baseline", None, None, None)
    write_generated_batch_contract(
        tmp_path,
        question_ids=["risk"],
        question_specs=[question],
        targets=[("baseline", identity)],
        result_files=[result],
    )
    return identity


def test_batch_contract_rejects_changed_question_semantics(tmp_path: Path) -> None:
    original = QuestionSpec(
        question_id="risk",
        question="Which students are at risk?",
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 2.5;",
        weighting_policy=WeightingPolicy(
            policy_type="gpa_band",
            bands=(WeightBand(max_gpa=2.0, weight=2.0),),
        ),
    )
    identity = _write_contract(tmp_path, original)

    changed = QuestionSpec(
        question_id="risk",
        question=original.question,
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 3.0;",
        weighting_policy=original.weighting_policy,
    )
    with pytest.raises(ValueError, match="question semantics"):
        validate_generated_batch_contract(
            tmp_path,
            question_ids=["risk"],
            question_specs=[changed],
            targets=[("baseline", identity)],
            strict=True,
        )


def test_question_hash_normalizes_inline_text_like_registry_matching(
    tmp_path: Path,
) -> None:
    registry = QuestionSpec(question_id="risk", question="Who is at risk?")
    identity = _write_contract(tmp_path, registry)
    inline = QuestionSpec(question_id="risk", question="  WHO   IS AT RISK? ")

    validate_generated_batch_contract(
        tmp_path,
        question_ids=["risk"],
        question_specs=[inline],
        targets=[("baseline", identity)],
        strict=True,
    )


# --- 目标清单语义绑定 ---


def test_batch_contract_rejects_changed_manifest_semantics(tmp_path: Path) -> None:
    result = tmp_path / "risk__baseline.csv"
    result.write_text("student_id\nS0001\n", encoding="utf-8")
    question = QuestionSpec(question_id="risk", question="Who is at risk?")
    original = TargetBatchIdentity(
        variant="baseline",
        cohort_fingerprint=None,
        variant_file_hashes=(("academic_records.csv", "a" * 64),),
        manifest_digest="b" * 64,
    )
    write_generated_batch_contract(
        tmp_path,
        question_ids=["risk"],
        question_specs=[question],
        targets=[("baseline", original)],
        result_files=[result],
    )
    changed = TargetBatchIdentity(
        variant=original.variant,
        cohort_fingerprint=original.cohort_fingerprint,
        variant_file_hashes=original.variant_file_hashes,
        manifest_digest="c" * 64,
    )

    with pytest.raises(ValueError, match="target variant"):
        validate_generated_batch_contract(
            tmp_path,
            question_ids=["risk"],
            question_specs=[question],
            targets=[("baseline", changed)],
            strict=True,
        )


# --- OpenAI 客户端协议边界 ---


class RecordingEndpoint:
    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def make_question() -> QuestionSpec:
    return QuestionSpec(
        question_id="model-client",
        question="Which students require review?",
    )


def make_responses_client(
    responses_endpoint: RecordingEndpoint,
    chat_endpoint: RecordingEndpoint,
) -> SimpleNamespace:
    return SimpleNamespace(
        responses=responses_endpoint,
        chat=SimpleNamespace(completions=chat_endpoint),
    )


def test_responses_success_returns_sql_without_calling_chat() -> None:
    responses = RecordingEndpoint(
        result=SimpleNamespace(
            output_text="```sql\nSELECT student_id FROM academic_records;\n```"
        )
    )
    chat = RecordingEndpoint(
        result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="SELECT 1;"))]
        )
    )
    generator = OpenAiSqlGenerator(
        "gpt-test",
        client=make_responses_client(responses, chat),
    )

    sql = generator.generate_sql(question=make_question(), schema_context="schema")

    assert sql == "SELECT student_id FROM academic_records;"
    assert len(responses.calls) == 1
    assert responses.calls[0]["model"] == "gpt-test"
    assert [item["role"] for item in responses.calls[0]["input"]] == [
        "system",
        "user",
    ]
    assert chat.calls == []


def test_responses_error_is_propagated_without_calling_chat() -> None:
    expected_error = RuntimeError("responses rate limit")
    responses = RecordingEndpoint(error=expected_error)
    chat = RecordingEndpoint(
        result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="SELECT 1;"))]
        )
    )
    generator = OpenAiSqlGenerator(
        "gpt-test",
        client=make_responses_client(responses, chat),
    )

    with pytest.raises(RuntimeError) as caught:
        generator.generate_sql(question=make_question(), schema_context="schema")

    assert caught.value is expected_error
    assert chat.calls == []


@pytest.mark.parametrize("output_text", [None, "", " \n"])
def test_empty_responses_output_fails_without_calling_chat(
    output_text: str | None,
) -> None:
    responses = RecordingEndpoint(result=SimpleNamespace(output_text=output_text))
    chat = RecordingEndpoint(
        result=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="SELECT 1;"))]
        )
    )
    generator = OpenAiSqlGenerator(
        "gpt-test",
        client=make_responses_client(responses, chat),
    )

    with pytest.raises(RuntimeError, match="Responses API returned empty output"):
        generator.generate_sql(question=make_question(), schema_context="schema")

    assert chat.calls == []


def test_client_without_responses_capability_uses_chat_completions() -> None:
    chat = RecordingEndpoint(
        result=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="SELECT student_id FROM academic_records;"
                    )
                )
            ]
        )
    )
    legacy_client = SimpleNamespace(chat=SimpleNamespace(completions=chat))
    generator = OpenAiSqlGenerator("gpt-legacy", client=legacy_client)

    sql = generator.generate_sql(question=make_question(), schema_context="schema")

    assert sql == "SELECT student_id FROM academic_records;"
    assert len(chat.calls) == 1
    assert chat.calls[0]["model"] == "gpt-legacy"
    assert [item["role"] for item in chat.calls[0]["messages"]] == [
        "system",
        "user",
    ]


# --- 评测变体选择 ---


@pytest.mark.parametrize("variants", [[], ["high_fragmentation"], ["unknown"]])
def test_evaluation_variant_selection_requires_known_baseline(
    tmp_path: Path,
    variants: list[str],
) -> None:
    with pytest.raises(ValueError, match="include baseline"):
        evaluate_text_to_sql_outputs(
            run_dir=tmp_path / "run",
            questions=[QuestionSpec(question_id="risk", question="Who is at risk?")],
            generated_results_dir=tmp_path / "generated",
            output_dir=tmp_path / "evaluation",
            plot_formats=[],
            strict=True,
            variants=variants,
        )


# --- 生成批次强契约 ---


class StaticSqlGenerator:
    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        return question.reference_sql or ""

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str:
        raise AssertionError("合法 SQL 不应进入修复分支")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _build_run(run_dir: Path) -> None:
    for variant in VARIANT_NAMES:
        variant_dir = run_dir / "variants" / variant
        _write_csv(
            variant_dir / "academic_records.csv",
            ["student_id", "gpa", "enrollment_status", "semester"],
            [
                {
                    "student_id": "S0001",
                    "gpa": "2.10",
                    "enrollment_status": "full_time",
                    "semester": "Fall 2024",
                }
            ],
        )
        _write_csv(
            variant_dir / "financial_aid_records.csv",
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [
                {
                    "student_id": "S0001",
                    "aid_amount": "1000.00",
                    "aid_status": "suspended",
                    "disbursement_date": "2024-09-01",
                }
            ],
        )


def _question(question_id: str = "at_risk") -> QuestionSpec:
    return QuestionSpec(
        question_id=question_id,
        question=f"Which students match {question_id}?",
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 2.5;",
    )


def _generate_batch(
    run_dir: Path,
    generated_dir: Path,
    *,
    questions: list[QuestionSpec] | None = None,
) -> None:
    results = run_text_to_sql_experiment(
        questions=questions or [_question()],
        targets=[
            TextToSqlTarget(
                label=variant,
                variant_dir=run_dir / "variants" / variant,
            )
            for variant in VARIANT_NAMES
        ],
        generator=StaticSqlGenerator(),
        model="static",
        max_retries=0,
        generated_results_dir=generated_dir,
    )
    assert all(result.success for result in results)


def _copy_as_empty_valid_batch(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination)
    contract = json.loads(
        (destination / "batch_contract.json").read_text(encoding="utf-8")
    )
    for result_path in destination.glob("*.csv"):
        result_path.write_text("student_id\n", encoding="utf-8")
        contract["result_files"][result_path.name] = hashlib.sha256(
            result_path.read_bytes()
        ).hexdigest()
    (destination / "batch_contract.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )


def _rewrite_result_and_refresh_digest(
    generated_dir: Path,
    *,
    filename: str,
    contents: str,
) -> None:
    """构造摘要合法但 CSV 结构恶意的批次，避免测试只命中哈希校验。"""

    result_path = generated_dir / filename
    result_path.write_text(contents, encoding="utf-8")
    contract_path = generated_dir / "batch_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["result_files"][filename] = hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest()
    contract_path.write_text(json.dumps(contract), encoding="utf-8")


def _evaluate(
    run_dir: Path,
    generated_dir: Path,
    output_dir: Path,
    *,
    questions: list[QuestionSpec] | None = None,
    strict: bool = True,
) -> dict[str, Path]:
    return evaluate_text_to_sql_outputs(
        run_dir=run_dir,
        questions=questions or [_question()],
        generated_results_dir=generated_dir,
        output_dir=output_dir,
        plot_formats=[],
        strict=strict,
    )


def test_generation_publishes_strong_contract_and_evaluation_accepts_it(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    _build_run(run_dir)

    _generate_batch(run_dir, generated_dir)

    payload = json.loads((generated_dir / "batch_contract.json").read_text())
    assert payload["contract_version"] == 1
    assert payload["question_ids"] == ["at_risk"]
    assert set(payload["targets"]) == set(VARIANT_NAMES)
    assert {
        label: target["variant"] for label, target in payload["targets"].items()
    } == {variant: variant for variant in VARIANT_NAMES}
    assert set(payload["result_files"]) == {
        f"at_risk__{variant}.csv" for variant in VARIANT_NAMES
    }
    assert all(len(digest) == 64 for digest in payload["result_files"].values())
    outputs = _evaluate(run_dir, generated_dir, tmp_path / "evaluation")
    assert all(path.is_file() for path in outputs.values())


def test_evaluation_allows_a_question_subset_from_the_same_batch(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    questions = [_question("first"), _question("second")]
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir, questions=questions)

    outputs = _evaluate(
        run_dir,
        generated_dir,
        tmp_path / "evaluation",
        questions=[questions[1]],
    )

    assert all(path.is_file() for path in outputs.values())


def test_evaluation_reads_private_snapshot_after_generated_directory_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    replacement_dir = tmp_path / "replacement"
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    _copy_as_empty_valid_batch(generated_dir, replacement_dir)
    original_write = evaluation_pipeline._write_evaluation_output_tree

    def replace_source_after_validation(**kwargs: object) -> dict[str, Path]:
        generated_dir.replace(tmp_path / "validated-batch")
        replacement_dir.replace(generated_dir)
        return original_write(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        evaluation_pipeline,
        "_write_evaluation_output_tree",
        replace_source_after_validation,
    )

    outputs = _evaluate(run_dir, generated_dir, tmp_path / "evaluation")

    with outputs["per_query_metrics"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["baseline_count"] for row in rows} == {"1"}


def test_evaluation_rejects_targets_if_the_run_is_atomically_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    replacement_run = tmp_path / "replacement-run"
    old_run = tmp_path / "old-run"
    output_dir = tmp_path / "evaluation"
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    _build_run(replacement_run)
    high_aid = (
        replacement_run
        / "variants"
        / "high_fragmentation"
        / "financial_aid_records.csv"
    )
    high_aid.write_text(
        "student_id,aid_amount,aid_status,disbursement_date\n",
        encoding="utf-8",
    )
    original_snapshot = evaluation_pipeline.snapshot_generated_batch

    @contextmanager
    def replace_run_after_generated_snapshot(
        *args: object,
        **kwargs: object,
    ):
        with original_snapshot(*args, **kwargs) as snapshot:
            run_dir.rename(old_run)
            replacement_run.rename(run_dir)
            yield snapshot

    monkeypatch.setattr(
        evaluation_pipeline,
        "snapshot_generated_batch",
        replace_run_after_generated_snapshot,
    )

    with pytest.raises(RuntimeError, match="targets changed during evaluation"):
        _evaluate(run_dir, generated_dir, output_dir)

    assert not output_dir.exists()


def test_snapshot_rejects_mid_copy_replacement_and_preserves_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    replacement_dir = tmp_path / "replacement"
    output_dir = tmp_path / "evaluation"
    output_dir.mkdir()
    sentinel = output_dir / "previous.txt"
    sentinel.write_text("old evaluation", encoding="utf-8")
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    _copy_as_empty_valid_batch(generated_dir, replacement_dir)
    original_copy = generated_batch._copy_regular_file

    def replace_source_after_contract_copy(source: Path, destination: Path) -> None:
        original_copy(source, destination)
        if source == generated_dir / "batch_contract.json":
            generated_dir.replace(tmp_path / "validated-batch")
            replacement_dir.replace(generated_dir)

    monkeypatch.setattr(
        generated_batch,
        "_copy_regular_file",
        replace_source_after_contract_copy,
    )

    with pytest.raises(ValueError, match="result hash"):
        _evaluate(run_dir, generated_dir, output_dir)

    assert sentinel.read_text(encoding="utf-8") == "old evaluation"
    assert sorted(path.name for path in output_dir.iterdir()) == [sentinel.name]
    assert not list(tmp_path.glob(".generated-batch.snapshot-*"))
    assert not list(tmp_path.glob(".evaluation.staging-*"))


def test_custom_target_mapping_is_consumable_by_evaluation(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    _build_run(run_dir)
    custom_targets = [
        TextToSqlTarget("baseline", run_dir / "variants" / "baseline"),
        TextToSqlTarget("department_a", run_dir / "variants" / "low_fragmentation"),
    ]
    results = run_text_to_sql_experiment(
        questions=[_question()],
        targets=custom_targets,
        generator=StaticSqlGenerator(),
        model="static",
        max_retries=0,
        generated_results_dir=generated_dir,
    )
    assert all(result.success for result in results)

    outputs = evaluate_text_to_sql_outputs(
        run_dir=run_dir,
        questions=[_question()],
        generated_results_dir=generated_dir,
        output_dir=tmp_path / "evaluation",
        plot_formats=[],
        strict=True,
        targets=[(target.label, target.variant_dir) for target in custom_targets],
    )

    assert all(path.is_file() for path in outputs.values())


def test_strict_evaluation_rejects_a_missing_batch_contract_before_staging(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    output_dir = tmp_path / "evaluation"
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    (generated_dir / "batch_contract.json").unlink()

    with pytest.raises(FileNotFoundError, match="batch contract"):
        _evaluate(run_dir, generated_dir, output_dir)

    assert not output_dir.exists()
    assert not list(tmp_path.glob(".evaluation.staging-*"))


def test_non_strict_evaluation_warns_for_a_legacy_batch_without_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    (generated_dir / "batch_contract.json").unlink()

    outputs = _evaluate(
        run_dir,
        generated_dir,
        tmp_path / "evaluation",
        strict=False,
    )

    warning = capsys.readouterr().out
    assert all(path.is_file() for path in outputs.values())
    assert "Warning" in warning and "batch contract" in warning


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("question", "question IDs"),
        ("missing_target", "result files"),
        ("swapped_variant", "target variant"),
        ("variant_hash", "variant file hashes"),
    ],
)
def test_evaluation_rejects_contract_from_another_batch_before_staging(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    output_dir = tmp_path / "evaluation"
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    contract_path = generated_dir / "batch_contract.json"
    payload = json.loads(contract_path.read_text())
    if mutation == "question":
        payload["question_ids"] = ["another_question"]
    elif mutation == "missing_target":
        payload["targets"].pop("high_fragmentation")
    elif mutation == "swapped_variant":
        payload["targets"]["baseline"]["variant"] = "high_fragmentation"
    else:
        payload["targets"]["baseline"]["variant_file_hashes"] = {
            "academic_records": "a" * 64
        }
    contract_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _evaluate(run_dir, generated_dir, output_dir)

    assert not output_dir.exists()
    assert not list(tmp_path.glob(".evaluation.staging-*"))


@pytest.mark.parametrize("tamper", ["replace_bytes", "extra_csv", "missing_csv"])
def test_evaluation_rejects_tampered_result_files_before_staging(
    tmp_path: Path,
    tamper: str,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    output_dir = tmp_path / "evaluation"
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    result = generated_dir / "at_risk__baseline.csv"
    if tamper == "replace_bytes":
        result.write_text("student_id\nS9999\n", encoding="utf-8")
    elif tamper == "extra_csv":
        (generated_dir / "stale.csv").write_text("student_id\n", encoding="utf-8")
    else:
        result.unlink()

    with pytest.raises(ValueError, match="result (file set|hash)"):
        _evaluate(run_dir, generated_dir, output_dir)

    assert not output_dir.exists()
    assert not list(tmp_path.glob(".evaluation.staging-*"))


def test_strict_snapshot_preserves_additional_named_result_columns(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    snapshot_parent = tmp_path / "snapshots"
    question = _question()
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    _rewrite_result_and_refresh_digest(
        generated_dir,
        filename="at_risk__baseline.csv",
        contents=("student_id,risk_score,explanation\nS0001,0.7,manual review\n"),
    )
    contract = generated_batch.load_generated_batch_contract(generated_dir)

    with generated_batch.snapshot_generated_batch(
        generated_dir,
        snapshot_parent=snapshot_parent,
        question_ids=[question.question_id],
        question_specs=[question],
        targets=list(contract.targets),
        strict=True,
    ) as snapshot:
        with (snapshot / "at_risk__baseline.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        assert reader.fieldnames == ["student_id", "risk_score", "explanation"]
        assert rows == [
            {
                "student_id": "S0001",
                "risk_score": "0.7",
                "explanation": "manual review",
            }
        ]

    assert not list(snapshot_parent.glob(".generated-batch.snapshot-*"))


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (
            "student_id,student_id\nS0001,S0001\n",
            "header names must be unique",
        ),
        (
            ",risk_score\nS0001,0.7\n",
            "header names must be nonempty",
        ),
        (
            "other_id,risk_score\nS0001,0.7\n",
            "entity key exactly once",
        ),
        (
            "student_id\nS0001,unexpected\n",
            "more fields than the header",
        ),
    ],
    ids=["duplicate-header", "empty-header", "missing-entity-key", "wide-row"],
)
def test_strict_snapshot_rejects_malformed_generated_result_csv(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    snapshot_parent = tmp_path / "snapshots"
    question = _question()
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    _rewrite_result_and_refresh_digest(
        generated_dir,
        filename="at_risk__baseline.csv",
        contents=contents,
    )
    contract = generated_batch.load_generated_batch_contract(generated_dir)

    with (
        pytest.raises(ValueError, match=message),
        generated_batch.snapshot_generated_batch(
            generated_dir,
            snapshot_parent=snapshot_parent,
            question_ids=[question.question_id],
            question_specs=[question],
            targets=list(contract.targets),
            strict=True,
        ),
    ):
        pytest.fail("畸形 CSV 不得进入评估快照")

    assert not list(snapshot_parent.glob(".generated-batch.snapshot-*"))


@pytest.mark.parametrize(
    "contents",
    [
        (
            '{"contract_version":1,"contract_version":1,'
            '"question_ids":["at_risk"],"targets":{},"result_files":{}}'
        ),
        (
            '{"contract_version":1,"question_ids":["at_risk"],'
            '"targets":{},"result_files":{},"unknown":true}'
        ),
        (
            '{"contract_version":NaN,"question_ids":["at_risk"],'
            '"targets":{},"result_files":{}}'
        ),
    ],
)
def test_evaluation_rejects_malformed_contract_json(
    tmp_path: Path,
    contents: str,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = tmp_path / "generated"
    output_dir = tmp_path / "evaluation"
    _build_run(run_dir)
    _generate_batch(run_dir, generated_dir)
    (generated_dir / "batch_contract.json").write_text(contents, encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match="batch contract"):
        _evaluate(run_dir, generated_dir, output_dir)

    assert not output_dir.exists()


# --- 生成目录事务发布 ---


class TransactionalSqlGenerator:
    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        return question.reference_sql or ""

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str:
        raise AssertionError("合法 SQL 不应进入修复分支")


class FailedSqlGenerator(TransactionalSqlGenerator):
    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        raise RuntimeError("模型暂时不可用")


class FileWritingRuntime:
    def __init__(self, *, fail_on_sql_token: str | None = None) -> None:
        self.fail_on_sql_token = fail_on_sql_token

    def execute(
        self,
        sql: str,
        *,
        output_csv: Path | None = None,
        required_columns: tuple[str, ...] = (),
    ) -> list[dict[str, str]]:
        if self.fail_on_sql_token and self.fail_on_sql_token in sql:
            raise RuntimeError("reference query failed")
        rows = [{"student_id": "S0001"}]
        if output_csv is not None:
            output_csv.parent.mkdir(parents=True, exist_ok=True)
            output_csv.write_text("student_id\nS0001\n", encoding="utf-8")
        return rows


def question(question_id: str, marker: str = "S0001") -> QuestionSpec:
    return QuestionSpec(
        question_id=question_id,
        question=f"Which students match {question_id}?",
        reference_sql=(
            f"SELECT student_id FROM academic_records WHERE student_id = '{marker}';"
        ),
    )


def run_batch(
    *,
    tmp_path: Path,
    questions: list[QuestionSpec],
    generated_dir: Path,
    generator: TransactionalSqlGenerator | FailedSqlGenerator,
    runtime: FileWritingRuntime,
):
    target = TextToSqlTarget(label="baseline", variant_dir=tmp_path / "baseline")
    return runner._run_text_to_sql_with_runtimes(
        questions=questions,
        targets=[target],
        generator=generator,
        model="static",
        max_retries=0,
        generated_results_dir=generated_dir,
        schema_context="schema",
        runtimes={"baseline": runtime},
    )


def staging_paths(generated_dir: Path) -> list[Path]:
    return list(generated_dir.parent.glob(f".{generated_dir.name}.*"))


def test_uncaught_mid_batch_error_preserves_previous_complete_directory(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    sentinel = generated_dir / "previous-complete.csv"
    sentinel.write_text("old batch", encoding="utf-8")

    with pytest.raises(RuntimeError, match="reference query failed"):
        run_batch(
            tmp_path=tmp_path,
            questions=[question("first"), question("second", "FAIL_REFERENCE")],
            generated_dir=generated_dir,
            generator=TransactionalSqlGenerator(),
            runtime=FileWritingRuntime(fail_on_sql_token="FAIL_REFERENCE"),
        )

    assert sentinel.read_text(encoding="utf-8") == "old batch"
    assert sorted(path.name for path in generated_dir.iterdir()) == [sentinel.name]
    assert staging_paths(generated_dir) == []


def test_model_failure_preserves_previous_complete_directory(tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    sentinel = generated_dir / "first__baseline.csv"
    sentinel.write_text("old batch", encoding="utf-8")

    results = run_batch(
        tmp_path=tmp_path,
        questions=[question("first")],
        generated_dir=generated_dir,
        generator=FailedSqlGenerator(),
        runtime=FileWritingRuntime(),
    )

    assert [result.success for result in results] == [False]
    assert sentinel.read_text(encoding="utf-8") == "old batch"
    assert sorted(path.name for path in generated_dir.iterdir()) == [sentinel.name]
    assert staging_paths(generated_dir) == []


def test_successful_batch_replaces_previous_directory_as_one_unit(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    (generated_dir / "previous-complete.csv").write_text("old batch", encoding="utf-8")

    results = run_batch(
        tmp_path=tmp_path,
        questions=[question("first"), question("second")],
        generated_dir=generated_dir,
        generator=TransactionalSqlGenerator(),
        runtime=FileWritingRuntime(),
    )

    assert all(result.success for result in results)
    assert sorted(path.name for path in generated_dir.iterdir()) == [
        "batch_contract.json",
        "first__baseline.csv",
        "second__baseline.csv",
    ]
    assert all(
        path.read_text(encoding="utf-8") == "student_id\nS0001\n"
        for path in generated_dir.glob("*.csv")
    )
    assert staging_paths(generated_dir) == []


def test_publish_failure_restores_previous_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    sentinel = generated_dir / "previous-complete.csv"
    sentinel.write_text("old batch", encoding="utf-8")
    original_replace = Path.replace

    def fail_staging_publish(source: Path, target: Path) -> Path:
        if source.name.startswith(".generated.staging-") and target == generated_dir:
            raise OSError("simulated publish failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_staging_publish)

    with pytest.raises(OSError, match="simulated publish failure"):
        run_batch(
            tmp_path=tmp_path,
            questions=[question("first")],
            generated_dir=generated_dir,
            generator=TransactionalSqlGenerator(),
            runtime=FileWritingRuntime(),
        )

    assert sentinel.read_text(encoding="utf-8") == "old batch"
    assert sorted(path.name for path in generated_dir.iterdir()) == [sentinel.name]
    assert staging_paths(generated_dir) == []


def test_publish_failure_after_rename_restores_only_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    sentinel = generated_dir / "previous-complete.csv"
    sentinel.write_text("old batch", encoding="utf-8")
    original_replace = Path.replace

    def publish_then_fail(source: Path, target: Path) -> Path:
        if source.name.startswith(".generated.staging-") and target == generated_dir:
            original_replace(source, target)
            raise OSError("simulated post-rename failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", publish_then_fail)

    with pytest.raises(OSError, match="simulated post-rename failure"):
        run_batch(
            tmp_path=tmp_path,
            questions=[question("first")],
            generated_dir=generated_dir,
            generator=TransactionalSqlGenerator(),
            runtime=FileWritingRuntime(),
        )

    assert sentinel.read_text(encoding="utf-8") == "old batch"
    assert sorted(path.name for path in generated_dir.iterdir()) == [sentinel.name]
    assert staging_paths(generated_dir) == []


def test_publish_rollback_preserves_a_concurrent_replacement_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    (generated_dir / "previous-complete.csv").write_text("old batch", encoding="utf-8")
    concurrent_staging = tmp_path / ".concurrent.staging"
    concurrent_staging.mkdir()
    concurrent_marker = concurrent_staging / "concurrent-complete.csv"
    concurrent_marker.write_text("concurrent batch", encoding="utf-8")
    displaced_batch = tmp_path / ".concurrent.previous"
    original_replace = Path.replace

    def publish_then_lose_ownership(source: Path, target: Path) -> Path:
        if source.name.startswith(".generated.staging-") and target == generated_dir:
            original_replace(source, target)
            original_replace(generated_dir, displaced_batch)
            original_replace(concurrent_staging, generated_dir)
            raise OSError("simulated post-rename failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", publish_then_lose_ownership)

    with pytest.raises(RuntimeError, match="ownership"):
        run_batch(
            tmp_path=tmp_path,
            questions=[question("first")],
            generated_dir=generated_dir,
            generator=TransactionalSqlGenerator(),
            runtime=FileWritingRuntime(),
        )

    assert (generated_dir / concurrent_marker.name).read_text(
        encoding="utf-8"
    ) == "concurrent batch"
    backups = list(tmp_path.glob(".generated.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "previous-complete.csv").read_text(encoding="utf-8") == (
        "old batch"
    )


@pytest.mark.parametrize(
    "unsafe_directory",
    [Path("."), Path("/"), Path("safe/../escape")],
)
def test_unsafe_directory_cannot_be_used_as_generated_results_target(
    tmp_path: Path,
    unsafe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="generated_results_dir"):
        run_batch(
            tmp_path=tmp_path,
            questions=[question("first")],
            generated_dir=unsafe_directory,
            generator=TransactionalSqlGenerator(),
            runtime=FileWritingRuntime(),
        )
