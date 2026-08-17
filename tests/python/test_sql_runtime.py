from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest
from benchmark import sql_runtime
from benchmark.evaluation.metrics import compare_entity_sets
from benchmark.questions import QuestionSpec, TemporalEvaluation
from benchmark.sql_runtime import (
    SqlValidationError,
    VariantSqlRuntime,
    extract_sql,
    register_csv_views,
    validate_read_only_sql,
)
from benchmark.temporal import (
    compare_temporal_action_rows,
    compute_temporal_metrics,
    parse_temporal_context,
)
from benchmark.text_to_sql.runner import (
    TextToSqlTarget,
    entity_ids,
    generate_question_sql,
    run_generated_sql_against_target,
    run_text_to_sql_experiment,
)

# 相关回归按职责集中；来源标记用于快速定位历史测试语义。

# --- SQL 运行时与资源边界 ---


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


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT a.student_id FROM academic_records AS a "
            "JOIN financial_aid_records AS f ON TRUE"
        ),
        (
            "SELECT a.student_id FROM academic_records AS a "
            "JOIN financial_aid_records AS f ON 1 = 1"
        ),
    ],
)
def test_sql_validation_rejects_constant_join_conditions(sql: str) -> None:
    with pytest.raises(ValueError, match="Cartesian"):
        validate_read_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sleep_ms(1);",
        "SELECT write_log('benchmark escape');",
        "SELECT setseed(0.5);",
    ],
)
def test_sql_validation_rejects_side_effect_functions(sql: str) -> None:
    with pytest.raises(ValueError, match="function"):
        validate_read_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT list_transform(range(10000000), x -> x) AS payload, "
            "student_id FROM academic_records LIMIT 1"
        ),
        "SELECT repeat(student_id, 1000000) FROM academic_records",
        "SELECT list(student_id) FROM academic_records",
        "SELECT array_agg(student_id) FROM academic_records",
    ],
)
def test_sql_validation_rejects_single_row_collection_expansion(sql: str) -> None:
    with pytest.raises(SqlValidationError, match="collection|resource"):
        validate_read_only_sql(sql)


def test_sql_validation_rejects_a_join_condition_that_ignores_the_new_table() -> None:
    sql = (
        "SELECT a.student_id FROM academic_records AS a "
        "JOIN financial_aid_records AS f ON a.student_id = f.student_id "
        "JOIN identity_crosswalk AS x ON a.student_id = f.student_id"
    )

    with pytest.raises(ValueError, match="Cartesian"):
        validate_read_only_sql(sql)


@pytest.mark.parametrize(
    "condition",
    [
        "(a.student_id = f.student_id) IS NOT NULL",
        "NOT (a.student_id = f.student_id)",
        "(a.student_id = f.student_id) = FALSE",
        ("CASE WHEN a.student_id = f.student_id THEN TRUE ELSE TRUE END"),
        "COALESCE(a.student_id = f.student_id, TRUE)",
    ],
)
def test_sql_validation_rejects_nested_or_negated_join_equalities(
    condition: str,
) -> None:
    sql = (
        "SELECT a.student_id FROM academic_records AS a "
        f"JOIN financial_aid_records AS f ON {condition}"
    )

    with pytest.raises(ValueError, match="Cartesian"):
        validate_read_only_sql(sql)


def test_sql_validation_allows_a_bare_column_equality_in_an_and_condition() -> None:
    sql = (
        "SELECT a.student_id FROM academic_records AS a "
        "JOIN financial_aid_records AS f "
        "ON a.student_id = f.student_id AND f.aid_amount > 0"
    )

    assert validate_read_only_sql(sql) == sql


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
        "SELECT * FROM academic_records NATURAL JOIN identity_crosswalk",
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


def test_governed_views_replay_late_rows_and_resolve_local_status_codes(
    tmp_path: Path,
) -> None:
    datasets = [
        (
            "academic_records.csv",
            ["student_id", "gpa", "enrollment_status", "semester"],
            [["S0001", "2.10", "full_time", "Fall 2024"]],
        ),
        (
            "financial_aid_records.csv",
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [],
        ),
        (
            "financial_aid_late_arrivals.csv",
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [
                [
                    "financial-aid::S0001",
                    "1200.00",
                    "financial-aid::suspended",
                    "2024-09-01",
                ]
            ],
        ),
        (
            "identity_crosswalk.csv",
            ["canonical_student_id", "financial_aid_student_id"],
            [["S0001", "financial-aid::S0001"]],
        ),
        (
            "aid_status_crosswalk.csv",
            ["financial_aid_status", "canonical_aid_status"],
            [["financial-aid::suspended", "suspended"]],
        ),
        (
            "financial_aid_publication_events.csv",
            [
                "event_id",
                "financial_aid_student_id",
                "event_time",
                "observed_at",
                "published_at",
                "arrival_stream",
            ],
            [
                [
                    "aid-disbursement::financial-aid::S0001",
                    "financial-aid::S0001",
                    "2024-09-01",
                    "2024-09-01T00:00:00Z",
                    "2024-10-09T00:00:00Z",
                    "late",
                ]
            ],
        ),
    ]
    for filename, header, rows in datasets:
        with (tmp_path / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    with (
        pytest.raises(FileNotFoundError, match="verifiable manifest"),
        VariantSqlRuntime(tmp_path),
    ):
        pass


def _write_contract_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _write_v2_variant(run_dir: Path) -> Path:
    variant_dir = run_dir / "variants" / "high_fragmentation"
    datasets = {
        "academic_records": (
            ["student_id", "gpa", "enrollment_status", "semester"],
            [["S0001", "2.10", "full_time", "Fall 2024"]],
        ),
        "financial_aid_records": (
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [["S0001", "1000.00", "suspended", "2024-09-01"]],
        ),
        "financial_aid_late_arrivals": (
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [],
        ),
        "identity_crosswalk": (
            ["canonical_student_id", "financial_aid_student_id"],
            [["S0001", "S0001"]],
        ),
        "aid_status_crosswalk": (
            ["financial_aid_status", "canonical_aid_status"],
            [["suspended", "suspended"]],
        ),
    }
    hashes = {}
    for name, (header, rows) in datasets.items():
        csv_path = variant_dir / f"{name}.csv"
        _write_contract_csv(csv_path, header, rows)
        hashes[name] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "variant": "high_fragmentation",
                "variant_file_hashes": hashes,
                "baseline_dataset_id": "a" * 64,
                "random_seed": 42,
                "baseline_file_hashes": hashes,
            }
        ),
        encoding="utf-8",
    )
    return variant_dir


