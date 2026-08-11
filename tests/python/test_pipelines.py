from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb
import pytest
from benchmark.analysis import analyze_run
from benchmark.evaluation.pipeline import evaluate_text_to_sql_outputs
from benchmark.questions import QuestionSpec
from benchmark.text_to_sql.runner import (
    TextToSqlTarget,
    resolve_targets,
    run_generated_sql_against_target,
    run_text_to_sql_experiment,
    validate_generated_sql_against_targets,
    write_results,
)

VARIANTS = [
    "baseline",
    "low_fragmentation",
    "medium_fragmentation",
    "high_fragmentation",
]


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
        raise AssertionError("valid reference SQL must not need repair")


class FlakySqlGenerator:
    def __init__(self) -> None:
        self.generate_calls = 0

    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        self.generate_calls += 1
        if self.generate_calls == 1:
            raise RuntimeError("temporary model failure")
        return question.reference_sql or ""

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str:
        raise AssertionError("a missing SQL candidate must be generated again")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_run(run_dir: Path) -> None:
    for variant in VARIANTS:
        variant_dir = run_dir / "variants" / variant
        write_csv(
            variant_dir / "academic_records.csv",
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
                    "gpa": "3.20",
                    "enrollment_status": "part_time",
                    "semester": "Fall 2024",
                },
            ],
        )
        write_csv(
            variant_dir / "financial_aid_records.csv",
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
                    "aid_amount": "500.00",
                    "aid_status": "active",
                    "disbursement_date": "2024-09-01",
                },
            ],
        )
        manifest = {
            "fragmentation_score": 1.0,
            "selected_row_ids": {
                "drop_row": [],
                "null_aid_amount": [],
                "null_aid_status": [],
            },
        }
        manifest_path = run_dir / "manifests" / f"{variant}_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_python_reference_generation_and_evaluation_pipeline(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="at_risk",
        question="Which students are at risk with suspended aid?",
        reference_sql=(
            "SELECT a.student_id FROM academic_records AS a "
            "JOIN financial_aid_records AS f USING (student_id) "
            "WHERE a.gpa < 2.5 AND f.aid_status = 'suspended';"
        ),
    )
    targets = [
        TextToSqlTarget(label=variant, variant_dir=run_dir / "variants" / variant)
        for variant in VARIANTS
    ]
    generated_dir = run_dir / "generated"
    results = run_text_to_sql_experiment(
        questions=[question],
        targets=targets,
        generator=StaticSqlGenerator(),
        model="static",
        max_retries=0,
        generated_results_dir=generated_dir,
    )
    write_results(run_dir / "text_to_sql.csv", results)

    assert len(results) == 4
    assert all(result.success for result in results)
    outputs = evaluate_text_to_sql_outputs(
        run_dir=run_dir,
        questions=[question],
        generated_results_dir=generated_dir,
        output_dir=run_dir / "evaluation",
        plot_formats=[],
        strict=True,
    )
    with outputs["per_query_metrics"].open(encoding="utf-8", newline="") as handle:
        metrics = list(csv.DictReader(handle))
    assert len(metrics) == 4
    assert {row["miss_rate"] for row in metrics} == {"0"}


def test_reference_analysis_runs_every_registered_query_across_variants(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="at_risk",
        question="Which students are at risk?",
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 2.5;",
    )

    outputs = analyze_run(run_dir, [question])

    with outputs["reference_query_metrics"].open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert {row["query_id"] for row in rows} == {"at_risk"}
    assert {row["missed_count"] for row in rows} == {"0"}


