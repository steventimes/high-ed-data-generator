from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import duckdb
import pytest
from benchmark import analysis, cli
from benchmark.analysis import analyze_run
from benchmark.cli import _filter_questions, build_parser
from benchmark.evaluation.metrics import write_csv as write_metrics_csv
from benchmark.evaluation.pipeline import (
    _validate_evaluation_output_dir,
    evaluate_text_to_sql_outputs,
)
from benchmark.questions import DEFAULT_REGISTRY, QuestionSpec, load_questions
from benchmark.sql_runtime import write_rows
from benchmark.text_to_sql.runner import (
    TextToSqlTarget,
    _validate_generated_results_directory,
    resolve_targets,
    run_generated_sql_against_target,
    run_text_to_sql_experiment,
    validate_generated_sql_against_targets,
    write_results,
)

# 相关回归按职责集中；来源标记用于快速定位历史测试语义。

# --- 生成与评测主链路 ---

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
    baseline_hashes: dict[str, str] | None = None
    operators = (
        "drop_row",
        "null_aid_amount",
        "null_aid_status",
        "identifier_mismatch",
    )
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
        write_csv(
            variant_dir / "identity_crosswalk.csv",
            ["canonical_student_id", "financial_aid_student_id"],
            [
                {"canonical_student_id": "S0001", "financial_aid_student_id": "S0001"},
                {"canonical_student_id": "S0002", "financial_aid_student_id": "S0002"},
            ],
        )
        hashes = {
            name: hashlib.sha256((variant_dir / f"{name}.csv").read_bytes()).hexdigest()
            for name in (
                "academic_records",
                "financial_aid_records",
                "identity_crosswalk",
            )
        }
        if baseline_hashes is None:
            baseline_hashes = hashes.copy()
        manifest = {
            "manifest_version": 1,
            "variant": variant,
            "baseline_dataset_id": "a" * 64,
            "baseline_file_hashes": baseline_hashes,
            "variant_file_hashes": hashes,
            "random_seed": 42,
            "corruption_percentages": {name: 0.0 for name in operators},
            "selected_row_ids": {name: [] for name in operators},
            "fragmentation_score": 1.0,
            "invariants": {
                "mutate_academic_records": False,
                "regenerate_population_per_variant": False,
                "corruption_applies_only_to": "financial_aid_records",
            },
        }
        manifest_path = run_dir / "manifests" / f"{variant}_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def configure_v2_cohort(run_dir: Path, variants: list[str] = VARIANTS) -> None:
    baseline_hashes: dict[str, str] | None = None
    for variant in variants:
        variant_dir = run_dir / "variants" / variant
        write_csv(
            variant_dir / "financial_aid_late_arrivals.csv",
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [],
        )
        write_csv(
            variant_dir / "identity_crosswalk.csv",
            ["canonical_student_id", "financial_aid_student_id"],
            [
                {"canonical_student_id": "S0001", "financial_aid_student_id": "S0001"},
                {"canonical_student_id": "S0002", "financial_aid_student_id": "S0002"},
            ],
        )
        write_csv(
            variant_dir / "aid_status_crosswalk.csv",
            ["financial_aid_status", "canonical_aid_status"],
            [
                {"financial_aid_status": "active", "canonical_aid_status": "active"},
                {
                    "financial_aid_status": "suspended",
                    "canonical_aid_status": "suspended",
                },
            ],
        )
        hashes = {
            path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in variant_dir.glob("*.csv")
        }
        if baseline_hashes is None:
            baseline_hashes = hashes.copy()
        manifest_path = run_dir / "manifests" / f"{variant}_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "manifest_version": 2,
                "variant": variant,
                "variant_file_hashes": hashes,
                "baseline_dataset_id": "a" * 64,
                "random_seed": 42,
                "baseline_file_hashes": baseline_hashes,
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def mutate_manifest(run_dir: Path, variant: str, **changes: object) -> None:
    manifest_path = run_dir / "manifests" / f"{variant}_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(changes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def configure_v3_temporal_variant(run_dir: Path, variant: str) -> None:
    variant_dir = run_dir / "variants" / variant
    is_baseline = variant == "baseline"
    write_csv(
        variant_dir / "financial_aid_late_arrivals.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        []
        if is_baseline
        else [
            {
                "student_id": "S0001",
                "aid_amount": "1000.00",
                "aid_status": "suspended",
                "disbursement_date": "2024-09-01",
            }
        ],
    )
    write_csv(
        variant_dir / "identity_crosswalk.csv",
        ["canonical_student_id", "financial_aid_student_id"],
        [
            {"canonical_student_id": "S0001", "financial_aid_student_id": "S0001"},
            {"canonical_student_id": "S0002", "financial_aid_student_id": "S0002"},
        ],
    )
    write_csv(
        variant_dir / "aid_status_crosswalk.csv",
        ["financial_aid_status", "canonical_aid_status"],
        [
            {"financial_aid_status": "active", "canonical_aid_status": "active"},
            {
                "financial_aid_status": "suspended",
                "canonical_aid_status": "suspended",
            },
        ],
    )
    publication_events = [
        {
            "event_id": "aid-disbursement::S0002",
            "financial_aid_student_id": "S0002",
            "event_time": "2024-09-01",
            "observed_at": "2024-09-01T00:00:00Z",
            "published_at": "2024-10-02T00:00:00Z",
            "arrival_stream": "current",
        },
        {
            "event_id": "aid-disbursement::S0001",
            "financial_aid_student_id": "S0001",
            "event_time": "2024-09-01",
            "observed_at": "2024-09-01T00:00:00Z",
            "published_at": (
                "2024-10-02T00:00:00Z" if is_baseline else "2024-10-09T00:00:00Z"
            ),
            "arrival_stream": "current" if is_baseline else "late",
        },
    ]
    write_csv(
        variant_dir / "financial_aid_publication_events.csv",
        [
            "event_id",
            "financial_aid_student_id",
            "event_time",
            "observed_at",
            "published_at",
            "arrival_stream",
        ],
        publication_events,
    )
    hashes = {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in variant_dir.glob("*.csv")
    }
    manifest_path = run_dir / "manifests" / f"{variant}_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if is_baseline:
        baseline_hashes = hashes
    else:
        baseline_manifest = json.loads(
            (run_dir / "manifests" / "baseline_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        baseline_hashes = baseline_manifest["baseline_file_hashes"]
    identity = hashlib.sha256(str(42).encode())
    for dataset_name in (
        "academic_records",
        "financial_aid_records",
        "financial_aid_late_arrivals",
        "identity_crosswalk",
        "aid_status_crosswalk",
    ):
        identity.update(baseline_hashes[dataset_name].encode())
    operators = {
        "aid_status_code_drift",
        "drop_row",
        "identifier_mismatch",
        "null_aid_amount",
        "null_aid_status",
        "publication_delay",
    }
    manifest.update(
        {
            "manifest_version": 3,
            "variant": variant,
            "variant_file_hashes": hashes,
            "baseline_dataset_id": identity.hexdigest(),
            "random_seed": 42,
            "baseline_file_hashes": baseline_hashes,
            "corruption_percentages": {
                **{name: 0.0 for name in operators},
                "publication_delay": 0.0 if is_baseline else 0.5,
            },
            "selected_row_ids": {
                **{name: [] for name in operators},
                "publication_delay": [] if is_baseline else ["S0001"],
            },
            "fragmentation_score": 1.0 if is_baseline else 0.5,
            "invariants": {
                "mutate_academic_records": False,
                "regenerate_population_per_variant": False,
                "corruption_applies_only_to": "financial_aid_domain",
            },
            "temporal": {
                "contract_version": 1,
                "timezone": "UTC",
                "logical_time": True,
                "snapshots": {
                    "current": {
                        "published_at": "2024-10-02T00:00:00Z",
                        "event_time_watermark": "2024-10-01T00:00:00Z",
                    },
                    "replayed": {
                        "published_at": "2024-10-09T00:00:00Z",
                        "event_time_watermark": "2024-10-01T00:00:00Z",
                    },
                },
                "current_record_count": 2 if is_baseline else 1,
                "late_record_count": 0 if is_baseline else 1,
            },
        }
    )
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
    temporal_fields = {
        "current_freshness_lag_days",
        "replayed_freshness_lag_days",
        "replay_delay_days",
        "stale_missed_count",
        "stale_extra_count",
        "stale_action_count",
        "stale_action_denominator",
        "stale_action_rate",
    }
    assert all(row[field] == "" for row in rows for field in temporal_fields)


def test_reference_analysis_rolls_back_both_csvs_when_second_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    metrics_dir = run_dir / "metrics"
    metrics_dir.mkdir()
    metrics_path = metrics_dir / "reference_query_metrics.csv"
    results_path = metrics_dir / "reference_query_results.csv"
    unrelated_path = metrics_dir / "per_query_metrics.csv"
    metrics_path.write_text("old-metrics\n", encoding="utf-8")
    results_path.write_text("old-results\n", encoding="utf-8")
    unrelated_path.write_text("unrelated\n", encoding="utf-8")

    real_replace = os.replace
    failed_once = False

    def fail_second_publish(
        source: Path | str,
        destination: Path | str,
        **kwargs: int,
    ) -> None:
        nonlocal failed_once
        if (
            Path(destination).name == results_path.name
            and kwargs.get("dst_dir_fd") is not None
            and not failed_once
        ):
            failed_once = True
            raise OSError("forced second publish failure")
        real_replace(source, destination, **kwargs)

    question = QuestionSpec(
        question_id="at_risk",
        question="Which students are at risk?",
        reference_sql="SELECT student_id FROM academic_records WHERE gpa < 2.5;",
    )
    with monkeypatch.context() as context:
        context.setattr(os, "replace", fail_second_publish)
        with pytest.raises(OSError, match="forced second publish failure"):
            analyze_run(run_dir, [question])

    assert metrics_path.read_text(encoding="utf-8") == "old-metrics\n"
    assert results_path.read_text(encoding="utf-8") == "old-results\n"
    assert unrelated_path.read_text(encoding="utf-8") == "unrelated\n"
    assert sorted(path.name for path in metrics_dir.iterdir()) == [
        "per_query_metrics.csv",
        "reference_query_metrics.csv",
        "reference_query_results.csv",
    ]

    analyze_run(run_dir, [question])

    assert metrics_path.read_text(encoding="utf-8") != "old-metrics\n"
    assert results_path.read_text(encoding="utf-8") != "old-results\n"
    assert unrelated_path.read_text(encoding="utf-8") == "unrelated\n"


def test_evaluation_quantifies_stale_decisions_until_late_rows_are_replayed(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    for variant in VARIANTS:
        variant_dir = run_dir / "variants" / variant
        write_csv(
            variant_dir / "financial_aid_records.csv",
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [
                {
                    "student_id": "S0002",
                    "aid_amount": "500.00",
                    "aid_status": "active",
                    "disbursement_date": "2024-09-01",
                },
                *(
                    [
                        {
                            "student_id": "S0001",
                            "aid_amount": "1000.00",
                            "aid_status": "suspended",
                            "disbursement_date": "2024-09-01",
                        }
                    ]
                    if variant == "baseline"
                    else []
                ),
            ],
        )
        configure_v3_temporal_variant(run_dir, variant)

    question = next(
        item
        for item in load_questions(DEFAULT_REGISTRY)
        if item.question_id == "at_risk_financial_disruption_governed"
    )
    assert question.temporal_evaluation is not None
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
        metrics = {row["variant_name"]: row for row in csv.DictReader(handle)}

    high = metrics["high_fragmentation"]
    assert high["current_freshness_lag_days"] == "1"
    assert high["replayed_freshness_lag_days"] == "8"
    assert high["replay_delay_days"] == "7"
    assert high["stale_missed_count"] == "1"
    assert high["stale_extra_count"] == "0"
    assert high["stale_action_count"] == "1"
    assert high["stale_action_denominator"] == "1"
    assert high["stale_action_rate"] == "1"
    assert metrics["baseline"]["stale_action_rate"] == "0"
    assert all(metrics[variant]["stale_action_rate"] == "1" for variant in VARIANTS[1:])


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


def test_failed_text_to_sql_run_preserves_previous_complete_results(
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
    with stale_path.open(encoding="utf-8", newline="") as handle:
        assert list(csv.DictReader(handle)) == [{"student_id": "STALE"}]


def test_text_to_sql_exception_preserves_previous_complete_results(
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

    with stale_path.open(encoding="utf-8", newline="") as handle:
        assert list(csv.DictReader(handle)) == [{"student_id": "STALE"}]


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
            strict=False,
        )


def _write_generated_results(
    generated_dir: Path,
    question_ids: list[str],
    *,
    variants: list[str] = VARIANTS,
) -> None:
    for question_id in question_ids:
        for variant in variants:
            write_csv(
                generated_dir / f"{question_id}__{variant}.csv",
                ["student_id"],
                [{"student_id": "S0001"}],
            )


def _simple_question(question_id: str) -> QuestionSpec:
    return QuestionSpec(
        question_id=question_id,
        question=f"Which students match {question_id}?",
    )


def test_evaluation_failure_keeps_previous_output_and_removes_staging(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    generated_dir = tmp_path / "generated"
    _write_generated_results(generated_dir, ["first"])
    _write_generated_results(generated_dir, ["second"], variants=["baseline"])
    output_dir = tmp_path / "evaluation"
    sentinel = output_dir / "sentinel.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("previous complete evaluation", encoding="utf-8")

    with pytest.raises(ValueError, match="plot format"):
        evaluate_text_to_sql_outputs(
            run_dir=run_dir,
            questions=[_simple_question("first"), _simple_question("second")],
            generated_results_dir=generated_dir,
            output_dir=output_dir,
            plot_formats=["invalid"],
            strict=False,
        )

    assert sentinel.read_text(encoding="utf-8") == "previous complete evaluation"
    assert list(output_dir.iterdir()) == [sentinel]
    assert not list(tmp_path.glob(".evaluation.staging-*"))


def test_successful_evaluation_replaces_previous_output_and_returns_final_paths(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    generated_dir = tmp_path / "generated"
    _write_generated_results(generated_dir, ["at_risk"])
    output_dir = tmp_path / "evaluation"
    sentinel = output_dir / "sentinel.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("stale", encoding="utf-8")

    outputs = evaluate_text_to_sql_outputs(
        run_dir=run_dir,
        questions=[_simple_question("at_risk")],
        generated_results_dir=generated_dir,
        output_dir=output_dir,
        plot_formats=[],
        strict=False,
    )

    assert not sentinel.exists()
    assert outputs == {
        "per_query_metrics": output_dir / "metrics" / "per_query_metrics.csv",
        "missed_students": output_dir / "metrics" / "missed_students.csv",
        "extra_students": output_dir / "metrics" / "extra_students.csv",
        "summary": output_dir / "reports" / "summary.md",
    }
    assert all(path.is_file() for path in outputs.values())
    assert not list(tmp_path.glob(".evaluation.staging-*"))


@pytest.mark.parametrize(
    "unsafe_output_dir",
    [Path(""), Path("."), Path("/"), Path("safe/../escape")],
)
def test_evaluation_rejects_unsafe_output_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_output_dir: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="output_dir"):
        evaluate_text_to_sql_outputs(
            run_dir=tmp_path / "run",
            questions=[_simple_question("at_risk")],
            generated_results_dir=tmp_path / "generated",
            output_dir=unsafe_output_dir,
            plot_formats=[],
            strict=True,
        )


def test_evaluation_publish_failure_restores_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    run_dir = tmp_path / "run"
    build_run(run_dir)
    generated_dir = tmp_path / "generated"
    _write_generated_results(generated_dir, ["at_risk"])
    output_dir = tmp_path / "evaluation"
    sentinel = output_dir / "sentinel.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("previous complete evaluation", encoding="utf-8")
    real_replace = os.replace
    replace_count = 0

    def fail_new_output_publish(source: Path, target: Path) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("injected publication failure")
        real_replace(source, target)

    monkeypatch.setattr(
        "benchmark.evaluation.pipeline.os.replace", fail_new_output_publish
    )

    with pytest.raises(OSError, match="injected publication failure"):
        evaluate_text_to_sql_outputs(
            run_dir=run_dir,
            questions=[_simple_question("at_risk")],
            generated_results_dir=generated_dir,
            output_dir=output_dir,
            plot_formats=[],
            strict=False,
        )

    assert sentinel.read_text(encoding="utf-8") == "previous complete evaluation"
    assert list(output_dir.iterdir()) == [sentinel]
    assert not list(tmp_path.glob(".evaluation.staging-*"))
    assert not list(tmp_path.glob(".evaluation.backup-*"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"baseline_dataset_id": "b" * 64}, "different cohorts"),
        ({"random_seed": 43}, "different cohorts"),
        (
            {
                "baseline_file_hashes": {
                    name: ("b" * 64 if name == "academic_records" else "a" * 64)
                    for name in (
                        "academic_records",
                        "financial_aid_records",
                        "financial_aid_late_arrivals",
                        "identity_crosswalk",
                        "aid_status_crosswalk",
                    )
                }
            },
            "different cohorts",
        ),
    ],
)
def test_text_to_sql_rejects_cross_cohort_targets_before_writing(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    configure_v2_cohort(run_dir)
    mutate_manifest(run_dir, "high_fragmentation", **change)
    generated_dir = tmp_path / "generated"

    with pytest.raises(ValueError, match=message):
        run_text_to_sql_experiment(
            questions=[
                QuestionSpec(
                    question_id="at_risk",
                    question="Which students are at risk?",
                    reference_sql=(
                        "SELECT student_id FROM academic_records WHERE gpa < 2.5;"
                    ),
                )
            ],
            targets=[
                TextToSqlTarget(
                    label=variant,
                    variant_dir=run_dir / "variants" / variant,
                )
                for variant in VARIANTS
            ],
            generator=StaticSqlGenerator(),
            model="static",
            max_retries=0,
            generated_results_dir=generated_dir,
        )

    assert not generated_dir.exists()


def test_text_to_sql_rejects_mixed_legacy_and_versioned_targets(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    configure_v2_cohort(run_dir, ["baseline"])

    with pytest.raises(ValueError, match="downgrade|legacy and versioned"):
        run_text_to_sql_experiment(
            questions=[
                QuestionSpec(
                    question_id="at_risk",
                    question="Which students are at risk?",
                    reference_sql="SELECT student_id FROM academic_records;",
                )
            ],
            targets=[
                TextToSqlTarget(
                    label=variant,
                    variant_dir=run_dir / "variants" / variant,
                )
                for variant in ("baseline", "low_fragmentation")
            ],
            generator=StaticSqlGenerator(),
            model="static",
            max_retries=0,
        )


def test_analysis_rejects_cross_cohort_run_before_creating_metrics(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    configure_v2_cohort(run_dir)
    mutate_manifest(run_dir, "medium_fragmentation", random_seed=43)

    with pytest.raises(ValueError, match="different cohorts"):
        analyze_run(
            run_dir,
            [
                QuestionSpec(
                    question_id="at_risk",
                    question="Which students are at risk?",
                    reference_sql="SELECT student_id FROM academic_records;",
                )
            ],
        )

    assert not (run_dir / "metrics").exists()


def test_analysis_uses_one_frozen_run_when_the_source_is_atomically_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    replacement_dir = tmp_path / "replacement"
    build_run(run_dir)
    build_run(replacement_dir)
    replacement_baseline_hashes: dict[str, str] | None = None
    for variant in VARIANTS:
        variant_dir = replacement_dir / "variants" / variant
        academic_path = variant_dir / "academic_records.csv"
        academic_path.write_text(
            academic_path.read_text(encoding="utf-8").replace(
                "S0001,2.10",
                "S0001,3.10",
            ),
            encoding="utf-8",
        )
        hashes = {
            name: hashlib.sha256((variant_dir / f"{name}.csv").read_bytes()).hexdigest()
            for name in (
                "academic_records",
                "financial_aid_records",
                "identity_crosswalk",
            )
        }
        if replacement_baseline_hashes is None:
            replacement_baseline_hashes = hashes.copy()
        manifest_path = replacement_dir / "manifests" / f"{variant}_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["baseline_dataset_id"] = "b" * 64
        manifest["baseline_file_hashes"] = replacement_baseline_hashes
        manifest["variant_file_hashes"] = hashes
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    original_execute = analysis.VariantSqlRuntime.execute
    replaced = False

    def replace_after_reading_baseline(
        runtime: analysis.VariantSqlRuntime,
        sql: str,
        output_csv: Path | str | None = None,
        *,
        required_columns: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        nonlocal replaced
        rows = original_execute(
            runtime,
            sql,
            output_csv,
            required_columns=required_columns,
        )
        if runtime.variant_dir.name == "baseline" and not replaced:
            replaced = True
            run_dir.rename(tmp_path / "old-run")
            replacement_dir.rename(run_dir)
        return rows

    monkeypatch.setattr(
        analysis.VariantSqlRuntime,
        "execute",
        replace_after_reading_baseline,
    )
    with pytest.raises(RuntimeError, match="changed during analysis"):
        analyze_run(
            run_dir,
            [
                QuestionSpec(
                    question_id="at_risk",
                    question="Which students are at risk?",
                    reference_sql=(
                        "SELECT student_id FROM academic_records WHERE gpa < 2.5;"
                    ),
                )
            ],
        )

    assert replaced
    assert not (run_dir / "metrics").exists()


def test_analysis_never_publishes_old_metrics_into_a_replacement_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    replacement_dir = tmp_path / "replacement"
    old_run_dir = tmp_path / "old-run"
    build_run(run_dir)
    build_run(replacement_dir)
    original_write = analysis._write_reference_outputs
    replaced = False

    def replace_at_publish(**kwargs: object) -> None:
        nonlocal replaced
        replaced = True
        run_dir.rename(old_run_dir)
        replacement_dir.rename(run_dir)
        original_write(**kwargs)

    monkeypatch.setattr(
        analysis,
        "_write_reference_outputs",
        replace_at_publish,
    )

    with pytest.raises(RuntimeError, match="changed during analysis"):
        analyze_run(
            run_dir,
            [
                QuestionSpec(
                    question_id="at_risk",
                    question="Which students are at risk?",
                    reference_sql=(
                        "SELECT student_id FROM academic_records WHERE gpa < 2.5;"
                    ),
                )
            ],
        )

    assert replaced
    assert not (run_dir / "metrics").exists()
    assert not (old_run_dir / "metrics").exists()


def test_evaluation_rejects_cross_cohort_run_before_creating_output(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    build_run(run_dir)
    configure_v2_cohort(run_dir)
    mutate_manifest(run_dir, "low_fragmentation", baseline_dataset_id="b" * 64)
    output_dir = tmp_path / "evaluation"

    with pytest.raises(ValueError, match="different cohorts"):
        evaluate_text_to_sql_outputs(
            run_dir=run_dir,
            questions=[
                QuestionSpec(
                    question_id="at_risk",
                    question="Which students are at risk?",
                )
            ],
            generated_results_dir=tmp_path / "generated",
            output_dir=output_dir,
            plot_formats=[],
            strict=True,
        )

    assert not output_dir.exists()


# --- CLI 入口与启动预检 ---


def test_module_cli_exposes_all_pipeline_stages() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "python" / "src")

    completed = subprocess.run(
        [sys.executable, "-m", "benchmark", "--help"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "{analyze,text-to-sql,evaluate}" in completed.stdout


def test_module_entrypoint_can_be_imported_without_running_cli() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "python" / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import benchmark.__main__; print('imported')",
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "imported"


def test_default_registry_loads_outside_repository_cwd(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "python" / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from benchmark.questions import DEFAULT_REGISTRY, load_questions; "
                "questions = load_questions(DEFAULT_REGISTRY); "
                "print(len(questions))"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert int(completed.stdout) > 0


def test_query_filter_rejects_partially_unknown_ids() -> None:
    questions = [QuestionSpec(question_id="known", question="Known question?")]

    with pytest.raises(ValueError, match="unknown"):
        _filter_questions(questions, ["known", "typo"])


def test_evaluate_cli_accepts_the_same_variant_subset_as_generation() -> None:
    args = build_parser().parse_args(
        [
            "evaluate",
            "--variant",
            "baseline",
            "--variant",
            "low_fragmentation",
            "--target",
            "baseline=/tmp/baseline",
        ]
    )

    assert args.variant == ["baseline", "low_fragmentation"]
    assert args.target == ["baseline=/tmp/baseline"]


def test_run_script_uses_the_release_generator(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo_args = tmp_path / "cargo-args.txt"
    fake_cargo = bin_dir / "cargo"
    fake_cargo.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "${CARGO_ARGS_FILE}"\n',
        encoding="utf-8",
    )
    fake_cargo.chmod(0o755)
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "PYTHON_BIN": str(fake_python),
            "CARGO_ARGS_FILE": str(cargo_args),
            "CONFIG_PATH": str(root / "configs" / "benchmark.yaml"),
            "RUN_DIR": str(tmp_path / "run"),
            "REGISTRY_PATH": str(root / "python/src/benchmark/query_registry.json"),
        }
    )
    completed = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--release" in cargo_args.read_text(encoding="utf-8").splitlines()


def test_run_script_rejects_invalid_registry_before_generation(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo_marker = tmp_path / "cargo-called.txt"
    run_dir = tmp_path / "run"
    invalid_registry = tmp_path / "invalid-registry.json"
    invalid_registry.write_text("not valid JSON", encoding="utf-8")

    fake_cargo = bin_dir / "cargo"
    fake_cargo.write_text(
        '#!/usr/bin/env bash\ntouch "${CARGO_MARKER}"\nmkdir -p "${RUN_DIR}"\n',
        encoding="utf-8",
    )
    fake_cargo.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "PYTHON_BIN": sys.executable,
            "CARGO_MARKER": str(cargo_marker),
            "CONFIG_PATH": str(root / "configs" / "benchmark.yaml"),
            "RUN_DIR": str(run_dir),
            "REGISTRY_PATH": str(invalid_registry),
        }
    )
    completed = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not cargo_marker.exists()
    assert not run_dir.exists()


@pytest.mark.parametrize(
    "invalid_field", ["reference_sql", "current_reference_sql", "replay_reference_sql"]
)
@pytest.mark.parametrize(
    ("invalid_sql", "expected_error"),
    [
        ("SELECT missing_column FROM academic_records;", "missing_column"),
        ("SELECT gpa FROM academic_records;", "student_id"),
        ("SELECT student_id AS STUDENT_ID FROM academic_records;", "student_id"),
        (
            "SELECT student_id, student_id FROM academic_records;",
            "duplicate column names",
        ),
    ],
)
def test_run_script_rejects_invalid_registry_sql_before_generation(
    tmp_path: Path,
    invalid_field: str,
    invalid_sql: str,
    expected_error: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    registry_payload = json.loads(
        (root / "python/src/benchmark/query_registry.json").read_text(encoding="utf-8")
    )
    if invalid_field == "reference_sql":
        registry_payload["queries"][0]["reference_sql"] = invalid_sql
    elif invalid_field == "replay_reference_sql":
        temporal_query = next(
            query
            for query in registry_payload["queries"]
            if "temporal_evaluation" in query
        )
        temporal = temporal_query["temporal_evaluation"]
        # 让主 reference 选择有效的 current SQL，单独验证 replay 也必须完成绑定。
        temporal["snapshot"] = "current"
        temporal_query["reference_sql"] = temporal["current_reference_sql"]
        temporal["replay_reference_sql"] = invalid_sql

    else:
        temporal_query = next(
            query
            for query in registry_payload["queries"]
            if "temporal_evaluation" in query
        )
        temporal = temporal_query["temporal_evaluation"]
        # replay 仍是选中且有效的主 SQL，确保 current 字段也不能逃过预检。
        temporal["current_reference_sql"] = invalid_sql
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo_marker = tmp_path / "cargo-called.txt"
    run_dir = tmp_path / "run"
    fake_cargo = bin_dir / "cargo"
    fake_cargo.write_text(
        '#!/usr/bin/env bash\ntouch "${CARGO_MARKER}"\nmkdir -p "${RUN_DIR}"\n',
        encoding="utf-8",
    )
    fake_cargo.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "PYTHON_BIN": sys.executable,
            "CARGO_MARKER": str(cargo_marker),
            "CONFIG_PATH": str(root / "configs" / "benchmark.yaml"),
            "RUN_DIR": str(run_dir),
            "REGISTRY_PATH": str(registry),
        }
    )
    completed = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert expected_error in completed.stderr

    assert completed.returncode != 0
    assert not cargo_marker.exists()
    assert not run_dir.exists()


# --- CLI 汇总输出路径安全 ---


def _layout(tmp_path: Path) -> tuple[Path, Path, list[TextToSqlTarget]]:
    run_dir = tmp_path / "run"
    variant_dir = run_dir / "variants" / "baseline"
    variant_dir.mkdir(parents=True)
    (run_dir / "manifests").mkdir()
    (run_dir / "config_snapshot").mkdir()
    generated_dir = run_dir / "metrics" / "generated"
    targets = [TextToSqlTarget("baseline", variant_dir)]
    return run_dir, generated_dir, targets


@pytest.mark.parametrize(
    "relative_output",
    [
        "metrics/generated/result.csv",
        "metrics/generated/batch_contract.json",
        "variants/baseline/academic_records.csv",
        "variants/output.csv",
        "manifests/output.csv",
        "config_snapshot/output.csv",
    ],
)
def test_text_to_sql_summary_output_cannot_overlap_batch_or_inputs(
    tmp_path: Path,
    relative_output: str,
) -> None:
    run_dir, generated_dir, targets = _layout(tmp_path)

    with pytest.raises(ValueError, match="must not overlap"):
        cli._validate_text_to_sql_summary_output(
            run_dir / relative_output,
            run_dir=run_dir,
            generated_results_dir=generated_dir,
            targets=targets,
        )


def test_text_to_sql_summary_output_rejects_symlink_parent(tmp_path: Path) -> None:
    _, generated_dir, targets = _layout(tmp_path)
    generated_dir.mkdir(parents=True)
    alias = tmp_path / "generated-alias"
    alias.symlink_to(generated_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="must not overlap"):
        cli._validate_text_to_sql_summary_output(
            alias / "batch_contract.json",
            run_dir=tmp_path / "run",
            generated_results_dir=generated_dir,
            targets=targets,
        )


def test_text_to_sql_summary_output_rejects_directory_symlink_and_parent_escape(
    tmp_path: Path,
) -> None:
    run_dir, generated_dir, targets = _layout(tmp_path)
    directory = tmp_path / "directory"
    directory.mkdir()
    regular = tmp_path / "regular.csv"
    regular.write_text("old", encoding="utf-8")
    symlink = tmp_path / "output-link.csv"
    symlink.symlink_to(regular)

    for unsafe in (directory, symlink, Path("safe") / ".." / "output.csv"):
        with pytest.raises(
            ValueError, match="safe CSV path|symbolic link|must be a file"
        ):
            cli._validate_text_to_sql_summary_output(
                unsafe,
                run_dir=run_dir,
                generated_results_dir=generated_dir,
                targets=targets,
            )


def test_default_metrics_sibling_summary_output_is_allowed(tmp_path: Path) -> None:
    run_dir, generated_dir, targets = _layout(tmp_path)
    output = run_dir / "metrics" / "text_to_sql_experiments.csv"

    assert (
        cli._validate_text_to_sql_summary_output(
            output,
            run_dir=run_dir,
            generated_results_dir=generated_dir,
            targets=targets,
        )
        == output.resolve()
    )


def test_cli_rejects_unsafe_summary_before_starting_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, generated_dir, _ = _layout(tmp_path)
    called = False

    def unexpected_generation(**kwargs: object) -> list[object]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(cli, "run_text_to_sql_experiment", unexpected_generation)
    args = Namespace(
        run_dir=run_dir,
        registry=DEFAULT_REGISTRY,
        question=[],
        variant=["baseline"],
        target=[],
        generated_results_dir=generated_dir,
        output=run_dir / "variants" / "baseline" / "academic_records.csv",
        model="unused",
        max_retries=0,
    )

    with pytest.raises(ValueError, match="must not overlap"):
        cli._run_text_to_sql(args)

    assert called is False


# --- 输出目录重叠保护 ---


@pytest.mark.parametrize("name", ["target", "target_parent"])
def test_generation_output_cannot_replace_a_target_tree(
    tmp_path: Path,
    name: str,
) -> None:
    target_dir = tmp_path / "run" / "variants" / "baseline"
    target_dir.mkdir(parents=True)
    destination = target_dir if name == "target" else target_dir.parent

    with pytest.raises(ValueError, match="overlap benchmark input trees"):
        _validate_generated_results_directory(
            destination,
            targets=[TextToSqlTarget("baseline", target_dir)],
        )


@pytest.mark.parametrize("name", ["run", "run_parent", "generated"])
def test_evaluation_output_cannot_replace_an_input_tree(
    tmp_path: Path,
    name: str,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = run_dir / "metrics" / "generated"
    generated_dir.mkdir(parents=True)
    destination = {
        "run": run_dir,
        "run_parent": run_dir.parent,
        "generated": generated_dir,
    }[name]

    with pytest.raises(ValueError, match="must not (equal or contain|overlap)"):
        _validate_evaluation_output_dir(
            destination,
            run_dir=run_dir,
            protected_paths=[
                run_dir / "variants",
                run_dir / "manifests",
                run_dir / "metrics",
                run_dir / "config_snapshot",
                generated_dir,
            ],
        )


def test_nested_default_output_directories_remain_allowed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    generated_dir = run_dir / "metrics" / "generated"
    generated_dir.mkdir(parents=True)

    assert (
        _validate_generated_results_directory(
            generated_dir,
            targets=[TextToSqlTarget("baseline", run_dir / "variants" / "baseline")],
        )
        == generated_dir.resolve()
    )
    evaluation_dir = run_dir / "evaluation" / "text_to_sql"
    assert (
        _validate_evaluation_output_dir(
            evaluation_dir,
            run_dir=run_dir,
            protected_paths=[
                run_dir / "variants",
                run_dir / "manifests",
                run_dir / "metrics",
                run_dir / "config_snapshot",
                generated_dir,
            ],
        )
        == evaluation_dir.resolve()
    )


def test_generation_rejects_a_symlink_parent_and_unselected_sibling(
    tmp_path: Path,
) -> None:
    variants_dir = tmp_path / "run" / "variants"
    baseline = variants_dir / "baseline"
    sibling = variants_dir / "high_fragmentation"
    baseline.mkdir(parents=True)
    sibling.mkdir()
    alias = tmp_path / "variant-alias"
    alias.symlink_to(variants_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="overlap benchmark input trees"):
        _validate_generated_results_directory(
            alias / "high_fragmentation",
            targets=[TextToSqlTarget("baseline", baseline)],
        )


def test_evaluation_rejects_an_output_reaching_run_through_symlink_parent(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    generated_dir = run_dir / "metrics" / "generated"
    generated_dir.mkdir(parents=True)
    alias = tmp_path / "parent-alias"
    real_parent = tmp_path / "real-parent"
    real_run = real_parent / "run"
    real_generated = real_run / "metrics" / "generated"
    real_generated.mkdir(parents=True)
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="must not (equal or contain|overlap)"):
        _validate_evaluation_output_dir(
            alias / "run",
            run_dir=real_run,
            protected_paths=[
                real_run / "variants",
                real_run / "manifests",
                real_run / "metrics",
                real_run / "config_snapshot",
                real_generated,
            ],
        )


# --- 路径边界扩展 ---


def _run_layout(tmp_path: Path) -> tuple[Path, list[TextToSqlTarget]]:
    run_dir = tmp_path / "run"
    variant_dir = run_dir / "variants" / "baseline"
    variant_dir.mkdir(parents=True)
    (run_dir / "manifests").mkdir()
    (run_dir / "config_snapshot").mkdir()
    return run_dir, [TextToSqlTarget("baseline", variant_dir)]


@pytest.mark.parametrize(
    "relative_output",
    [
        "variants/baseline/generated",
        "manifests/generated",
        "config_snapshot/generated",
        "metrics",
    ],
)
def test_generation_directory_rejects_nested_inputs_and_metrics_root(
    tmp_path: Path,
    relative_output: str,
) -> None:
    run_dir, targets = _run_layout(tmp_path)

    with pytest.raises(ValueError, match="must not (overlap|equal)"):
        _validate_generated_results_directory(
            run_dir / relative_output, targets=targets
        )


def test_generation_allows_a_dedicated_metrics_child(tmp_path: Path) -> None:
    run_dir, targets = _run_layout(tmp_path)
    destination = run_dir / "metrics" / "generated"

    assert (
        _validate_generated_results_directory(
            destination,
            targets=targets,
        )
        == destination.resolve()
    )


@pytest.mark.parametrize(
    "relative_output",
    [
        "variants/evaluation",
        "manifests/evaluation",
        "config_snapshot/evaluation",
        "metrics/evaluation",
        "metrics/generated/nested",
    ],
)
def test_evaluation_directory_rejects_nested_input_and_generated_trees(
    tmp_path: Path,
    relative_output: str,
) -> None:
    run_dir, _ = _run_layout(tmp_path)
    generated_dir = run_dir / "metrics" / "generated"

    with pytest.raises(ValueError, match="must not overlap"):
        _validate_evaluation_output_dir(
            run_dir / relative_output,
            run_dir=run_dir,
            protected_paths=[
                run_dir / "variants",
                run_dir / "manifests",
                run_dir / "metrics",
                run_dir / "config_snapshot",
                generated_dir,
            ],
        )


def test_evaluation_allows_default_run_evaluation_child(tmp_path: Path) -> None:
    run_dir, _ = _run_layout(tmp_path)
    generated_dir = run_dir / "metrics" / "generated"
    destination = run_dir / "evaluation" / "text_to_sql"

    assert (
        _validate_evaluation_output_dir(
            destination,
            run_dir=run_dir,
            protected_paths=[
                run_dir / "variants",
                run_dir / "manifests",
                run_dir / "metrics",
                run_dir / "config_snapshot",
                generated_dir,
            ],
        )
        == destination.resolve()
    )


# --- 整批运行事务发布 ---

ROOT = Path(__file__).resolve().parents[2]
STAGING_GLOB = ".run.pipeline-staging-*"


def _small_config(tmp_path: Path) -> Path:
    source = (ROOT / "configs" / "benchmark.yaml").read_text(encoding="utf-8")
    config = tmp_path / "benchmark.yaml"
    config.write_text(source.replace("size: 500", "size: 12"), encoding="utf-8")
    return config


def _cargo_wrapper(tmp_path: Path) -> tuple[Path, Path]:
    real_cargo = shutil.which("cargo")
    assert real_cargo is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "cargo-called.txt"
    wrapper = bin_dir / "cargo"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'touch "${CARGO_MARKER}"\n'
        'exec "${REAL_CARGO}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return bin_dir, marker


def _run_script(
    tmp_path: Path,
    *,
    run_dir: Path,
    registry: Path,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, marker = _cargo_wrapper(tmp_path)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "PYTHON_BIN": sys.executable,
            "REAL_CARGO": shutil.which("cargo") or "cargo",
            "CARGO_MARKER": str(marker),
            "CONFIG_PATH": str(_small_config(tmp_path)),
            "RUN_DIR": str(run_dir),
            "REGISTRY_PATH": str(registry),
        }
    )
    completed = subprocess.run(
        ["bash", "run.sh"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, marker


def test_run_script_keeps_previous_run_when_bound_sql_fails_on_generated_data(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sentinel = run_dir / "sentinel.txt"
    sentinel.write_text("previous complete run", encoding="utf-8")
    payload = json.loads(
        (ROOT / "python/src/benchmark/query_registry.json").read_text(encoding="utf-8")
    )
    payload["queries"][0]["reference_sql"] = (
        "SELECT student_id, CAST(enrollment_status AS INTEGER) AS status_number "
        "FROM academic_records;"
    )
    registry = tmp_path / "runtime-failure-registry.json"
    registry.write_text(json.dumps(payload), encoding="utf-8")

    completed, cargo_marker = _run_script(
        tmp_path,
        run_dir=run_dir,
        registry=registry,
    )

    assert completed.returncode != 0
    assert "Conversion Error" in completed.stderr
    assert cargo_marker.is_file(), "预检通过后必须确实执行 Rust 生成器"
    assert sentinel.read_text(encoding="utf-8") == "previous complete run"
    assert list(run_dir.iterdir()) == [sentinel]
    assert list(tmp_path.glob(STAGING_GLOB)) == []


def test_run_script_publishes_generation_and_metrics_as_one_batch(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sentinel = run_dir / "sentinel.txt"
    sentinel.write_text("stale run", encoding="utf-8")

    completed, cargo_marker = _run_script(
        tmp_path,
        run_dir=run_dir,
        registry=ROOT / "python/src/benchmark/query_registry.json",
    )

    assert completed.returncode == 0, completed.stderr
    assert cargo_marker.is_file()
    assert not sentinel.exists()
    assert (run_dir / "variants" / "baseline" / "academic_records.csv").is_file()
    assert (run_dir / "metrics" / "reference_query_metrics.csv").is_file()
    assert (run_dir / "metrics" / "reference_query_results.csv").is_file()
    assert list(tmp_path.glob(STAGING_GLOB)) == []


def test_publish_failure_restores_previous_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from benchmark.run_transaction import prepare_run_staging, publish_staged_run

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sentinel = run_dir / "sentinel.txt"
    sentinel.write_text("previous complete run", encoding="utf-8")
    staging = prepare_run_staging(str(run_dir))
    (staging / "new.txt").write_text("new complete run", encoding="utf-8")
    real_replace = os.replace

    def fail_new_run_publish(source: Path | str, target: Path | str) -> None:
        if Path(source) == staging and Path(target) == run_dir:
            raise OSError("forced publish failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_new_run_publish)

    with pytest.raises(OSError, match="forced publish failure"):
        publish_staged_run(staging, str(run_dir))

    assert sentinel.read_text(encoding="utf-8") == "previous complete run"
    assert list(run_dir.iterdir()) == [sentinel]
    assert (staging / "new.txt").read_text(encoding="utf-8") == "new complete run"


@pytest.mark.parametrize("unsafe", ["", ".", "./", "/", "/.", "//", "run/../other"])
def test_staging_rejects_ambiguous_or_broad_run_paths(
    tmp_path: Path,
    unsafe: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from benchmark.run_transaction import prepare_run_staging

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="safe run directory"):
        prepare_run_staging(unsafe)


def test_staging_rejects_symlink_and_non_directory_targets(tmp_path: Path) -> None:
    from benchmark.run_transaction import prepare_run_staging

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    symlink = tmp_path / "linked-run"
    symlink.symlink_to(real_directory, target_is_directory=True)
    regular_file = tmp_path / "run-file"
    regular_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="symbolic link"):
        prepare_run_staging(str(symlink))
    with pytest.raises(ValueError, match="not a directory"):
        prepare_run_staging(str(regular_file))


# --- CSV 原子写入与权限 ---


class ExplodingValue:
    def __str__(self) -> str:
        raise RuntimeError("forced serialization failure")


@pytest.mark.parametrize(
    "writer",
    [
        lambda path: write_metrics_csv(path, [{"value": ExplodingValue()}], ["value"]),
        lambda path: write_rows(path, [{"value": ExplodingValue()}], ["value"]),
    ],
)
def test_csv_writers_preserve_previous_file_when_serialization_fails(
    tmp_path: Path,
    writer,
) -> None:
    output = tmp_path / "result.csv"
    output.write_text("previous,complete\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="forced serialization failure"):
        writer(output)

    assert output.read_text(encoding="utf-8") == "previous,complete\n"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.skipif(not hasattr(Path, "chmod"), reason="file modes are unavailable")
def test_csv_writer_uses_normal_umask_permissions_for_new_files(
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.csv"

    write_rows(output, [{"value": "complete"}], ["value"])

    assert output.stat().st_mode & 0o777 == 0o664


def test_csv_writer_preserves_existing_file_permissions(tmp_path: Path) -> None:
    output = tmp_path / "result.csv"
    output.write_text("old\n", encoding="utf-8")
    output.chmod(0o640)

    write_rows(output, [{"value": "complete"}], ["value"])

    assert output.stat().st_mode & 0o777 == 0o640
