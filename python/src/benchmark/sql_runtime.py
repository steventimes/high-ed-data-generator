from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Timer
from typing import Any

import duckdb
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from benchmark.csv_io import write_csv_atomically

_ALLOWED_TABLES = {
    "academic_records",
    "financial_aid_records",
    "financial_aid_late_arrivals",
    "identity_crosswalk",
    "aid_status_crosswalk",
    "financial_aid_publication_events",
    "benchmark_temporal_snapshots",
}
_EXPECTED_COLUMNS = ["student_id"]
_MAX_RESULT_ROWS = 100_000
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_MAX_RESULT_VALUE_BYTES = 1024 * 1024
_EXPANDING_FUNCTIONS = {
    "array_agg",
    "arrayagg",
    "generate_series",
    "generateseries",
    "group_concat",
    "list",
    "list_agg",
    "listagg",
    "lpad",
    "map_from_entries",
    "range",
    "repeat",
    "rpad",
    "space",
    "string_agg",
    "transform",
    "unnest",
}
_QUERY_TIMEOUT_SECONDS = 30.0
_DUCKDB_MEMORY_LIMIT = "512MB"
_DUCKDB_THREADS = 2
_ALLOWED_ANONYMOUS_FUNCTIONS: set[str] = set()
_CANONICAL_AID_STATUSES = {"active", "suspended", "none"}
_CSV_SCHEMAS = {
    "academic_records": (
        ("student_id", "VARCHAR"),
        ("gpa", "DOUBLE"),
        ("enrollment_status", "VARCHAR"),
        ("semester", "VARCHAR"),
    ),
    "financial_aid_records": (
        ("student_id", "VARCHAR"),
        ("aid_amount", "DOUBLE"),
        ("aid_status", "VARCHAR"),
        ("disbursement_date", "DATE"),
    ),
    "financial_aid_late_arrivals": (
        ("student_id", "VARCHAR"),
        ("aid_amount", "DOUBLE"),
        ("aid_status", "VARCHAR"),
        ("disbursement_date", "DATE"),
    ),
    "identity_crosswalk": (
        ("canonical_student_id", "VARCHAR"),
        ("financial_aid_student_id", "VARCHAR"),
    ),
    "aid_status_crosswalk": (
        ("financial_aid_status", "VARCHAR"),
        ("canonical_aid_status", "VARCHAR"),
    ),
    "financial_aid_publication_events": (
        ("event_id", "VARCHAR"),
        ("financial_aid_student_id", "VARCHAR"),
        ("event_time", "DATE"),
        ("observed_at", "TIMESTAMPTZ"),
        ("published_at", "TIMESTAMPTZ"),
        ("arrival_stream", "VARCHAR"),
    ),
}
_MANIFEST_V1_DATASETS = (
    "academic_records",
    "financial_aid_records",
    "identity_crosswalk",
)
_MANIFEST_V1_FIELDS = {
    "manifest_version",
    "variant",
    "baseline_dataset_id",
    "baseline_file_hashes",
    "variant_file_hashes",
    "random_seed",
    "corruption_percentages",
    "selected_row_ids",
    "fragmentation_score",
    "invariants",
}
_MANIFEST_V1_OPERATORS = {
    "drop_row",
    "null_aid_amount",
    "null_aid_status",
    "identifier_mismatch",
}
_MANIFEST_V2_DATASETS = (
    "academic_records",
    "financial_aid_records",
    "financial_aid_late_arrivals",
    "identity_crosswalk",
    "aid_status_crosswalk",
)
_MANIFEST_V3_DATASETS = (
    *_MANIFEST_V2_DATASETS,
    "financial_aid_publication_events",
)
_MANIFEST_V3_FIELDS = {
    "manifest_version",
    "variant",
    "baseline_dataset_id",
    "baseline_file_hashes",
    "variant_file_hashes",
    "random_seed",
    "corruption_percentages",
    "selected_row_ids",
    "fragmentation_score",
    "invariants",
    "temporal",
}
_MANIFEST_V3_OPERATORS = {
    "aid_status_code_drift",
    "drop_row",
    "identifier_mismatch",
    "null_aid_amount",
    "null_aid_status",
    "publication_delay",
}
_MANIFEST_V3_INVARIANTS = {
    "mutate_academic_records": False,
    "regenerate_population_per_variant": False,
    "corruption_applies_only_to": "financial_aid_domain",
}
_TEMPORAL_FIELDS = {
    "contract_version",
    "timezone",
    "logical_time",
    "snapshots",
    "current_record_count",
    "late_record_count",
}
_SNAPSHOT_FIELDS = {"published_at", "event_time_watermark"}
_VOLATILE_FUNCTIONS = {
    "clock_timestamp",
    "current_date",
    "current_time",
    "current_timestamp",
    "gen_random_uuid",
    "localtime",
    "localtimestamp",
    "now",
    "rand",
    "random",
    "statement_timestamp",
    "transaction_timestamp",
    "uuid",
}


class SqlValidationError(ValueError):
    """SQL 超出只读基准沙箱。"""


def validate_read_only_sql(sql: str) -> str:
    normalized = sql.strip()
    if not normalized:
        raise SqlValidationError("SQL must not be empty")
    try:
        statements = parse(normalized, read="duckdb")
    except ParseError as error:
        raise SqlValidationError(f"Invalid DuckDB SQL: {error}") from error
    if len(statements) != 1:
        raise SqlValidationError("Exactly one SQL statement is allowed")

    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise SqlValidationError("Only read-only query statements are allowed")

    cte_names = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    if any(
        with_clause.args.get("recursive")
        for with_clause in statement.find_all(exp.With)
    ):
        raise SqlValidationError(
            "Recursive CTEs are not allowed by the benchmark resource limits"
        )
    for join in statement.find_all(exp.Join):
        kind = str(join.args.get("kind") or "").casefold()
        if kind == "cross" or not _has_bounded_join_condition(join):
            # 仅写 ON TRUE 仍是笛卡尔积；连接必须显式约束两个不同来源的列。
            raise SqlValidationError(
                "Cartesian joins are not allowed by the benchmark resource limits"
            )

    allowed_names = _ALLOWED_TABLES | cte_names
    for table in statement.find_all(exp.Table):
        # 表函数可能绕过已注册视图读取任意文件；AST 中其 this 不是普通标识符。
        if not isinstance(table.this, exp.Identifier):
            raise SqlValidationError("Table functions are not allowed in benchmark SQL")
        if table.catalog or table.db:
            raise SqlValidationError(
                "Qualified table names are not allowed in benchmark SQL"
            )
        if table.name.lower() not in allowed_names:
            raise SqlValidationError(
                f"Table is not available in benchmark SQL: {table.name}"
            )
    for function in statement.find_all(exp.Func):
        function_name = (
            function.name
            if isinstance(function, exp.Anonymous)
            else function.sql_name()
        ).casefold()
        if function_name in _VOLATILE_FUNCTIONS:
            # 同一 SQL 会在校验和落盘阶段执行多次，因此禁止依赖时间或随机数。
            raise SqlValidationError(
                "Benchmark SQL must be deterministic; volatile functions are not allowed"
            )
        if function_name in _EXPANDING_FUNCTIONS:
            raise SqlValidationError(
                "SQL collection or value expansion is not allowed by the "
                "benchmark resource limits"
            )
        if (
            isinstance(function, exp.Anonymous)
            and function_name not in _ALLOWED_ANONYMOUS_FUNCTIONS
        ):
            # SQLGlot 无法分类的函数默认拒绝，避免 DuckDB 扩展函数产生等待或副作用。
            raise SqlValidationError(
                f"SQL function is not allowed in benchmark SQL: {function_name}"
            )
    return normalized


