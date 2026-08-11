from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmark.questions import DEFAULT_REGISTRY, load_questions, resolve_questions


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