def _refresh_manifest_hash(run_dir: Path, dataset_name: str) -> None:
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    csv_path = run_dir / "variants" / "high_fragmentation" / f"{dataset_name}.csv"
    manifest["variant_file_hashes"][dataset_name] = hashlib.sha256(
        csv_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_baseline_manifest_hashes_must_match_verified_baseline_files(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    high_dir = _write_v2_variant(run_dir)
    baseline_dir = high_dir.parent / "baseline"
    high_dir.rename(baseline_dir)

    high_manifest = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(high_manifest.read_text(encoding="utf-8"))
    manifest["variant"] = "baseline"
    manifest["baseline_file_hashes"]["academic_records"] = "f" * 64
    baseline_manifest = run_dir / "manifests" / "baseline_manifest.json"
    baseline_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    high_manifest.unlink()

    with (
        pytest.raises(
            ValueError, match="baseline_file_hashes.*verified baseline files"
        ),
        VariantSqlRuntime(baseline_dir),
    ):
        pass


_PUBLICATION_EVENT_HEADER = [
    "event_id",
    "financial_aid_student_id",
    "event_time",
    "observed_at",
    "published_at",
    "arrival_stream",
]


def _write_v3_publication_events(
    run_dir: Path,
    rows: list[list[str]],
) -> Path:
    variant_dir = run_dir / "variants" / "high_fragmentation"
    event_path = variant_dir / "financial_aid_publication_events.csv"
    _write_contract_csv(event_path, _PUBLICATION_EVENT_HEADER, rows)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_version"] = 3
    operators = {
        "aid_status_code_drift",
        "drop_row",
        "identifier_mismatch",
        "null_aid_amount",
        "null_aid_status",
        "publication_delay",
    }
    manifest["corruption_percentages"] = {name: 0.0 for name in operators}
    manifest["selected_row_ids"] = {name: [] for name in operators}
    manifest["fragmentation_score"] = 1.0
    manifest["invariants"] = sql_runtime._MANIFEST_V3_INVARIANTS
    manifest["temporal"] = {
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
        "current_record_count": sum(row[-1] == "current" for row in rows),
        "late_record_count": sum(row[-1] == "late" for row in rows),
    }
    manifest["variant_file_hashes"]["financial_aid_publication_events"] = (
        hashlib.sha256(event_path.read_bytes()).hexdigest()
    )
    manifest["baseline_file_hashes"]["financial_aid_publication_events"] = manifest[
        "variant_file_hashes"
    ]["financial_aid_publication_events"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return variant_dir


def _write_two_student_v3_contract(
    run_dir: Path,
    *,
    current_rows: list[list[str]],
    late_rows: list[list[str]],
    identity_rows: list[list[str]],
    selected_operator: str,
    claimed_ids: list[str],
    fragmentation_score: float,
) -> Path:
    """构造两个学生的完整 v3 契约，供换 ID 攻击测试复用。"""
    variant_dir = _write_v2_variant(run_dir)
    datasets = {
        "academic_records": (
            ["student_id", "gpa", "enrollment_status", "semester"],
            [
                ["S0001", "2.10", "full_time", "Fall 2024"],
                ["S0002", "3.10", "full_time", "Fall 2024"],
            ],
        ),
        "financial_aid_records": (
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            current_rows,
        ),
        "financial_aid_late_arrivals": (
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            late_rows,
        ),
        "identity_crosswalk": (
            ["canonical_student_id", "financial_aid_student_id"],
            identity_rows,
        ),
        "aid_status_crosswalk": (
            ["financial_aid_status", "canonical_aid_status"],
            [
                ["active", "active"],
                ["financial-aid::active", "active"],
                ["suspended", "suspended"],
                ["financial-aid::suspended", "suspended"],
                ["none", "none"],
                ["financial-aid::none", "none"],
            ],
        ),
    }
    for name, (header, rows) in datasets.items():
        _write_contract_csv(variant_dir / f"{name}.csv", header, rows)
        _refresh_manifest_hash(run_dir, name)

    event_rows = []
    for index, (stream, rows) in enumerate(
        (("current", current_rows), ("late", late_rows)),
        start=1,
    ):
        for offset, row in enumerate(rows):
            event_rows.append(
                [
                    f"event-{index}-{offset}",
                    row[0],
                    row[3],
                    f"{row[3]}T00:00:00Z",
                    "2024-10-02T00:00:00Z"
                    if stream == "current"
                    else "2024-10-09T00:00:00Z",
                    stream,
                ]
            )
    variant_dir = _write_v3_publication_events(run_dir, event_rows)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corruption_percentages"][selected_operator] = len(claimed_ids) / 2
    manifest["selected_row_ids"][selected_operator] = claimed_ids
    manifest["fragmentation_score"] = fragmentation_score
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return variant_dir


def test_v3_runtime_rejects_a_corrupted_variant_disguised_as_baseline(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    high_dir = _write_two_student_v3_contract(
        run_dir,
        current_rows=[["S0002", "1000", "active", "2024-09-01"]],
        late_rows=[],
        identity_rows=[["S0001", "S0001"], ["S0002", "S0002"]],
        selected_operator="drop_row",
        claimed_ids=["S0001"],
        fragmentation_score=0.5,
    )
    baseline_dir = high_dir.parent / "baseline"
    high_dir.rename(baseline_dir)

    high_manifest = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(high_manifest.read_text(encoding="utf-8"))
    manifest["variant"] = "baseline"
    manifest["baseline_file_hashes"] = dict(manifest["variant_file_hashes"])
    dataset_identity = hashlib.sha256()
    dataset_identity.update(str(manifest["random_seed"]).encode())
    for dataset_name in (
        "academic_records",
        "financial_aid_records",
        "financial_aid_late_arrivals",
        "identity_crosswalk",
        "aid_status_crosswalk",
    ):
        dataset_identity.update(manifest["baseline_file_hashes"][dataset_name].encode())
    manifest["baseline_dataset_id"] = dataset_identity.hexdigest()
    baseline_manifest = run_dir / "manifests" / "baseline_manifest.json"
    baseline_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    high_manifest.unlink()

    with (
        pytest.raises(ValueError, match="baseline.*uncorrupted"),
        VariantSqlRuntime(baseline_dir),
    ):
        pass


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("baseline_dataset_id", "not-a-digest", "baseline_dataset_id"),
        ("random_seed", -1, "random_seed"),
        ("random_seed", True, "random_seed"),
    ],
)
def test_versioned_runtime_rejects_invalid_cohort_identity_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_versioned_runtime_rejects_incomplete_baseline_hashes(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["baseline_file_hashes"].pop("identity_crosswalk")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="baseline_file_hashes.*identity_crosswalk"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_runtime_exposes_an_immutable_comparable_cohort_fingerprint(
    tmp_path: Path,
) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")

    with (
        VariantSqlRuntime(variant_dir) as first,
        VariantSqlRuntime(variant_dir) as second,
    ):
        assert first.cohort_fingerprint == second.cohort_fingerprint
        assert first.cohort_fingerprint is not None
        assert first.cohort_fingerprint.random_seed == 42


def test_runtime_rejects_nested_result_types(tmp_path: Path) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")

    with (
        VariantSqlRuntime(variant_dir) as runtime,
        pytest.raises(ValueError, match="collection|binary"),
    ):
        runtime.execute("SELECT [student_id] AS identifiers FROM academic_records")


def test_runtime_rejects_an_oversized_scalar_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")
    monkeypatch.setattr(sql_runtime, "_MAX_RESULT_VALUE_BYTES", 2)

    with (
        VariantSqlRuntime(variant_dir) as runtime,
        pytest.raises(ValueError, match="value exceeding"),
    ):
        runtime.execute("SELECT student_id FROM academic_records")


def test_runtime_rejects_a_missing_manifest_from_a_manifested_run(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest_path.rename(run_dir / "manifests" / "other_manifest.json")

    with pytest.raises(FileNotFoundError, match="manifest.*high_fragmentation"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_v2_runtime_rejects_a_missing_governance_sidecar(tmp_path: Path) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")
    (variant_dir / "aid_status_crosswalk.csv").unlink()

    with pytest.raises(FileNotFoundError, match="aid_status_crosswalk"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_manifestless_targets_derive_a_verifiable_academic_cohort(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    for variant_dir, student_id in ((first_dir, "A001"), (second_dir, "B999")):
        _write_contract_csv(
            variant_dir / "academic_records.csv",
            ["student_id", "gpa", "enrollment_status", "semester"],
            [[student_id, "2.1", "full_time", "Fall 2024"]],
        )
        _write_contract_csv(
            variant_dir / "financial_aid_records.csv",
            [
                "student_id",
                "aid_amount",
                "aid_status",
                "disbursement_date",
            ],
            [[student_id, "1000", "active", "2024-09-01"]],
        )

    with (
        VariantSqlRuntime(first_dir) as first,
        VariantSqlRuntime(second_dir) as second,
    ):
        assert first.cohort_fingerprint is not None
        assert second.cohort_fingerprint is not None
        with pytest.raises(ValueError, match="different cohorts"):
            sql_runtime.require_matching_cohorts(
                [
                    ("first", first.cohort_fingerprint),
                    ("second", second.cohort_fingerprint),
                ]
            )


def test_v2_runtime_rejects_a_file_hash_mismatch(tmp_path: Path) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")
    with (variant_dir / "financial_aid_records.csv").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write("S0002,500.00,active,2024-09-01\n")

    with pytest.raises(ValueError, match="SHA-256"), VariantSqlRuntime(variant_dir):
        pass


def test_runtime_rejects_reordered_mapping_headers(tmp_path: Path) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")
    identity = variant_dir / "identity_crosswalk.csv"
    _write_contract_csv(
        identity,
        ["financial_aid_student_id", "canonical_student_id"],
        [["S0001", "S0001"]],
    )
    manifest_path = tmp_path / "run" / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["variant_file_hashes"]["identity_crosswalk"] = hashlib.sha256(
        identity.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="header"), VariantSqlRuntime(variant_dir):
        pass


@pytest.mark.parametrize(
    ("dataset_name", "header", "rows"),
    [
        (
            "identity_crosswalk",
            ["canonical_student_id", "financial_aid_student_id"],
            [
                ["S0001", "department::S0001"],
                ["S0001", "department::S0001"],
            ],
        ),
        (
            "aid_status_crosswalk",
            ["financial_aid_status", "canonical_aid_status"],
            [["suspended", "suspended"], ["suspended", "none"]],
        ),
    ],
)
def test_runtime_rejects_duplicate_mapping_source_keys(
    tmp_path: Path,
    dataset_name: str,
    header: list[str],
    rows: list[list[str]],
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    _write_contract_csv(variant_dir / f"{dataset_name}.csv", header, rows)
    _refresh_manifest_hash(run_dir, dataset_name)

    with pytest.raises(ValueError, match=rf"{dataset_name}.*source key"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_runtime_rejects_an_unknown_canonical_student_mapping(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    _write_contract_csv(
        variant_dir / "identity_crosswalk.csv",
        ["canonical_student_id", "financial_aid_student_id"],
        [["S9999", "S0001"]],
    )
    _refresh_manifest_hash(run_dir, "identity_crosswalk")

    with pytest.raises(ValueError, match="canonical student"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_runtime_rejects_a_noncanonical_status_mapping(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    _write_contract_csv(
        variant_dir / "aid_status_crosswalk.csv",
        ["financial_aid_status", "canonical_aid_status"],
        [["suspended", "paused"]],
    )
    _refresh_manifest_hash(run_dir, "aid_status_crosswalk")

    with pytest.raises(ValueError, match="canonical aid status"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_v2_runtime_rejects_an_unmapped_aid_student_id(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    _write_contract_csv(
        variant_dir / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        [["department::S0001", "1000.00", "suspended", "2024-09-01"]],
    )
    _refresh_manifest_hash(run_dir, "financial_aid_records")

    with pytest.raises(ValueError, match="identity_crosswalk.*cover"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_v2_runtime_rejects_an_unmapped_nonempty_aid_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    _write_contract_csv(
        variant_dir / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        [["S0001", "1000.00", "department::suspended", "2024-09-01"]],
    )
    _refresh_manifest_hash(run_dir, "financial_aid_records")

    with pytest.raises(ValueError, match="aid_status_crosswalk.*cover"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


@pytest.mark.parametrize(
    ("row", "error"),
    [
        (["", "1000.00", "suspended", "2024-09-01"], "student_id"),
        (["S0001", "1000.00", "suspended", ""], "disbursement_date"),
    ],
)
def test_v2_runtime_rejects_missing_aid_business_identifiers(
    tmp_path: Path,
    row: list[str],
    error: str,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    _write_contract_csv(
        variant_dir / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        [row],
    )
    _refresh_manifest_hash(run_dir, "financial_aid_records")

    with pytest.raises(ValueError, match=error), VariantSqlRuntime(variant_dir):
        pass


def test_v3_runtime_requires_publication_events(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_version"] = 3
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="financial_aid_publication_events"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_v3_runtime_exposes_strict_temporal_snapshot_rows(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )

    with VariantSqlRuntime(variant_dir) as runtime:
        assert runtime.execute(
            "SELECT snapshot, "
            "CAST(published_at AS VARCHAR) AS published_at, "
            "CAST(event_time_watermark AS VARCHAR) AS event_time_watermark, "
            "typeof(published_at) AS published_type, "
            "typeof(event_time_watermark) AS watermark_type "
            "FROM benchmark_temporal_snapshots ORDER BY snapshot"
        ) == [
            {
                "snapshot": "current",
                "published_at": "2024-10-02 00:00:00+00",
                "event_time_watermark": "2024-10-01 00:00:00+00",
                "published_type": "TIMESTAMP WITH TIME ZONE",
                "watermark_type": "TIMESTAMP WITH TIME ZONE",
            },
            {
                "snapshot": "replayed",
                "published_at": "2024-10-09 00:00:00+00",
                "event_time_watermark": "2024-10-01 00:00:00+00",
                "published_type": "TIMESTAMP WITH TIME ZONE",
                "watermark_type": "TIMESTAMP WITH TIME ZONE",
            },
        ]


def test_v3_runtime_validates_temporal_record_counts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["temporal"]["current_record_count"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="current_record_count"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def _downgrade_fixture_to_historical_v1(run_dir: Path) -> Path:
    variant_dir = run_dir / "variants" / "high_fragmentation"
    for name in ("financial_aid_late_arrivals", "aid_status_crosswalk"):
        (variant_dir / f"{name}.csv").unlink()
    hashes = {
        name: hashlib.sha256((variant_dir / f"{name}.csv").read_bytes()).hexdigest()
        for name in (
            "academic_records",
            "financial_aid_records",
            "identity_crosswalk",
        )
    }
    operators = (
        "drop_row",
        "null_aid_amount",
        "null_aid_status",
        "identifier_mismatch",
    )
    manifest = {
        "manifest_version": 1,
        "variant": "high_fragmentation",
        "baseline_dataset_id": "a" * 64,
        "baseline_file_hashes": hashes,
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
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return variant_dir


def test_v1_manifest_keeps_legacy_sidecar_fallbacks(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _downgrade_fixture_to_historical_v1(run_dir)

    with VariantSqlRuntime(variant_dir) as runtime:
        assert runtime.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM financial_aid_late_arrivals) AS late_count, "
            "(SELECT COUNT(*) FROM identity_crosswalk) AS identity_count, "
            "(SELECT COUNT(*) FROM aid_status_crosswalk) AS status_count, "
            "(SELECT COUNT(*) FROM benchmark_temporal_snapshots) "
            "AS temporal_count, "
            "typeof((SELECT max(published_at) "
            "FROM benchmark_temporal_snapshots)) AS published_type"
        ) == [
            {
                "late_count": 0,
                "identity_count": 1,
                "status_count": 3,
                "temporal_count": 0,
                "published_type": "TIMESTAMP WITH TIME ZONE",
            }
        ]


def test_v1_manifest_still_verifies_historical_file_hashes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _downgrade_fixture_to_historical_v1(run_dir)
    with (variant_dir / "financial_aid_records.csv").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write("S0002,500.00,active,2024-09-01\n")

    with pytest.raises(ValueError, match="SHA-256"), VariantSqlRuntime(variant_dir):
        pass


def test_register_csv_views_keeps_identity_as_the_fourth_positional_argument(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    _write_contract_csv(
        variant_dir / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        [["department::S0001", "1000.00", "suspended", "2024-09-01"]],
    )
    _write_contract_csv(
        variant_dir / "identity_crosswalk.csv",
        ["canonical_student_id", "financial_aid_student_id"],
        [["S0001", "department::S0001"]],
    )

    connection = duckdb.connect(database=":memory:")
    try:
        # 第四个位置参数是历史公开 API，必须继续表示 identity crosswalk。
        register_csv_views(
            connection,
            variant_dir / "academic_records.csv",
            variant_dir / "financial_aid_records.csv",
            variant_dir / "identity_crosswalk.csv",
        )
        assert connection.execute(
            "SELECT x.canonical_student_id "
            "FROM financial_aid_records AS f "
            "JOIN identity_crosswalk AS x "
            "ON f.student_id = x.financial_aid_student_id"
        ).fetchall() == [("S0001",)]
    finally:
        connection.close()


def test_register_csv_views_locks_down_external_access(tmp_path: Path) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")
    connection = duckdb.connect(database=":memory:")
    try:
        register_csv_views(
            connection,
            variant_dir / "academic_records.csv",
            variant_dir / "financial_aid_records.csv",
        )
        assert connection.execute(
            "SELECT current_setting('enable_external_access'), "
            "current_setting('lock_configuration'), current_setting('threads')"
        ).fetchone() == (False, True, 2)
        with pytest.raises(duckdb.Error):
            connection.execute("SET threads = 3")
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        (
            [
                [
                    "event-1",
                    "S0001",
                    "2024-09-01",
                    "2024-09-01T00:00:00Z",
                    "2024-09-02T00:00:00Z",
                    "current",
                ],
                [
                    "event-1",
                    "S0001",
                    "2024-09-02",
                    "2024-09-02T00:00:00Z",
                    "2024-09-03T00:00:00Z",
                    "late",
                ],
            ],
            "event_id.*unique",
        ),
        (
            [
                [
                    "event-1",
                    "S0001",
                    "2024-09-01",
                    "2024-09-01T00:00:00Z",
                    "2024-09-02T00:00:00Z",
                    "backfill",
                ]
            ],
            "arrival_stream",
        ),
        (
            [
                [
                    "event-1",
                    "S0001",
                    "",
                    "2024-09-01T00:00:00Z",
                    "2024-09-02T00:00:00Z",
                    "current",
                ]
            ],
            "event_time",
        ),
        (
            [
                [
                    "event-1",
                    "S0001",
                    "2024-09-01",
                    "",
                    "2024-09-02T00:00:00Z",
                    "current",
                ]
            ],
            "observed_at",
        ),
        (
            [
                [
                    "event-1",
                    "S0001",
                    "2024-09-01",
                    "2024-09-01T00:00:00Z",
                    "",
                    "current",
                ]
            ],
            "published_at",
        ),
    ],
)
def test_v3_runtime_rejects_invalid_publication_event_contracts(
    tmp_path: Path,
    rows: list[list[str]],
    error: str,
) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(run_dir, rows)

    with pytest.raises(ValueError, match=error), VariantSqlRuntime(variant_dir):
        pass


@pytest.mark.parametrize(
    ("row", "error"),
    [
        (
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-02T00:00:00Z",
                "2024-09-01T00:00:00Z",
                "current",
            ],
            "published_at.*observed_at",
        ),
        (
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-02T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ],
            "event_time.*observed_at",
        ),
    ],
)
def test_v3_runtime_rejects_inconsistent_publication_times(
    tmp_path: Path,
    row: list[str],
    error: str,
) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(run_dir, [row])

    with pytest.raises(ValueError, match=error), VariantSqlRuntime(variant_dir):
        pass


def test_v3_runtime_rejects_an_orphan_publication_event(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S9999",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )

    with pytest.raises(ValueError, match="publication events.*aid records"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_v3_runtime_rejects_duplicate_aid_business_keys(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    aid_rows = [
        ["S0001", "1000.00", "suspended", "2024-09-01"],
        ["S0001", "500.00", "suspended", "2024-09-01"],
    ]
    _write_contract_csv(
        variant_dir / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        aid_rows,
    )
    _refresh_manifest_hash(run_dir, "financial_aid_records")
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )

    with pytest.raises(ValueError, match="business keys.*unique"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_v3_runtime_allows_multiple_disbursement_events_per_student(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    aid_rows = [
        ["S0001", "1000.00", "suspended", "2024-09-01"],
        ["S0001", "500.00", "suspended", "2024-09-15"],
    ]
    _write_contract_csv(
        variant_dir / "financial_aid_records.csv",
        ["student_id", "aid_amount", "aid_status", "disbursement_date"],
        aid_rows,
    )
    _refresh_manifest_hash(run_dir, "financial_aid_records")
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ],
            [
                "event-2",
                "S0001",
                "2024-09-15",
                "2024-09-15T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ],
        ],
    )

    with VariantSqlRuntime(variant_dir) as runtime:
        assert runtime.execute(
            "SELECT COUNT(*) AS event_count FROM financial_aid_records"
        ) == [{"event_count": 2}]


def test_runtime_rejects_results_over_the_row_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")
    monkeypatch.setattr(sql_runtime, "_MAX_RESULT_ROWS", 0)

    with (
        VariantSqlRuntime(variant_dir) as runtime,
        pytest.raises(ValueError, match="maximum of 0 rows"),
    ):
        runtime.execute("SELECT student_id FROM academic_records")


def test_runtime_rejects_duplicate_result_column_names(tmp_path: Path) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")

    with VariantSqlRuntime(variant_dir) as runtime:  # noqa: SIM117
        with pytest.raises(ValueError, match="duplicate column names"):
            runtime.execute(
                "SELECT a.student_id, f.student_id "
                "FROM academic_records AS a "
                "JOIN financial_aid_records AS f "
                "ON a.student_id = f.student_id"
            )


def test_runtime_converts_a_query_deadline_to_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ImmediateTimer:
        def __init__(
            self,
            _interval: float,
            callback: Callable[[], None],
        ) -> None:
            self.callback = callback
            self.daemon = False

        def start(self) -> None:
            self.callback()

        def cancel(self) -> None:
            pass

    variant_dir = _write_v2_variant(tmp_path / "run")
    with VariantSqlRuntime(variant_dir) as runtime:
        monkeypatch.setattr(sql_runtime, "Timer", ImmediateTimer)
        with pytest.raises(TimeoutError, match="30 seconds"):
            runtime.execute("SELECT student_id FROM academic_records")
        monkeypatch.undo()
        assert runtime.execute("SELECT student_id FROM academic_records") == [
            {"student_id": "S0001"}
        ]


def test_runtime_materializes_a_stable_snapshot(tmp_path: Path) -> None:
    variant_dir = _write_v2_variant(tmp_path / "run")
    with VariantSqlRuntime(variant_dir) as runtime:
        assert runtime.execute(
            "SELECT COUNT(*) AS row_count FROM academic_records"
        ) == [{"row_count": 1}]
        with (variant_dir / "academic_records.csv").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write("S0002,3.20,part_time,Fall 2024\n")
        assert runtime.execute(
            "SELECT COUNT(*) AS row_count FROM academic_records"
        ) == [{"row_count": 1}]


def test_runtime_rejects_a_manifest_downgraded_with_modern_fields(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="downgrade"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_runtime_rejects_v1_among_versioned_sibling_manifests(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest_path.write_text(json.dumps({"manifest_version": 1}), encoding="utf-8")
    (run_dir / "manifests" / "baseline_manifest.json").write_text(
        json.dumps({"manifest_version": 2}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="downgrade"):  # noqa: SIM117
        with VariantSqlRuntime(variant_dir):
            pass


def test_runtime_rejects_v3_manifest_downgraded_to_v2(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_version"] = 2
    manifest.pop("temporal")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="downgrade"), VariantSqlRuntime(variant_dir):
        pass


def test_temporal_policy_is_part_of_the_cohort_fingerprint(tmp_path: Path) -> None:
    fingerprints = []
    for name, replayed_at in (
        ("seven-days", "2024-10-09T00:00:00Z"),
        ("fourteen-days", "2024-10-16T00:00:00Z"),
    ):
        run_dir = tmp_path / name
        _write_v2_variant(run_dir)
        variant_dir = _write_v3_publication_events(
            run_dir,
            [
                [
                    "event-1",
                    "S0001",
                    "2024-09-01",
                    "2024-09-01T00:00:00Z",
                    "2024-10-02T00:00:00Z",
                    "current",
                ]
            ],
        )
        manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["temporal"]["snapshots"]["replayed"]["published_at"] = replayed_at
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with VariantSqlRuntime(variant_dir) as runtime:
            fingerprints.append((name, runtime.cohort_fingerprint))

    assert fingerprints[0][1] != fingerprints[1][1]
    with pytest.raises(ValueError, match="different cohorts"):
        sql_runtime.require_matching_cohorts(fingerprints)


def test_runtime_resolves_a_symlinked_standard_variant_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )
    alias = tmp_path / "target-alias"
    alias.symlink_to(variant_dir, target_is_directory=True)

    with VariantSqlRuntime(alias) as runtime:
        assert runtime.manifest["manifest_version"] == 3
        assert runtime.cohort_fingerprint is not None


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "missing fields.*selected_row_ids"),
        ("score", "fragmentation_score"),
        ("score_drift", "does not match benchmark CSVs"),
        ("unknown", "unknown academic student IDs"),
        ("count", "count does not match"),
        ("invariants", "invariants"),
    ],
)
def test_v3_runtime_rejects_invalid_governance_manifest_fields(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if case == "missing":
        manifest.pop("selected_row_ids")
    elif case == "score":
        manifest["fragmentation_score"] = -99
    elif case == "score_drift":
        manifest["fragmentation_score"] = 0.0
    elif case == "unknown":
        manifest["corruption_percentages"]["publication_delay"] = 1.0
        manifest["selected_row_ids"]["publication_delay"] = ["S9999"]
    elif case == "count":
        manifest["corruption_percentages"]["publication_delay"] = 1.0
    else:
        manifest["invariants"]["mutate_academic_records"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=message), VariantSqlRuntime(variant_dir):
        pass


def test_v3_runtime_rejects_same_size_drop_row_id_swap(tmp_path: Path) -> None:
    variant_dir = _write_two_student_v3_contract(
        tmp_path / "run",
        current_rows=[["S0001", "1000.00", "suspended", "2024-09-01"]],
        late_rows=[],
        identity_rows=[["S0001", "S0001"], ["S0002", "S0002"]],
        selected_operator="drop_row",
        # 实际缺行的是 S0002；这里伪造为另一个已知 ID，数量仍然正确。
        claimed_ids=["S0001"],
        fragmentation_score=0.5,
    )

    with (
        pytest.raises(ValueError, match="drop_row.*do not match"),
        VariantSqlRuntime(variant_dir),
    ):
        pass


def test_v3_runtime_requires_identity_mapping_for_dropped_students(
    tmp_path: Path,
) -> None:
    variant_dir = _write_two_student_v3_contract(
        tmp_path / "run",
        current_rows=[["S0001", "1000.00", "suspended", "2024-09-01"]],
        late_rows=[],
        # S0002 虽没有 aid 行，Rust v3 仍会为完整学业人群保留 identity 映射。
        identity_rows=[["S0001", "S0001"]],
        selected_operator="drop_row",
        claimed_ids=["S0002"],
        fragmentation_score=0.5,
    )

    with (
        pytest.raises(ValueError, match="identity_crosswalk.*every academic"),
        VariantSqlRuntime(variant_dir),
    ):
        pass


def test_v3_runtime_rejects_same_size_identifier_mismatch_id_swap(
    tmp_path: Path,
) -> None:
    variant_dir = _write_two_student_v3_contract(
        tmp_path / "run",
        current_rows=[
            ["S0001", "1000.00", "suspended", "2024-09-01"],
            [
                "financial-aid::S0002",
                "900.00",
                "active",
                "2024-09-02",
            ],
        ],
        late_rows=[],
        identity_rows=[
            ["S0001", "S0001"],
            ["S0002", "financial-aid::S0002"],
        ],
        selected_operator="identifier_mismatch",
        # crosswalk 表明真实错配是 S0002，manifest 却声称是 S0001。
        claimed_ids=["S0001"],
        fragmentation_score=0.5,
    )

    with (
        pytest.raises(ValueError, match="identifier_mismatch.*do not match"),
        VariantSqlRuntime(variant_dir),
    ):
        pass


@pytest.mark.parametrize("operator", ["null_aid_amount", "null_aid_status"])
def test_v3_runtime_rejects_null_operator_id_swap_across_drop_overlap(
    tmp_path: Path,
    operator: str,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_two_student_v3_contract(
        run_dir,
        current_rows=[["S0002", "900.00", "active", "2024-09-02"]],
        late_rows=[],
        identity_rows=[["S0001", "S0001"], ["S0002", "S0002"]],
        selected_operator="drop_row",
        claimed_ids=["S0001"],
        fragmentation_score=0.5,
    )
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corruption_percentages"][operator] = 0.5
    # 真正抽中的 S0001 已被 drop；换成仍可见的 S0002 不能因数量相同而被接受。
    manifest["selected_row_ids"][operator] = ["S0002"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with (
        pytest.raises(ValueError, match=rf"{operator}.*do not match"),
        VariantSqlRuntime(variant_dir),
    ):
        pass


def test_v3_runtime_rejects_status_drift_id_swap_across_null_status_overlap(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    variant_dir = _write_two_student_v3_contract(
        run_dir,
        current_rows=[
            ["S0001", "1000.00", "", "2024-09-01"],
            ["S0002", "900.00", "active", "2024-09-02"],
        ],
        late_rows=[],
        identity_rows=[["S0001", "S0001"], ["S0002", "S0002"]],
        selected_operator="null_aid_status",
        claimed_ids=["S0001"],
        fragmentation_score=5 / 6,
    )
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corruption_percentages"]["aid_status_code_drift"] = 0.5
    # S0001 的漂移被 null_status 遮蔽；伪称 S0002 漂移必须与可见状态不一致。
    manifest["selected_row_ids"]["aid_status_code_drift"] = ["S0002"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with (
        pytest.raises(ValueError, match="aid_status_code_drift.*do not match"),
        VariantSqlRuntime(variant_dir),
    ):
        pass


def test_v3_runtime_rejects_same_size_publication_delay_id_swap(
    tmp_path: Path,
) -> None:
    """防止 manifest 用另一个合法学号冒充真正进入迟到流的学生。"""
    run_dir = tmp_path / "run"
    variant_dir = _write_v2_variant(run_dir)
    datasets = {
        "academic_records": (
            ["student_id", "gpa", "enrollment_status", "semester"],
            [
                ["S0001", "2.10", "full_time", "Fall 2024"],
                ["S0002", "3.10", "full_time", "Fall 2024"],
            ],
        ),
        "financial_aid_records": (
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [["S0001", "1000.00", "suspended", "2024-09-01"]],
        ),
        "financial_aid_late_arrivals": (
            ["student_id", "aid_amount", "aid_status", "disbursement_date"],
            [["S0002", "900.00", "suspended", "2024-09-02"]],
        ),
        "identity_crosswalk": (
            ["canonical_student_id", "financial_aid_student_id"],
            [["S0001", "S0001"], ["S0002", "S0002"]],
        ),
    }
    for name, (header, rows) in datasets.items():
        _write_contract_csv(variant_dir / f"{name}.csv", header, rows)
        _refresh_manifest_hash(run_dir, name)

    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ],
            [
                "event-2",
                "S0002",
                "2024-09-02",
                "2024-09-02T00:00:00Z",
                "2024-10-09T00:00:00Z",
                "late",
            ],
        ],
    )
    manifest_path = run_dir / "manifests" / "high_fragmentation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corruption_percentages"]["publication_delay"] = 0.5
    # 迟到流实际是 S0002；同数量换成 S0001 不应只靠计数蒙混过关。
    manifest["selected_row_ids"]["publication_delay"] = ["S0001"]
    manifest["fragmentation_score"] = 0.5
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with (
        pytest.raises(ValueError, match="publication_delay.*do not match"),
        VariantSqlRuntime(variant_dir),
    ):
        pass


def test_v3_runtime_returns_timestamptz_values_without_fetch_failure(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _write_v2_variant(run_dir)
    variant_dir = _write_v3_publication_events(
        run_dir,
        [
            [
                "event-1",
                "S0001",
                "2024-09-01",
                "2024-09-01T00:00:00Z",
                "2024-10-02T00:00:00Z",
                "current",
            ]
        ],
    )

    with VariantSqlRuntime(variant_dir) as runtime:
        rows = runtime.execute(
            "SELECT financial_aid_student_id AS student_id, published_at "
            "FROM financial_aid_publication_events",
            required_columns=("student_id",),
        )

    assert rows[0]["student_id"] == "S0001"
    assert rows[0]["published_at"].isoformat() == "2024-10-02T00:00:00+00:00"


# --- 时序快照与陈旧行动指标 ---


def temporal_manifest() -> dict[str, object]:
    return {
        "manifest_version": 3,
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
            "current_record_count": 1,
            "late_record_count": 1,
        },
    }


def test_temporal_context_derives_snapshot_freshness_and_replay_delay() -> None:
    context = parse_temporal_context(temporal_manifest())

    assert context is not None
    assert context.current_freshness_lag_days == 1.0
    assert context.replayed_freshness_lag_days == 8.0
    assert context.replay_delay_days == 7.0


def test_legacy_manifest_has_no_temporal_context() -> None:
    assert parse_temporal_context({"manifest_version": 2}) is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda manifest: manifest["temporal"]["snapshots"]["current"].__setitem__(
                "published_at", "2024-10-02T00:00:00"
            ),
            "timezone-aware UTC",
        ),
        (
            lambda manifest: manifest["temporal"]["snapshots"]["current"].__setitem__(
                "event_time_watermark", "2024-10-03T00:00:00Z"
            ),
            "watermark must not be later",
        ),
        (
            lambda manifest: manifest["temporal"]["snapshots"]["replayed"].__setitem__(
                "published_at", "2024-10-01T12:00:00Z"
            ),
            "replayed published_at must not be earlier",
        ),
        (
            lambda manifest: manifest["temporal"]["snapshots"]["replayed"].pop(
                "event_time_watermark"
            ),
            "event_time_watermark",
        ),
    ],
)
def test_temporal_context_rejects_invalid_snapshot_contracts(
    mutate: object, message: str
) -> None:
    manifest = deepcopy(temporal_manifest())
    mutate(manifest)

    with pytest.raises((TypeError, ValueError), match=message):
        parse_temporal_context(manifest)


def test_stale_action_metrics_use_symmetric_difference_over_union() -> None:
    metrics = compare_temporal_action_rows(
        current_rows=[{"student_id": "s0001"}, {"student_id": "S0002"}],
        replay_rows=[{"student_id": "S0002"}, {"student_id": "S0003"}],
        entity_key="student_id",
    )

    assert metrics.stale_missed_count == 1
    assert metrics.stale_extra_count == 1
    assert metrics.stale_action_count == 2
    assert metrics.stale_action_denominator == 3
    assert metrics.stale_action_rate == pytest.approx(2 / 3)


def test_empty_temporal_action_union_has_no_rate() -> None:
    metrics = compare_temporal_action_rows(
        current_rows=[],
        replay_rows=[],
        entity_key="student_id",
    )

    assert metrics.stale_action_count == 0
    assert metrics.stale_action_denominator == 0
    assert metrics.stale_action_rate is None


def test_temporal_action_pair_rejects_duplicate_entities() -> None:
    rows = [{"student_id": "S0001"}, {"student_id": " s0001 "}]

    with pytest.raises(ValueError, match="duplicate student_id"):
        compare_temporal_action_rows(
            current_rows=rows,
            replay_rows=[],
            entity_key="student_id",
        )


def test_temporal_metrics_execute_explicit_current_and_replay_sql() -> None:
    current_sql = "SELECT student_id FROM financial_aid_records;"
    replay_sql = "SELECT student_id FROM financial_aid_late_arrivals;"
    rows_by_sql = {
        current_sql: [{"student_id": "S0001"}, {"student_id": "S0002"}],
        replay_sql: [{"student_id": "S0002"}, {"student_id": "S0003"}],
    }

    def execute(
        sql: str, *, required_columns: tuple[str, ...]
    ) -> list[dict[str, object]]:
        assert required_columns == ("student_id",)
        return rows_by_sql[sql]

    metrics = compute_temporal_metrics(
        manifest=temporal_manifest(),
        temporal_evaluation=TemporalEvaluation(
            current_reference_sql=current_sql,
            replay_reference_sql=replay_sql,
        ),
        entity_key="student_id",
        execute=execute,
    )

    assert metrics.to_record() == {
        "current_freshness_lag_days": 1.0,
        "replayed_freshness_lag_days": 8.0,
        "replay_delay_days": 7.0,
        "stale_missed_count": 1,
        "stale_extra_count": 1,
        "stale_action_count": 2,
        "stale_action_denominator": 3,
        "stale_action_rate": pytest.approx(2 / 3),
    }


# --- 结果集合完整性与隐私隔离 ---


class RecordingRepairGenerator:
    def __init__(self, generated_sql: str) -> None:
        self.generated_sql = generated_sql
        self.repair_errors: list[str] = []

    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        return self.generated_sql

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str:
        self.repair_errors.append(error)
        return self.generated_sql


class FixedRowsRuntime:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def execute(
        self,
        sql: str,
        output_csv: Path | None = None,
        *,
        required_columns: tuple[str, ...] = (),
    ) -> list[dict[str, str]]:
        return list(self.rows)


def _write_variant(path: Path) -> None:
    path.mkdir(parents=True)
    with (path / "academic_records.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["student_id", "gpa", "enrollment_status", "semester"])
        writer.writerow(["S0001", "2.10", "full_time", "Fall 2024"])
    with (path / "financial_aid_records.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["student_id", "aid_amount", "aid_status", "disbursement_date"])
        writer.writerow(["S0001", "1000.00", "suspended", "2024-09-01"])


def test_reference_entity_ids_are_never_sent_to_the_repair_model() -> None:
    question = QuestionSpec(
        question_id="secret",
        question="Which students need action?",
    )
    generator = RecordingRepairGenerator("SELECT student_id FROM academic_records")
    target = TextToSqlTarget(label="baseline", variant_dir=Path("baseline"))

    _generated_sql, error = generate_question_sql(
        question=question,
        generator=generator,
        schema_context="schema",
        targets=[target],
        reference_ids_by_target={"baseline": {"REFERENCE_SECRET"}},
        max_retries=1,
        runtimes={"baseline": FixedRowsRuntime([{"student_id": "GENERATED_SECRET"}])},
    )

    assert generator.repair_errors == []
    assert error is not None
    assert "REFERENCE_SECRET" not in error
    assert "GENERATED_SECRET" not in error
    assert "sample=" not in error


def test_duplicate_entity_values_are_redacted_before_model_repair() -> None:
    question = QuestionSpec(
        question_id="duplicate-secret",
        question="Which students need action?",
    )
    generator = RecordingRepairGenerator("SELECT student_id FROM academic_records")
    target = TextToSqlTarget(label="baseline", variant_dir=Path("baseline"))

    _generated_sql, error = generate_question_sql(
        question=question,
        generator=generator,
        schema_context="schema",
        targets=[target],
        reference_ids_by_target={"baseline": {"SECRET_STUDENT"}},
        max_retries=1,
        runtimes={
            "baseline": FixedRowsRuntime(
                [
                    {"student_id": "SECRET_STUDENT"},
                    {"student_id": "SECRET_STUDENT"},
                ]
            )
        },
    )

    assert len(generator.repair_errors) == 1
    assert "SECRET_STUDENT" not in generator.repair_errors[0]
    assert generator.repair_errors[0] == (
        "Generated SQL returned duplicate entity rows"
    )
    assert error is not None
    assert "SECRET_STUDENT" not in error


def test_published_generated_batch_must_include_a_baseline_target(
    tmp_path: Path,
) -> None:
    variant = tmp_path / "custom"
    _write_variant(variant)
    generated_dir = tmp_path / "generated"

    with pytest.raises(ValueError, match="include baseline"):
        run_text_to_sql_experiment(
            questions=[
                QuestionSpec(
                    question_id="risk",
                    question="Which students need action?",
                    reference_sql="SELECT student_id FROM academic_records",
                )
            ],
            targets=[TextToSqlTarget(label="custom", variant_dir=variant)],
            generator=RecordingRepairGenerator(
                "SELECT student_id FROM academic_records"
            ),
            model="static",
            max_retries=0,
            generated_results_dir=generated_dir,
        )

    assert not generated_dir.exists()


def test_baseline_label_cannot_point_to_a_fragmented_variant(
    tmp_path: Path,
) -> None:
    fragmented = tmp_path / "high_fragmentation"
    _write_variant(fragmented)
    generated_dir = tmp_path / "generated"

    with pytest.raises(ValueError, match="baseline.*actual baseline"):
        run_text_to_sql_experiment(
            questions=[
                QuestionSpec(
                    question_id="risk",
                    question="Which students need action?",
                    reference_sql="SELECT student_id FROM academic_records",
                )
            ],
            targets=[
                TextToSqlTarget(
                    label="baseline",
                    variant_dir=fragmented,
                )
            ],
            generator=RecordingRepairGenerator(
                "SELECT student_id FROM academic_records"
            ),
            model="static",
            max_retries=0,
            generated_results_dir=generated_dir,
        )

    assert not generated_dir.exists()


def test_entity_id_contract_rejects_duplicate_action_rows() -> None:
    with pytest.raises(ValueError, match="duplicate student_id"):
        entity_ids(
            [{"student_id": "S0001"}, {"student_id": "S0001"}],
            "student_id",
        )


@pytest.mark.parametrize("side", ["baseline", "observed"])
def test_metric_comparison_rejects_duplicate_action_rows(side: str) -> None:
    rows = [{"student_id": "S0001"}, {"student_id": "S0001"}]
    baseline_rows = rows if side == "baseline" else []
    observed_rows = rows if side == "observed" else []
    with pytest.raises(ValueError, match=rf"{side.capitalize()}.*duplicate"):
        compare_entity_sets(baseline_rows, observed_rows)


def test_failed_duplicate_result_does_not_leave_an_output_file(tmp_path: Path) -> None:
    variant = tmp_path / "variant"
    _write_variant(variant)
    output = tmp_path / "generated.csv"

    result = run_generated_sql_against_target(
        question=QuestionSpec(
            question_id="duplicate",
            question="Which students need action?",
        ),
        target=TextToSqlTarget(label="variant", variant_dir=variant),
        model="static",
        generated_sql=(
            "SELECT student_id FROM academic_records UNION ALL "
            "SELECT student_id FROM academic_records;"
        ),
        reference_rows=[{"student_id": "S0001"}],
        reference_ids={"S0001"},
        output_csv=output,
    )

    assert result.success is False
    assert "duplicate student_id" in (result.error or "")
    assert not output.exists()