def _has_bounded_join_condition(join: exp.Join) -> bool:
    if join.args.get("using"):
        return True
    condition = join.args.get("on")
    if condition is None:
        return False

    # 只接受正向 AND 分支上的裸列等值；NOT/IS/CASE 内嵌的等值并不收窄连接。
    right_source = join.this.alias_or_name.casefold()
    for equality in _positive_join_conjuncts(condition):
        if not isinstance(equality, exp.EQ):
            continue
        left = equality.this
        right = equality.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_source = left.table.casefold()
        right_column_source = right.table.casefold()
        if (
            right_source in {left_source, right_column_source}
            and left_source
            and right_column_source
            and left_source != right_column_source
        ):
            return True
    return False


def _positive_join_conjuncts(condition: exp.Expression) -> list[exp.Expression]:
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return [
            *_positive_join_conjuncts(condition.this),
            *_positive_join_conjuncts(condition.expression),
        ]
    return [condition]


def extract_sql(text: str) -> str:
    stripped = text.strip()
    fence = re.search(
        r"```(?:sql)?\s*(.*?)```",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fence:
        stripped = fence.group(1).strip()

    starts = [
        match.start() for match in re.finditer(r"(?im)^(?:with|select)\b", stripped)
    ]
    if not starts:
        for keyword in ("with", "select"):
            match = re.search(rf"\b{keyword}\b", stripped, flags=re.IGNORECASE)
            if match:
                starts.append(match.start())
    if not starts:
        raise SqlValidationError("Model output did not contain a SELECT statement")

    sql = normalize_generated_sql(stripped[min(starts) :].strip().rstrip(";"))
    if not sql.endswith(";"):
        sql += ";"
    return validate_read_only_sql(sql)


def normalize_generated_sql(sql: str) -> str:
    normalized = sql.strip()
    if normalized.lower().startswith("with "):
        return normalized
    inferred_cte = infer_missing_initial_cte_name(normalized)
    if inferred_cte is not None:
        return f"WITH {inferred_cte} AS (\n{normalized}"
    return normalized


def infer_missing_initial_cte_name(sql: str) -> str | None:
    if (
        re.search(
            r"\)\s*,\s*[A-Za-z_][A-Za-z0-9_]*\s+AS\s*\(",
            sql,
            flags=re.IGNORECASE,
        )
        is None
    ):
        return None
    defined_ctes = {
        match.group(1).lower()
        for match in re.finditer(
            r"[,]\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(",
            sql,
            flags=re.IGNORECASE,
        )
    }
    referenced_names = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            sql,
            flags=re.IGNORECASE,
        )
    }
    candidates = sorted(
        name
        for name in referenced_names
        if name.lower() not in _ALLOWED_TABLES | defined_ctes
    )
    return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True)
class CohortFingerprint:
    """可跨变体比较的生成批次身份。"""

    baseline_dataset_id: str
    random_seed: int
    baseline_file_hashes: tuple[tuple[str, str], ...]
    temporal_snapshot_identity: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class _VariantContract:
    manifest_version: int
    csv_paths: dict[str, Path | None]
    temporal_snapshots: tuple[tuple[str, datetime, datetime], ...]
    temporal_record_counts: tuple[int, int] | None
    manifest_hashes: dict[str, str]
    cohort_fingerprint: CohortFingerprint | None
    manifest: dict[str, Any]


def _load_variant_contract(variant_dir: Path) -> _VariantContract:
    csv_paths: dict[str, Path | None] = {
        name: variant_dir / f"{name}.csv" for name in _CSV_SCHEMAS
    }
    for dataset_name in ("academic_records", "financial_aid_records"):
        path = csv_paths[dataset_name]
        if path is None or not path.is_file():
            raise FileNotFoundError(f"Missing benchmark CSV: {path}")

    manifest_path = _find_variant_manifest(variant_dir)
    manifest = _read_manifest(manifest_path) if manifest_path is not None else None
    if manifest is None:
        modern_sidecars = (
            "financial_aid_late_arrivals",
            "aid_status_crosswalk",
            "financial_aid_publication_events",
        )
        unexpected = [name for name in modern_sidecars if csv_paths[name].exists()]
        if unexpected:
            raise FileNotFoundError(
                "Versioned benchmark sidecars require a verifiable manifest: "
                + ", ".join(unexpected)
            )

    manifest_version = _manifest_version(manifest)
    if manifest_version == 1 and manifest is not None and manifest_path is not None:
        _validate_historical_v1_manifest(manifest, manifest_path, variant_dir)
    if manifest_version == 2 and manifest is not None:
        _validate_v2_not_downgraded(manifest, variant_dir)

    manifest_hashes: dict[str, str] = {}
    cohort_fingerprint: CohortFingerprint | None = None
    if manifest_version >= 1:
        if manifest is None:
            raise AssertionError("versioned manifest must be loaded")
        canonical_variant = variant_dir.resolve().name
        if manifest.get("variant") != canonical_variant:
            raise ValueError(
                "Manifest variant does not match variant directory: "
                f"{manifest.get('variant')!r} != {canonical_variant!r}"
            )
        hashes = manifest.get("variant_file_hashes")
        if not isinstance(hashes, dict):
            raise TypeError("Manifest variant_file_hashes must be an object")
        if manifest_version == 1:
            required_datasets = _MANIFEST_V1_DATASETS
        elif manifest_version == 2:
            required_datasets = _MANIFEST_V2_DATASETS
        else:
            required_datasets = _MANIFEST_V3_DATASETS
        for dataset_name in required_datasets:
            path = csv_paths[dataset_name]
            if path is None or not path.is_file():
                raise FileNotFoundError(f"Missing benchmark CSV: {path}")
            _verify_manifest_hash(path, dataset_name, hashes)
            manifest_hashes[dataset_name] = str(hashes[dataset_name]).casefold()
        if manifest_version == 3:
            _validate_v3_manifest(manifest)
            if canonical_variant == "baseline":
                _validate_v3_baseline_manifest(manifest, manifest_hashes)
        temporal_snapshots = (
            _parse_temporal_snapshots(manifest) if manifest_version == 3 else ()
        )
        cohort_fingerprint = _parse_cohort_fingerprint(
            manifest,
            required_datasets,
            temporal_snapshots=temporal_snapshots,
        )
        if canonical_variant == "baseline":
            verified_baseline_hashes = tuple(
                (dataset_name, manifest_hashes[dataset_name])
                for dataset_name in required_datasets
            )
            # baseline 是 cohort 真值锚点，声明的 hashes 必须等于刚验真的文件。
            if cohort_fingerprint.baseline_file_hashes != verified_baseline_hashes:
                raise ValueError(
                    "Manifest baseline_file_hashes do not match verified baseline files"
                )
    else:
        temporal_snapshots = ()
        legacy_hashes = {
            dataset_name: _sha256_file(csv_paths[dataset_name])
            for dataset_name in ("academic_records", "financial_aid_records")
        }
        manifest_hashes.update(legacy_hashes)
        academic_hash = legacy_hashes["academic_records"]
        identity = hashlib.sha256()
        identity.update(b"manifestless-academic-cohort\0")
        identity.update(academic_hash.encode())
        # 旧数据没有 seed/manifest；以真实学业快照作为可验证的最小 cohort 锚点。
        cohort_fingerprint = CohortFingerprint(
            baseline_dataset_id=identity.hexdigest(),
            random_seed=0,
            baseline_file_hashes=(("academic_records", academic_hash),),
        )
    for dataset_name, path in csv_paths.items():
        if path is not None and not path.is_file():
            csv_paths[dataset_name] = None

    temporal_record_counts = (
        _parse_temporal_record_counts(manifest) if manifest_version == 3 else None
    )
    return _VariantContract(
        manifest_version=manifest_version,
        csv_paths=csv_paths,
        temporal_snapshots=temporal_snapshots,
        temporal_record_counts=temporal_record_counts,
        manifest_hashes=manifest_hashes,
        cohort_fingerprint=cohort_fingerprint,
        manifest=manifest or {},
    )