def test_target_labels_must_not_overwrite_each_others_results(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with pytest.raises(ValueError, match="filename"):
        resolve_targets(
            run_dir=tmp_path,
            explicit_targets=[
                f"alpha-beta={first}",
                f"alpha beta={second}",
            ],
        )


def test_text_to_sql_retries_generation_after_a_temporary_model_failure(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="at_risk",
        question="Which students are at risk?",
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 2.5;",
    )
    generator = FlakySqlGenerator()

    results = run_text_to_sql_experiment(
        questions=[question],
        targets=[
            TextToSqlTarget(
                label="baseline",
                variant_dir=run_dir / "variants" / "baseline",
            )
        ],
        generator=generator,
        model="static",
        max_retries=1,
    )

    assert generator.generate_calls == 2
    assert len(results) == 1
    assert results[0].success is True


def test_evaluation_fails_when_no_generated_results_exist(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="at_risk",
        question="Which students are at risk?",
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 2.5;",
    )

    with pytest.raises(ValueError, match="No generated results"):
        evaluate_text_to_sql_outputs(
            run_dir=run_dir,
            questions=[question],
            generated_results_dir=tmp_path / "missing-results",
            output_dir=tmp_path / "evaluation",
            plot_formats=[],
            strict=False,
        )


def test_failed_text_to_sql_run_removes_stale_generated_results(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="at_risk",
        question="Which students are at risk?",
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 2.5;",
    )
    generated_dir = tmp_path / "generated"
    stale_path = generated_dir / "at_risk__baseline.csv"
    write_csv(stale_path, ["student_id"], [{"student_id": "STALE"}])

    results = run_text_to_sql_experiment(
        questions=[question],
        targets=[
            TextToSqlTarget(
                label="baseline",
                variant_dir=run_dir / "variants" / "baseline",
            )
        ],
        generator=FlakySqlGenerator(),
        model="static",
        max_retries=0,
        generated_results_dir=generated_dir,
    )

    assert results[0].success is False
    assert not stale_path.exists()


def test_text_to_sql_clears_all_stale_results_before_running(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    questions = [
        QuestionSpec(
            question_id="broken",
            question="Which students trigger a broken reference query?",
            reference_sql=(
                "SELECT missing_column AS student_id FROM academic_records;"
            ),
        ),
        QuestionSpec(
            question_id="later",
            question="Which students are at risk?",
            reference_sql=("SELECT student_id FROM academic_records WHERE gpa < 2.5;"),
        ),
    ]
    generated_dir = tmp_path / "generated"
    stale_path = generated_dir / "later__baseline.csv"
    write_csv(stale_path, ["student_id"], [{"student_id": "STALE"}])

    with pytest.raises(duckdb.BinderException, match="missing_column"):
        run_text_to_sql_experiment(
            questions=questions,
            targets=[
                TextToSqlTarget(
                    label="baseline",
                    variant_dir=run_dir / "variants" / "baseline",
                )
            ],
            generator=StaticSqlGenerator(),
            model="static",
            max_retries=0,
            generated_results_dir=generated_dir,
        )

    assert not stale_path.exists()


def test_reference_analysis_keeps_entity_key_header_for_empty_results(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="empty",
        question="Which students match an empty cohort?",
        reference_sql="SELECT student_id FROM academic_records WHERE FALSE;",
    )

    outputs = analyze_run(run_dir, [question])

    with outputs["reference_query_results"].open(
        encoding="utf-8",
        newline="",
    ) as handle:
        fieldnames = csv.DictReader(handle).fieldnames
    assert fieldnames == ["query_id", "variant", "student_id"]


def test_final_generated_result_must_still_match_the_reference_set(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    target = TextToSqlTarget(
        label="baseline",
        variant_dir=run_dir / "variants" / "baseline",
    )
    output_csv = tmp_path / "generated.csv"

    result = run_generated_sql_against_target(
        question=QuestionSpec(
            question_id="at_risk",
            question="Which students are at risk?",
            entity_key="student_id",
        ),
        target=target,
        model="static",
        generated_sql=(
            "SELECT student_id FROM academic_records WHERE student_id = 'S0002';"
        ),
        reference_rows=[{"student_id": "S0001"}],
        reference_ids={"S0001"},
        output_csv=output_csv,
    )

    assert result.success is False
    assert result.matches_reference_ids is False
    assert not output_csv.exists()


def test_reference_analysis_rejects_an_empty_result_without_entity_column(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="invalid_empty",
        question="Which students match?",
        reference_sql="SELECT gpa FROM academic_records WHERE FALSE;",
    )

    with pytest.raises(ValueError, match="student_id"):
        analyze_run(run_dir, [question])


def test_generated_sql_requires_entity_column_even_when_result_is_empty(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="empty",
        question="Which students match?",
        entity_key="student_id",
    )

    with pytest.raises(ValueError, match="student_id"):
        validate_generated_sql_against_targets(
            question=question,
            generated_sql="SELECT gpa FROM academic_records WHERE FALSE;",
            targets=[
                TextToSqlTarget(
                    label="baseline",
                    variant_dir=run_dir / "variants" / "baseline",
                )
            ],
            reference_ids_by_target={"baseline": set()},
        )


def test_evaluation_rejects_plot_formats_that_can_escape_the_figures_dir(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    question = QuestionSpec(
        question_id="at_risk",
        question="Which students are at risk?",
    )
    generated_dir = tmp_path / "generated"
    for variant in VARIANTS:
        write_csv(
            generated_dir / f"at_risk__{variant}.csv",
            ["student_id"],
            [{"student_id": "S0001"}],
        )

    with pytest.raises(ValueError, match="plot format"):
        evaluate_text_to_sql_outputs(
            run_dir=run_dir,
            questions=[question],
            generated_results_dir=generated_dir,
            output_dir=tmp_path / "evaluation",
            plot_formats=["../../../escape"],
            strict=True,
        )