def _validate_historical_v1_manifest(
    manifest: dict[str, Any],
    manifest_path: Path,
    variant_dir: Path,
) -> None:
    def reject(message: str) -> None:
        raise ValueError(f"Manifest downgrade detected: {message}")

    if set(manifest) != _MANIFEST_V1_FIELDS:
        reject("version 1 does not match the historical field contract")

    # v1 从未发布这些 sidecar；单改版本号不能让 v3 数据静默走 fallback。
    modern_sidecars = (
        "financial_aid_late_arrivals",
        "aid_status_crosswalk",
        "financial_aid_publication_events",
    )
    for dataset_name in modern_sidecars:
        path = variant_dir / f"{dataset_name}.csv"
        if path.exists() or path.is_symlink():
            reject(f"version 1 contains modern sidecar {path.name}")

    for field in ("baseline_file_hashes", "variant_file_hashes"):
        hashes = manifest.get(field)
        if not isinstance(hashes, dict) or set(hashes) != set(_MANIFEST_V1_DATASETS):
            reject(f"version 1 {field} does not match historical datasets")

    for field in ("corruption_percentages", "selected_row_ids"):
        values = manifest.get(field)
        if not isinstance(values, dict) or set(values) != _MANIFEST_V1_OPERATORS:
            reject(f"version 1 {field} does not match historical operators")

    for operator, value in manifest["corruption_percentages"].items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            reject(f"version 1 corruption rate is invalid for {operator}")
    for operator, values in manifest["selected_row_ids"].items():
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or len(values) != len(set(values))
        ):
            reject(f"version 1 selected_row_ids is invalid for {operator}")

    score = manifest.get("fragmentation_score")
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
    ):
        reject("version 1 fragmentation_score is invalid")

    expected_invariants = {
        "mutate_academic_records": False,
        "regenerate_population_per_variant": False,
        "corruption_applies_only_to": "financial_aid_records",
    }
    if manifest.get("invariants") != expected_invariants:
        reject("version 1 invariants do not match the historical contract")

    # 同一 run 已出现新版 manifest 时，单文件不能伪装成 v1 绕过校验。
    for sibling in manifest_path.parent.glob("*_manifest.json"):
        if sibling == manifest_path:
            continue
        sibling_manifest = _read_manifest(sibling)
        if _manifest_version(sibling_manifest) >= 2:
            reject(f"version 1 is mixed with versioned sibling {sibling.name}")


def _validate_v2_not_downgraded(
    manifest: dict[str, Any],
    variant_dir: Path,
) -> None:
    v3_dataset = "financial_aid_publication_events"
    modern_evidence = [
        "temporal" in manifest,
        (variant_dir / f"{v3_dataset}.csv").exists(),
    ]
    for field in ("baseline_file_hashes", "variant_file_hashes"):
        hashes = manifest.get(field)
        modern_evidence.append(isinstance(hashes, dict) and v3_dataset in hashes)
    for field in ("corruption_percentages", "selected_row_ids"):
        operators = manifest.get(field)
        modern_evidence.append(
            isinstance(operators, dict) and "publication_delay" in operators
        )
    if any(modern_evidence):
        raise ValueError(
            "Manifest downgrade detected: version 2 contains version 3 temporal data"
        )


def _validate_v3_manifest(manifest: dict[str, Any]) -> None:
    _require_exact_fields(manifest, _MANIFEST_V3_FIELDS, "Manifest version 3")
    for field in ("baseline_file_hashes", "variant_file_hashes"):
        hashes = _required_object(manifest[field], f"Manifest {field}")
        _require_exact_fields(
            hashes,
            set(_MANIFEST_V3_DATASETS),
            f"Manifest {field}",
        )

    rates = _required_object(
        manifest["corruption_percentages"],
        "Manifest corruption_percentages",
    )
    _require_exact_fields(
        rates,
        _MANIFEST_V3_OPERATORS,
        "Manifest corruption_percentages",
    )
    for operator, value in rates.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            raise ValueError(
                f"Manifest corruption percentage is invalid for {operator}"
            )

    selected = _required_object(
        manifest["selected_row_ids"],
        "Manifest selected_row_ids",
    )
    _require_exact_fields(
        selected,
        _MANIFEST_V3_OPERATORS,
        "Manifest selected_row_ids",
    )
    for operator, identifiers in selected.items():
        if (
            not isinstance(identifiers, list)
            or any(
                not isinstance(identifier, str) or not identifier.strip()
                for identifier in identifiers
            )
            or len(identifiers) != len(set(identifiers))
        ):
            raise ValueError(f"Manifest selected_row_ids is invalid for {operator}")

    score = manifest["fragmentation_score"]
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
    ):
        raise ValueError("Manifest fragmentation_score must be within 0..1")
    if manifest["invariants"] != _MANIFEST_V3_INVARIANTS:
        raise ValueError("Manifest version 3 invariants are invalid")


def _validate_v3_baseline_manifest(
    manifest: dict[str, Any],
    verified_hashes: dict[str, str],
) -> None:
    rates = manifest["corruption_percentages"]
    selected = manifest["selected_row_ids"]
    score = float(manifest["fragmentation_score"])
    if (
        any(float(value) != 0.0 for value in rates.values())
        or any(identifiers for identifiers in selected.values())
        or not math.isclose(score, 1.0, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise ValueError("Manifest baseline must represent uncorrupted data")

    seed = manifest["random_seed"]
    _non_negative_int(seed, "Manifest random_seed")
    identity = hashlib.sha256()
    identity.update(str(seed).encode())
    # Rust 的业务数据身份有意排除 publication events 的逻辑发布时间。
    for dataset_name in _MANIFEST_V2_DATASETS:
        identity.update(verified_hashes[dataset_name].encode())
    expected_dataset_id = identity.hexdigest()
    if str(manifest["baseline_dataset_id"]).casefold() != expected_dataset_id:
        raise ValueError(
            "Manifest baseline_dataset_id does not match verified baseline files"
        )


def _parse_cohort_fingerprint(
    manifest: dict[str, Any],
    required_datasets: tuple[str, ...],
    *,
    temporal_snapshots: tuple[tuple[str, datetime, datetime], ...] = (),
) -> CohortFingerprint:
    dataset_id = manifest.get("baseline_dataset_id")
    if (
        not isinstance(dataset_id, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", dataset_id) is None
    ):
        raise ValueError(
            "Manifest baseline_dataset_id must be a 64-character hex string"
        )
    random_seed = manifest.get("random_seed")
    _non_negative_int(random_seed, "Manifest random_seed")

    raw_hashes = manifest.get("baseline_file_hashes")
    if not isinstance(raw_hashes, dict):
        raise TypeError("Manifest baseline_file_hashes must be an object")
    normalized_hashes: list[tuple[str, str]] = []
    for dataset_name in required_datasets:
        digest = raw_hashes.get(dataset_name)
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
        ):
            raise ValueError(
                "Manifest baseline_file_hashes must define a 64-character "
                f"hex digest for {dataset_name}"
            )
        normalized_hashes.append((dataset_name, digest.casefold()))
    return CohortFingerprint(
        baseline_dataset_id=dataset_id.casefold(),
        random_seed=random_seed,
        baseline_file_hashes=tuple(normalized_hashes),
        temporal_snapshot_identity=tuple(
            (
                snapshot,
                published_at.isoformat(),
                event_time_watermark.isoformat(),
            )
            for snapshot, published_at, event_time_watermark in temporal_snapshots
        ),
    )


def require_matching_cohorts(
    labeled_fingerprints: list[tuple[str, CohortFingerprint | None]],
) -> None:
    """多目标必须全为 legacy，或属于同一个可验证生成批次。"""
    if len(labeled_fingerprints) < 2:
        return
    legacy_labels = [
        label for label, fingerprint in labeled_fingerprints if fingerprint is None
    ]
    if legacy_labels and len(legacy_labels) != len(labeled_fingerprints):
        raise ValueError(
            "Cannot mix legacy and versioned benchmark cohorts: "
            + ", ".join(label for label, _ in labeled_fingerprints)
        )
    if legacy_labels:
        return
    expected_label, expected = labeled_fingerprints[0]
    mismatched = [
        label
        for label, fingerprint in labeled_fingerprints[1:]
        if fingerprint != expected
    ]
    if mismatched:
        raise ValueError(
            "Benchmark targets belong to different cohorts: "
            f"{expected_label} differs from {', '.join(mismatched)}"
        )


def _find_variant_manifest(variant_dir: Path) -> Path | None:
    canonical_dir = variant_dir.resolve()
    if canonical_dir.parent.name != "variants":
        return None
    manifest_path = (
        canonical_dir.parent.parent
        / "manifests"
        / f"{canonical_dir.name}_manifest.json"
    )
    if manifest_path.is_file():
        return manifest_path
    manifests_dir = manifest_path.parent
    if manifests_dir.is_dir() and any(manifests_dir.glob("*_manifest.json")):
        raise FileNotFoundError(
            f"Missing benchmark manifest for variant: {manifest_path}"
        )
    return None


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid benchmark manifest JSON: {path}") from error
    if not isinstance(payload, dict):
        raise TypeError(f"Benchmark manifest must be an object: {path}")
    return payload


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"Benchmark manifest contains duplicate key: {key}")
        payload[key] = value
    return payload


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"Benchmark manifest contains non-finite number: {value}")


def _manifest_version(manifest: dict[str, Any] | None) -> int:
    if manifest is None:
        return 0
    version = manifest.get("manifest_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("Manifest manifest_version must be an integer")
    if version not in {1, 2, 3}:
        raise ValueError(f"Unsupported manifest version: {version}")
    return version


def _verify_manifest_hash(
    path: Path,
    dataset_name: str,
    hashes: dict[str, Any],
) -> None:
    expected = hashes.get(dataset_name)
    if (
        not isinstance(expected, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None
    ):
        raise ValueError(
            f"Manifest SHA-256 for {dataset_name} must be a 64-character hex string"
        )
    actual = _sha256_file(path)
    if actual != expected.casefold():
        raise ValueError(
            f"SHA-256 mismatch for {dataset_name}: expected {expected}, got {actual}"
        )


def _reverify_materialized_contract(contract: _VariantContract) -> None:
    for dataset_name, expected in contract.manifest_hashes.items():
        path = contract.csv_paths[dataset_name]
        if path is None:
            raise FileNotFoundError(
                f"Missing benchmark CSV during materialization: {dataset_name}"
            )
        actual = _sha256_file(path)
        if actual != expected:
            raise ValueError(
                f"SHA-256 changed during materialization for {dataset_name}: "
                f"expected {expected}, got {actual}"
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_temporal_snapshots(
    manifest: dict[str, Any],
) -> tuple[tuple[str, datetime, datetime], ...]:
    if "temporal" not in manifest:
        raise ValueError("Manifest version 3 must define temporal")
    temporal = _required_object(manifest["temporal"], "temporal")
    _require_exact_fields(temporal, _TEMPORAL_FIELDS, "temporal")
    if temporal["contract_version"] != 1:
        raise ValueError("temporal.contract_version must be 1")
    if temporal["timezone"] != "UTC":
        raise ValueError("temporal.timezone must be UTC")
    if temporal["logical_time"] is not True:
        raise ValueError("temporal.logical_time must be true")
    _non_negative_int(temporal["current_record_count"], "temporal.current_record_count")
    _non_negative_int(temporal["late_record_count"], "temporal.late_record_count")

    snapshots = _required_object(temporal["snapshots"], "temporal.snapshots")
    _require_exact_fields(snapshots, {"current", "replayed"}, "temporal.snapshots")
    current = _parse_temporal_snapshot(snapshots["current"], "current")
    replayed = _parse_temporal_snapshot(snapshots["replayed"], "replayed")
    if replayed[0] < current[0]:
        raise ValueError(
            "temporal replayed published_at must not be earlier than current"
        )
    if replayed[1] < current[1]:
        raise ValueError("temporal replayed event_time_watermark must not regress")
    return (
        ("current", current[0], current[1]),
        ("replayed", replayed[0], replayed[1]),
    )


def _parse_temporal_record_counts(
    manifest: dict[str, Any],
) -> tuple[int, int]:
    temporal = _required_object(manifest["temporal"], "temporal")
    current_count = temporal["current_record_count"]
    late_count = temporal["late_record_count"]
    _non_negative_int(current_count, "temporal.current_record_count")
    _non_negative_int(late_count, "temporal.late_record_count")
    return current_count, late_count


def _parse_temporal_snapshot(
    payload: Any,
    snapshot: str,
) -> tuple[datetime, datetime]:
    location = f"temporal.snapshots.{snapshot}"
    fields = _required_object(payload, location)
    _require_exact_fields(fields, _SNAPSHOT_FIELDS, location)
    published_at = _parse_utc_timestamp(
        fields["published_at"], f"{location}.published_at"
    )
    watermark = _parse_utc_timestamp(
        fields["event_time_watermark"],
        f"{location}.event_time_watermark",
    )
    if watermark > published_at:
        raise ValueError(f"{location} watermark must not be later than published_at")
    return published_at, watermark


def _parse_utc_timestamp(value: Any, location: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{location} must be an RFC3339 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{location} must be a valid RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{location} must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def _required_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{location} must be an object")
    return value


def _require_exact_fields(
    payload: dict[str, Any],
    expected: set[str],
    location: str,
) -> None:
    missing = sorted(expected - set(payload))
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")
    unknown = sorted(set(payload) - expected)
    if unknown:
        raise ValueError(f"{location} contains unknown fields: {', '.join(unknown)}")


def _non_negative_int(value: Any, location: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{location} must be an integer")
    if value < 0:
        raise ValueError(f"{location} must not be negative")


def _is_nested_result_type(result_type: str) -> bool:
    return (
        result_type.endswith("[]")
        or any(
            marker in result_type
            for marker in ("ARRAY(", "LIST(", "MAP(", "STRUCT(", "UNION(")
        )
        or result_type in {"BLOB", "JSON"}
    )


def _validate_result_size(rows: list[tuple[Any, ...]]) -> None:
    total_bytes = 0
    for row in rows:
        for value in row:
            if value is None:
                continue
            if isinstance(
                value, (list, tuple, dict, set, bytes, bytearray, memoryview)
            ):
                raise TypeError("SQL result contains a collection or binary value")
            value_bytes = len(str(value).encode("utf-8"))
            if value_bytes > _MAX_RESULT_VALUE_BYTES:
                raise ValueError(
                    "SQL result contains a value exceeding the resource limit"
                )
            total_bytes += value_bytes
            if total_bytes > _MAX_RESULT_BYTES:
                raise ValueError("SQL result exceeds the total byte resource limit")


class VariantSqlRuntime:
    """在同一变体上复用 DuckDB 连接和固定数据快照。"""

    def __init__(self, variant_dir: Path | str) -> None:
        self.variant_dir = Path(variant_dir)
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._manifest: dict[str, Any] = {}
        self._cohort_fingerprint: CohortFingerprint | None = None

    @property
    def manifest(self) -> dict[str, Any]:
        """返回与内存表同次加载的 manifest 防御性副本。"""
        return copy.deepcopy(self._manifest)

    @property
    def cohort_fingerprint(self) -> CohortFingerprint | None:
        """返回不可变的生成批次身份；runtime 未打开时为 ``None``。"""
        return self._cohort_fingerprint

    def __enter__(self) -> VariantSqlRuntime:  # noqa: PYI034  # Python 3.10 无 Self。
        if self._connection is not None:
            raise RuntimeError("Variant SQL runtime is already open")
        contract = _load_variant_contract(self.variant_dir)

        connection = duckdb.connect(database=":memory:")
        try:
            register_csv_views(
                connection,
                self.variant_dir / "academic_records.csv",
                self.variant_dir / "financial_aid_records.csv",
                contract.csv_paths["identity_crosswalk"],
                late_aid_csv=contract.csv_paths["financial_aid_late_arrivals"],
                status_crosswalk_csv=contract.csv_paths["aid_status_crosswalk"],
                publication_events_csv=contract.csv_paths[
                    "financial_aid_publication_events"
                ],
                temporal_snapshots=contract.temporal_snapshots,
                temporal_record_counts=contract.temporal_record_counts,
                strict_governance=contract.manifest_version >= 1,
            )
            if contract.manifest_version == 3:
                _validate_fragmentation_score(connection, contract.manifest)
                _validate_selected_row_ids(connection, contract.manifest)
            _reverify_materialized_contract(contract)
        except Exception:
            connection.close()
            raise
        self._manifest = copy.deepcopy(contract.manifest)
        self._cohort_fingerprint = contract.cohort_fingerprint
        self._connection = connection
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._cohort_fingerprint = None
        if connection is not None:
            connection.close()

    def execute(
        self,
        sql: str,
        output_csv: Path | str | None = None,
        *,
        required_columns: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        connection = self._connection
        if connection is None:
            raise RuntimeError("Variant SQL runtime is closed")
        validated_sql = validate_read_only_sql(sql).rstrip(";")

        timer_lock = Lock()
        query_finished = False
        deadline_reached = False

        def interrupt_on_timeout() -> None:
            nonlocal deadline_reached
            # 与 finally 共用锁，防止已取消计时器的迟到回调打断下一条查询。
            with timer_lock:
                if query_finished:
                    return
                deadline_reached = True
                connection.interrupt()

        timer = Timer(_QUERY_TIMEOUT_SECONDS, interrupt_on_timeout)
        timer.daemon = True
        timer.start()
        query_error: duckdb.Error | None = None
        result: list[tuple[Any, ...]] = []
        try:
            cursor = connection.execute(validated_sql)
            result_types = [
                str(description[1]).upper() for description in cursor.description
            ]
            if any(_is_nested_result_type(value) for value in result_types):
                raise ValueError(
                    "SQL result contains a collection or binary output type"
                )
            columns = [description[0] for description in cursor.description]
            normalized_columns = [column.casefold() for column in columns]
            if len(normalized_columns) != len(set(normalized_columns)):
                raise ValueError(
                    "SQL result contains duplicate column names; use unique aliases"
                )
            missing_columns = [
                column for column in required_columns if column not in columns
            ]
            if missing_columns:
                raise ValueError(
                    "SQL result is missing required columns: "
                    + ", ".join(missing_columns)
                )
            result = cursor.fetchmany(_MAX_RESULT_ROWS + 1)
        except duckdb.Error as error:
            query_error = error
        finally:
            with timer_lock:
                query_finished = True
                query_timed_out = deadline_reached
            timer.cancel()

        if query_timed_out:
            raise TimeoutError(
                f"SQL query exceeded {_QUERY_TIMEOUT_SECONDS:g} seconds"
            ) from query_error
        if query_error is not None:
            raise query_error
        if len(result) > _MAX_RESULT_ROWS:
            raise ValueError(f"SQL result exceeds maximum of {_MAX_RESULT_ROWS} rows")
        _validate_result_size(result)
        rows = [dict(zip(columns, row)) for row in result]
        if output_csv is not None:
            write_rows(Path(output_csv), rows, columns)
        return rows

    def materialized_rows(self, table_name: str) -> list[dict[str, Any]]:
        """读取已冻结的受信表；不接受模型 SQL，也不受候选结果行数上限影响。"""
        connection = self._connection
        if connection is None:
            raise RuntimeError("Variant SQL runtime is closed")
        if table_name not in _CSV_SCHEMAS:
            raise ValueError(f"Unknown materialized benchmark table: {table_name}")
        columns = [column for column, _data_type in _CSV_SCHEMAS[table_name]]
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        # table_name 只可能来自上面的内部白名单，不能注入任意标识符。
        result = connection.execute(
            f"SELECT {quoted_columns} FROM {table_name}"
        ).fetchall()
        return [dict(zip(columns, row)) for row in result]


def validate_variant_cohorts(
    labeled_variant_dirs: list[tuple[str, Path | str]],
) -> None:
    """只加载受控快照并验证批次，不执行分析查询。"""
    fingerprints: list[tuple[str, CohortFingerprint | None]] = []
    for label, variant_dir in labeled_variant_dirs:
        with VariantSqlRuntime(variant_dir) as runtime:
            fingerprints.append((label, runtime.cohort_fingerprint))
    require_matching_cohorts(fingerprints)


def run_sql_on_variant(
    variant_dir: Path | str,
    sql: str,
    output_csv: Path | str | None = None,
    *,
    required_columns: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    with VariantSqlRuntime(variant_dir) as runtime:
        return runtime.execute(
            sql,
            output_csv=output_csv,
            required_columns=required_columns,
        )


def register_csv_views(
    connection: duckdb.DuckDBPyConnection,
    academic_csv: Path,
    aid_csv: Path,
    identity_crosswalk_csv: Path | None = None,
    *,
    late_aid_csv: Path | None = None,
    status_crosswalk_csv: Path | None = None,
    publication_events_csv: Path | None = None,
    temporal_snapshots: tuple[tuple[str, datetime, datetime], ...] = (),
    temporal_record_counts: tuple[int, int] | None = None,
    strict_governance: bool = False,
) -> None:
    # 第四个位置参数历史上是 identity crosswalk；新增 sidecar 一律仅限关键字。
    csv_paths = {
        "academic_records": Path(academic_csv),
        "financial_aid_records": Path(aid_csv),
        "financial_aid_late_arrivals": (
            Path(late_aid_csv) if late_aid_csv is not None else None
        ),
        "identity_crosswalk": (
            Path(identity_crosswalk_csv) if identity_crosswalk_csv is not None else None
        ),
        "aid_status_crosswalk": (
            Path(status_crosswalk_csv) if status_crosswalk_csv is not None else None
        ),
        "financial_aid_publication_events": (
            Path(publication_events_csv) if publication_events_csv is not None else None
        ),
    }

    # 先检查每个表头，避免加载一部分表后才发现列顺序漂移。
    for table_name, path in csv_paths.items():
        if path is not None:
            _validate_csv_header(path, table_name)

    connection.execute(f"SET memory_limit = '{_DUCKDB_MEMORY_LIMIT}'")
    connection.execute(f"SET threads = {_DUCKDB_THREADS}")

    _materialize_csv_table(
        connection, "academic_records", csv_paths["academic_records"]
    )
    _materialize_csv_table(
        connection, "financial_aid_records", csv_paths["financial_aid_records"]
    )
    _materialize_optional_table(
        connection,
        "financial_aid_late_arrivals",
        csv_paths["financial_aid_late_arrivals"],
    )

    identity_path = csv_paths["identity_crosswalk"]
    if identity_path is None:
        # legacy/v1 没有交叉表时，只能提供规范 ID 的固定恒等映射。
        connection.execute(
            """
            CREATE TEMP TABLE identity_crosswalk AS
            SELECT student_id AS canonical_student_id,
                   student_id AS financial_aid_student_id
            FROM academic_records
            """
        )
    else:
        _materialize_csv_table(connection, "identity_crosswalk", identity_path)

    status_path = csv_paths["aid_status_crosswalk"]
    if status_path is None:
        # legacy/v1 兼容路径仍显式物化规范状态，不依赖外部文件。
        connection.execute(
            """
            CREATE TEMP TABLE aid_status_crosswalk AS
            SELECT *
            FROM (VALUES
                ('active', 'active'),
                ('suspended', 'suspended'),
                ('none', 'none')
            ) AS mapping(financial_aid_status, canonical_aid_status)
            """
        )
    else:
        _materialize_csv_table(connection, "aid_status_crosswalk", status_path)

    _materialize_optional_table(
        connection,
        "financial_aid_publication_events",
        csv_paths["financial_aid_publication_events"],
    )
    _materialize_temporal_snapshots(connection, temporal_snapshots)

    # 外部 I/O 仅在导入阶段开启；锁配置后 SQL 无法重新启用或扩大资源。
    connection.execute("SET enable_external_access = false")
    connection.execute("SET lock_configuration = true")

    _require_unique_source_key(connection, "academic_records", "student_id")
    _validate_mapping_contract(connection, strict_governance=strict_governance)
    _validate_publication_event_contract(connection)
    if temporal_record_counts is not None:
        _validate_strict_aid_business_keys(connection)
        _validate_temporal_record_counts(connection, temporal_record_counts)
        _validate_temporal_publication_contract(connection)


def _validate_csv_header(path: Path, table_name: str) -> None:
    expected = [column for column, _data_type in _CSV_SCHEMAS[table_name]]
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle, strict=True), None)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Missing benchmark CSV: {path}") from error
    if header != expected:
        raise ValueError(f"{path.name} header must be exactly {expected}; got {header}")


def _materialize_csv_table(
    connection: duckdb.DuckDBPyConnection,
    table_name: str,
    csv_path: Path | None,
) -> None:
    if csv_path is None:
        raise AssertionError(f"{table_name} requires a CSV path")
    columns = ", ".join(
        f"'{column}': '{data_type}'" for column, data_type in _CSV_SCHEMAS[table_name]
    )
    connection.execute(
        f"""
        CREATE TEMP TABLE {table_name} AS
        SELECT *
        FROM read_csv(
            '{_sql_literal(csv_path)}',
            auto_detect = false,
            header = true,
            delim = ',',
            quote = '"',
            escape = '"',
            encoding = 'utf-8',
            nullstr = '',
            strict_mode = true,
            columns = {{{columns}}}
        )
        """
    )


def _materialize_optional_table(
    connection: duckdb.DuckDBPyConnection,
    table_name: str,
    csv_path: Path | None,
) -> None:
    if csv_path is not None:
        _materialize_csv_table(connection, table_name, csv_path)
        return
    columns = ", ".join(
        f"{column} {data_type}" for column, data_type in _CSV_SCHEMAS[table_name]
    )
    connection.execute(f"CREATE TEMP TABLE {table_name} ({columns})")


def _materialize_temporal_snapshots(
    connection: duckdb.DuckDBPyConnection,
    snapshots: tuple[tuple[str, datetime, datetime], ...],
) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE benchmark_temporal_snapshots (
            snapshot VARCHAR,
            published_at TIMESTAMPTZ,
            event_time_watermark TIMESTAMPTZ
        )
        """
    )
    if snapshots:
        connection.executemany(
            "INSERT INTO benchmark_temporal_snapshots VALUES (?, ?, ?)",
            snapshots,
        )


def _validate_mapping_contract(
    connection: duckdb.DuckDBPyConnection,
    *,
    strict_governance: bool,
) -> None:
    _require_unique_source_key(
        connection, "identity_crosswalk", "financial_aid_student_id"
    )
    _require_unique_source_key(
        connection, "aid_status_crosswalk", "financial_aid_status"
    )

    invalid_identity = connection.execute(
        """
        SELECT x.canonical_student_id
        FROM identity_crosswalk AS x
        LEFT JOIN academic_records AS a
          ON x.canonical_student_id = a.student_id
        WHERE x.canonical_student_id IS NULL
           OR trim(x.canonical_student_id) = ''
           OR a.student_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if invalid_identity is not None:
        raise ValueError(
            "identity_crosswalk must map every source key to a known canonical student"
        )

    placeholders = ", ".join("?" for _value in _CANONICAL_AID_STATUSES)
    invalid_status = connection.execute(
        f"""
        SELECT canonical_aid_status
        FROM aid_status_crosswalk
        WHERE canonical_aid_status IS NULL
           OR trim(canonical_aid_status) = ''
           OR canonical_aid_status NOT IN ({placeholders})
        LIMIT 1
        """,
        sorted(_CANONICAL_AID_STATUSES),
    ).fetchone()
    if invalid_status is not None:
        raise ValueError("aid_status_crosswalk must map to a canonical aid status")

    if strict_governance:
        _validate_governance_coverage(connection)


def _require_unique_source_key(
    connection: duckdb.DuckDBPyConnection,
    table_name: str,
    column_name: str,
) -> None:
    invalid = connection.execute(
        f"""
        SELECT {column_name}
        FROM {table_name}
        GROUP BY {column_name}
        HAVING {column_name} IS NULL
            OR trim({column_name}) = ''
            OR COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if invalid is not None:
        raise ValueError(
            f"{table_name} source key {column_name} must be nonempty and unique"
        )


def _validate_strict_aid_business_keys(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    invalid = connection.execute(
        """
        WITH all_aid AS (
            SELECT student_id, disbursement_date FROM financial_aid_records
            UNION ALL
            SELECT student_id, disbursement_date FROM financial_aid_late_arrivals
        )
        SELECT student_id, disbursement_date
        FROM all_aid
        GROUP BY student_id, disbursement_date
        HAVING student_id IS NULL
            OR trim(student_id) = ''
            OR disbursement_date IS NULL
            OR COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if invalid is not None:
        raise ValueError(
            "v3 financial-aid (student_id, disbursement_date) business keys "
            "must be nonempty and unique across current and late streams"
        )


def _validate_governance_coverage(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    invalid_identifier = connection.execute(
        """
        WITH all_aid AS (
            SELECT student_id, disbursement_date FROM financial_aid_records
            UNION ALL
            SELECT student_id, disbursement_date FROM financial_aid_late_arrivals
        )
        SELECT CASE
                 WHEN student_id IS NULL OR trim(student_id) = ''
                   THEN 'student_id'
                 ELSE 'disbursement_date'
               END AS invalid_field
        FROM all_aid
        WHERE student_id IS NULL
           OR trim(student_id) = ''
           OR disbursement_date IS NULL
        LIMIT 1
        """
    ).fetchone()
    if invalid_identifier is not None:
        # 金额和状态允许按腐化配置置空，业务事件标识则必须始终可连接。
        raise ValueError(
            f"financial-aid {invalid_identifier[0]} must be nonempty in versioned data"
        )

    uncovered_student = connection.execute(
        """
        WITH all_aid AS (
            SELECT student_id, aid_status FROM financial_aid_records
            UNION ALL
            SELECT student_id, aid_status FROM financial_aid_late_arrivals
        )
        SELECT aid.student_id
        FROM all_aid AS aid
        LEFT JOIN identity_crosswalk AS x
          ON aid.student_id = x.financial_aid_student_id
        WHERE aid.student_id IS NOT NULL
          AND trim(aid.student_id) <> ''
          AND x.financial_aid_student_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if uncovered_student is not None:
        raise ValueError(
            "identity_crosswalk must cover every nonempty financial-aid student_id"
        )

    uncovered_status = connection.execute(
        """
        WITH all_aid AS (
            SELECT aid_status FROM financial_aid_records
            UNION ALL
            SELECT aid_status FROM financial_aid_late_arrivals
        )
        SELECT aid.aid_status
        FROM all_aid AS aid
        LEFT JOIN aid_status_crosswalk AS x
          ON aid.aid_status = x.financial_aid_status
        WHERE aid.aid_status IS NOT NULL
          AND trim(aid.aid_status) <> ''
          AND x.financial_aid_status IS NULL
        LIMIT 1
        """
    ).fetchone()
    if uncovered_status is not None:
        raise ValueError(
            "aid_status_crosswalk must cover every nonempty financial-aid aid_status"
        )


def _validate_fragmentation_score(
    connection: duckdb.DuckDBPyConnection,
    manifest: dict[str, Any],
) -> None:
    placeholders = ", ".join("?" for _value in _CANONICAL_AID_STATUSES)
    actual = connection.execute(
        f"""
        WITH per_student AS (
            SELECT
                academic.student_id,
                COALESCE(
                    MAX(
                        CASE
                            WHEN aid.student_id IS NULL THEN 0.0
                            ELSE (
                                1.0
                                + CASE WHEN aid.aid_amount IS NULL THEN 0 ELSE 1 END
                                + CASE
                                    WHEN aid.aid_status IN ({placeholders}) THEN 1
                                    ELSE 0
                                  END
                            ) / 3.0
                        END
                    ),
                    0.0
                ) AS completeness
            FROM academic_records AS academic
            LEFT JOIN financial_aid_records AS aid
              ON academic.student_id = aid.student_id
            GROUP BY academic.student_id
        )
        SELECT AVG(completeness) FROM per_student
        """,
        sorted(_CANONICAL_AID_STATUSES),
    ).fetchone()[0]
    expected = float(manifest["fragmentation_score"])
    if actual is None or not math.isclose(
        actual, expected, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise ValueError("Manifest fragmentation_score does not match benchmark CSVs")


def _validate_selected_row_ids(
    connection: duckdb.DuckDBPyConnection,
    manifest: dict[str, Any],
) -> None:
    known_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT student_id FROM academic_records"
        ).fetchall()
    }
    selected = manifest["selected_row_ids"]
    for operator, identifiers in selected.items():
        expected_count = math.floor(
            len(known_ids) * manifest["corruption_percentages"][operator] + 0.5
        )
        if len(identifiers) != expected_count:
            raise ValueError(
                "Manifest selected_row_ids count does not match corruption "
                f"percentage for {operator}"
            )
        unknown = sorted(set(identifiers) - known_ids)
        if unknown:
            raise ValueError(
                "Manifest selected_row_ids contains unknown academic student IDs "
                f"for {operator}: {', '.join(unknown[:5])}"
            )

    observed_ids = {
        str(row[0])
        for row in connection.execute(
            """
            WITH all_aid AS (
                SELECT student_id FROM financial_aid_records
                UNION ALL
                SELECT student_id FROM financial_aid_late_arrivals
            )
            SELECT DISTINCT identity.canonical_student_id
            FROM all_aid AS aid
            JOIN identity_crosswalk AS identity
              ON aid.student_id = identity.financial_aid_student_id
            """
        ).fetchall()
    }
    actual_dropped = known_ids - observed_ids
    if actual_dropped != set(selected["drop_row"]):
        raise ValueError(
            "Manifest selected_row_ids for drop_row do not match missing "
            "financial-aid CSV rows"
        )

    mapped_canonical_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT canonical_student_id FROM identity_crosswalk"
        ).fetchall()
    ]
    if set(mapped_canonical_ids) != known_ids or len(mapped_canonical_ids) != len(
        known_ids
    ):
        # drop 后没有 aid 行可反查，因此 crosswalk 自身必须覆盖完整学业人群。
        raise ValueError(
            "identity_crosswalk must map every academic student exactly once"
        )

    actual_mismatched = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_student_id
            FROM identity_crosswalk
            WHERE canonical_student_id <> financial_aid_student_id
            """
        ).fetchall()
    }
    if actual_mismatched != set(selected["identifier_mismatch"]):
        raise ValueError(
            "Manifest selected_row_ids for identifier_mismatch do not match "
            "identity crosswalk CSV rows"
        )

    def canonical_ids_where(predicate: str) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                f"""
                WITH all_aid AS (
                    SELECT student_id, aid_amount, aid_status
                    FROM financial_aid_records
                    UNION ALL
                    SELECT student_id, aid_amount, aid_status
                    FROM financial_aid_late_arrivals
                )
                SELECT DISTINCT identity.canonical_student_id
                FROM all_aid AS aid
                JOIN identity_crosswalk AS identity
                  ON aid.student_id = identity.financial_aid_student_id
                LEFT JOIN aid_status_crosswalk AS status
                  ON aid.aid_status = status.financial_aid_status
                WHERE {predicate}
                """
            ).fetchall()
        }

    dropped = set(selected["drop_row"])
    for operator, column in (
        ("null_aid_amount", "aid.aid_amount"),
        ("null_aid_status", "aid.aid_status"),
    ):
        actual_null = canonical_ids_where(f"{column} IS NULL")
        # drop 先于置空执行，因此交叠学生没有可供观察的 aid 行。
        expected_null = set(selected[operator]) - dropped
        if actual_null != expected_null:
            raise ValueError(
                f"Manifest selected_row_ids for {operator} do not match "
                "null values in financial-aid CSV rows"
            )

    actual_status_drift = canonical_ids_where(
        "aid.aid_status IS NOT NULL AND aid.aid_status <> status.canonical_aid_status"
    )
    # null_status 在状态码漂移之前生效；两者交叠时最终状态仍为 NULL。
    expected_status_drift = (
        set(selected["aid_status_code_drift"])
        - dropped
        - set(selected["null_aid_status"])
    )
    if actual_status_drift != expected_status_drift:
        raise ValueError(
            "Manifest selected_row_ids for aid_status_code_drift do not match "
            "department-local status values in financial-aid CSV rows"
        )

    actual_delayed = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT identity.canonical_student_id
            FROM financial_aid_late_arrivals AS aid
            JOIN identity_crosswalk AS identity
              ON aid.student_id = identity.financial_aid_student_id
            """
        ).fetchall()
    }
    # drop 先执行；同时被抽中 drop 的学生不会产生可观察的迟到记录。
    expected_delayed = set(selected["publication_delay"]) - set(selected["drop_row"])
    if actual_delayed != expected_delayed:
        raise ValueError(
            "Manifest selected_row_ids for publication_delay do not match "
            "financial-aid late-arrival CSV rows"
        )


def _validate_publication_event_contract(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    duplicate_event = connection.execute(
        """
        SELECT event_id
        FROM financial_aid_publication_events
        GROUP BY event_id
        HAVING event_id IS NULL OR trim(event_id) = '' OR COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate_event is not None:
        raise ValueError("publication event_id must be nonempty and unique")

    invalid_stream = connection.execute(
        """
        SELECT arrival_stream
        FROM financial_aid_publication_events
        WHERE arrival_stream IS NULL
           OR arrival_stream NOT IN ('current', 'late')
        LIMIT 1
        """
    ).fetchone()
    if invalid_stream is not None:
        raise ValueError("publication arrival_stream must be current or late")

    for column_name in ("event_time", "observed_at", "published_at"):
        missing_time = connection.execute(
            f"""
            SELECT event_id
            FROM financial_aid_publication_events
            WHERE {column_name} IS NULL
            LIMIT 1
            """
        ).fetchone()
        if missing_time is not None:
            raise ValueError(f"publication {column_name} must be nonempty")


def _validate_temporal_record_counts(
    connection: duckdb.DuckDBPyConnection,
    expected_counts: tuple[int, int],
) -> None:
    for table_name, stream, expected in (
        ("financial_aid_records", "current", expected_counts[0]),
        ("financial_aid_late_arrivals", "late", expected_counts[1]),
    ):
        actual = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        if actual != expected:
            raise ValueError(
                f"temporal {stream}_record_count does not match {table_name}: "
                f"expected {expected}, got {actual}"
            )
        event_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM financial_aid_publication_events
            WHERE arrival_stream = ?
            """,
            [stream],
        ).fetchone()[0]
        if event_count != expected:
            raise ValueError(
                f"temporal {stream}_record_count does not match publication events: "
                f"expected {expected}, got {event_count}"
            )


def _validate_temporal_publication_contract(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    inconsistent_time = connection.execute(
        """
        SELECT event_id
        FROM financial_aid_publication_events
        WHERE published_at < observed_at
        LIMIT 1
        """
    ).fetchone()
    if inconsistent_time is not None:
        raise ValueError("publication published_at must not precede observed_at")

    inconsistent_event_date = connection.execute(
        """
        SELECT event_id
        FROM financial_aid_publication_events
        WHERE event_time <> CAST(timezone('UTC', observed_at) AS DATE)
        LIMIT 1
        """
    ).fetchone()
    if inconsistent_event_date is not None:
        raise ValueError(
            "publication event_time must equal the UTC date of observed_at"
        )

    invalid_visibility = connection.execute(
        """
        SELECT event.event_id
        FROM financial_aid_publication_events AS event
        CROSS JOIN (
            SELECT
                max(CASE WHEN snapshot = 'current' THEN published_at END)
                    AS current_published_at,
                max(CASE WHEN snapshot = 'current'
                         THEN event_time_watermark END)
                    AS current_watermark,
                max(CASE WHEN snapshot = 'replayed' THEN published_at END)
                    AS replayed_published_at,
                max(CASE WHEN snapshot = 'replayed'
                         THEN event_time_watermark END)
                    AS replayed_watermark
            FROM benchmark_temporal_snapshots
        ) AS snapshots
        WHERE (
            event.arrival_stream = 'current'
            AND (
                event.published_at > snapshots.current_published_at
                OR event.event_time > CAST(
                    timezone('UTC', snapshots.current_watermark) AS DATE
                )
            )
        ) OR (
            event.arrival_stream = 'late'
            AND (
                event.published_at <= snapshots.current_published_at
                OR event.published_at > snapshots.replayed_published_at
                OR event.event_time > CAST(
                    timezone('UTC', snapshots.replayed_watermark) AS DATE
                )
            )
        )
        LIMIT 1
        """
    ).fetchone()
    if invalid_visibility is not None:
        raise ValueError(
            "publication event visibility must respect temporal snapshot horizons"
        )

    mismatch = connection.execute(
        """
        WITH expected AS (
            SELECT student_id, disbursement_date AS event_time,
                   'current' AS arrival_stream
            FROM financial_aid_records
            UNION ALL
            SELECT student_id, disbursement_date AS event_time,
                   'late' AS arrival_stream
            FROM financial_aid_late_arrivals
        ),
        joined AS (
            SELECT expected.student_id AS expected_student_id,
                   event.financial_aid_student_id AS event_student_id
            FROM expected
            FULL OUTER JOIN financial_aid_publication_events AS event
              ON expected.student_id = event.financial_aid_student_id
             AND expected.event_time = event.event_time
             AND expected.arrival_stream = event.arrival_stream
        )
        SELECT 1
        FROM joined
        WHERE expected_student_id IS NULL OR event_student_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if mismatch is not None:
        raise ValueError(
            "publication events and aid records must cover each other one-to-one"
        )


def write_rows(
    path: Path,
    rows: list[dict[str, Any]],
    columns: list[str] | None = None,
) -> None:
    fieldnames = columns or (list(rows[0]) if rows else _EXPECTED_COLUMNS)
    write_csv_atomically(
        path,
        rows,
        fieldnames,
        lambda value: "" if value is None else str(value),
    )


def _sql_literal(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")
