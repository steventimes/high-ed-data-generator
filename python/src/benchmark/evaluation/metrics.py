from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.csv_io import write_csv_atomically
from benchmark.questions import WeightingPolicy

VARIANT_NAMES = (
    "baseline",
    "low_fragmentation",
    "medium_fragmentation",
    "high_fragmentation",
)

FRAGMENTATION_LEVELS = {
    "baseline": "baseline",
    "low_fragmentation": "low",
    "medium_fragmentation": "medium",
    "high_fragmentation": "high",
}

FRAGMENTATION_LEVEL_ORDER = ["baseline", "low", "medium", "high"]


@dataclass(frozen=True)
class OutcomeMetrics:
    baseline_count: int
    returned_count: int
    tp: int
    fn: int
    fp: int
    miss_rate: float | None
    recall: float | None
    extra_rate: float | None
    jaccard: float | None
    weighted_miss_loss: float | None
    weighted_extra: float | None


def compute_fragmentation_score(academic_csv: Path | str, aid_csv: Path | str) -> float:
    return compute_fragmentation_score_from_rows(
        read_csv_rows(Path(academic_csv)),
        read_csv_rows(Path(aid_csv)),
    )


def compute_fragmentation_score_from_rows(
    academic_rows: list[dict[str, Any]],
    financial_aid_rows: list[dict[str, Any]],
) -> float:
    aid_rows = {str(row["student_id"]): row for row in financial_aid_rows}
    if not academic_rows:
        raise ValueError("academic_records must not be empty")

    total = 0.0
    for academic in academic_rows:
        aid = aid_rows.get(academic["student_id"])
        if aid is None:
            continue
        row_exists = 1.0
        amount_present = 1.0 if aid.get("aid_amount") not in (None, "") else 0.0
        status_present = (
            1.0 if aid.get("aid_status") in {"active", "suspended", "none"} else 0.0
        )
        total += (row_exists + amount_present + status_present) / 3.0
    return total / len(academic_rows)


def identified_student_ids(query_rows: list[dict[str, Any]]) -> set[str]:
    return {str(row["student_id"]) for row in query_rows}


def missed_students_vs_baseline(
    baseline_rows: list[dict[str, Any]],
    variant_rows: list[dict[str, Any]],
) -> list[str]:
    return sorted(
        identified_student_ids(baseline_rows) - identified_student_ids(variant_rows)
    )


def fragmentation_level_for_variant(variant: str) -> str:
    return FRAGMENTATION_LEVELS.get(variant, variant)


def fragmentation_level_sort_key(level: str) -> int:
    try:
        return FRAGMENTATION_LEVEL_ORDER.index(level)
    except ValueError:
        return len(FRAGMENTATION_LEVEL_ORDER)


def prepare_entity_rows(
    rows: list[dict[str, Any]],
    *,
    entity_key: str,
    uppercase_entity_key: bool = True,
    trim_whitespace: bool = True,
    score_column: str = "risk_score",
) -> tuple[dict[str, dict[str, Any]], int, int]:
    prepared: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    null_count = 0
    for row in rows:
        normalized_key = normalize_entity_value(
            row.get(entity_key),
            uppercase=uppercase_entity_key,
            trim_whitespace=trim_whitespace,
        )
        if normalized_key is None:
            null_count += 1
            continue

        candidate = dict(row)
        candidate[entity_key] = normalized_key
        existing = prepared.get(normalized_key)
        if existing is None:
            prepared[normalized_key] = candidate
            continue

        duplicate_count += 1
        prepared[normalized_key] = choose_preferred_row(
            existing, candidate, score_column=score_column
        )
    return prepared, duplicate_count, null_count


def compare_entity_sets(
    baseline_rows: list[dict[str, Any]],
    observed_rows: list[dict[str, Any]],
    *,
    entity_key: str = "student_id",
    weight_lookup: dict[str, float] | None = None,
    uppercase_entity_key: bool = True,
    trim_whitespace: bool = True,
) -> tuple[OutcomeMetrics, list[str], list[str]]:
    baseline_map, baseline_duplicate_count, baseline_null_count = prepare_entity_rows(
        baseline_rows,
        entity_key=entity_key,
        uppercase_entity_key=uppercase_entity_key,
        trim_whitespace=trim_whitespace,
    )
    observed_map, observed_duplicate_count, observed_null_count = prepare_entity_rows(
        observed_rows,
        entity_key=entity_key,
        uppercase_entity_key=uppercase_entity_key,
        trim_whitespace=trim_whitespace,
    )
    if baseline_null_count:
        raise ValueError(
            f"Baseline result contains {baseline_null_count} rows without {entity_key}"
        )
    if observed_null_count:
        raise ValueError(
            f"Observed result contains {observed_null_count} rows without {entity_key}"
        )
    if baseline_duplicate_count:
        raise ValueError(
            f"Baseline result contains {baseline_duplicate_count} duplicate {entity_key} rows"
        )
    if observed_duplicate_count:
        raise ValueError(
            f"Observed result contains {observed_duplicate_count} duplicate {entity_key} rows"
        )

    baseline_ids = set(baseline_map)
    observed_ids = set(observed_map)
    tp_ids = baseline_ids & observed_ids
    missing_ids = sorted(baseline_ids - observed_ids)
    extra_ids = sorted(observed_ids - baseline_ids)

    baseline_count = len(baseline_ids)
    returned_count = len(observed_ids)
    tp = len(tp_ids)
    fn = len(missing_ids)
    fp = len(extra_ids)
    union_count = len(baseline_ids | observed_ids)

    miss_rate = safe_ratio(fn, baseline_count)
    recall = safe_ratio(tp, baseline_count)
    extra_rate = safe_ratio(fp, baseline_count)
    jaccard = safe_ratio(tp, union_count)

    weighted_miss_loss = None
    weighted_extra = None
    if weight_lookup is not None:
        baseline_weight_total = sum(
            weight_lookup.get(entity_id, 1.0) for entity_id in baseline_ids
        )
        weighted_miss_loss = safe_ratio(
            sum(weight_lookup.get(entity_id, 1.0) for entity_id in missing_ids),
            baseline_weight_total,
        )
        weighted_extra = safe_ratio(
            sum(weight_lookup.get(entity_id, 1.0) for entity_id in extra_ids),
            baseline_weight_total,
        )

    metrics = OutcomeMetrics(
        baseline_count=baseline_count,
        returned_count=returned_count,
        tp=tp,
        fn=fn,
        fp=fp,
        miss_rate=miss_rate,
        recall=recall,
        extra_rate=extra_rate,
        jaccard=jaccard,
        weighted_miss_loss=weighted_miss_loss,
        weighted_extra=weighted_extra,
    )
    return metrics, missing_ids, extra_ids


def build_weight_lookup(
    *,
    academic_csv: Path | str,
    entity_key: str,
    weighting_policy: WeightingPolicy | None,
    uppercase_entity_key: bool = True,
    trim_whitespace: bool = True,
) -> dict[str, float]:
    return build_weight_lookup_from_rows(
        read_csv_rows(Path(academic_csv)),
        entity_key=entity_key,
        weighting_policy=weighting_policy,
        uppercase_entity_key=uppercase_entity_key,
        trim_whitespace=trim_whitespace,
    )


def build_weight_lookup_from_rows(
    rows: list[dict[str, Any]],
    *,
    entity_key: str,
    weighting_policy: WeightingPolicy | None,
    uppercase_entity_key: bool = True,
    trim_whitespace: bool = True,
) -> dict[str, float]:
    if weighting_policy is None:
        return {}
    if weighting_policy.policy_type != "gpa_band":
        raise ValueError(
            f"Unsupported weighting policy type: {weighting_policy.policy_type}"
        )

    lookup: dict[str, float] = {}
    for row in rows:
        normalized_key = normalize_entity_value(
            row.get(entity_key),
            uppercase=uppercase_entity_key,
            trim_whitespace=trim_whitespace,
        )
        if normalized_key is None:
            continue
        gpa_value = parse_optional_float(row.get("gpa"))
        lookup[normalized_key] = weight_for_gpa(
            gpa_value,
            weighting_policy=weighting_policy,
        )
    return lookup


def weight_for_gpa(
    gpa_value: float | None, *, weighting_policy: WeightingPolicy
) -> float:
    if gpa_value is None:
        return weighting_policy.default_weight
    for band in weighting_policy.bands:
        if gpa_value <= band.max_gpa:
            return band.weight
    return weighting_policy.default_weight


def choose_preferred_row(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    *,
    score_column: str,
) -> dict[str, Any]:
    existing_score = parse_optional_float(existing.get(score_column))
    candidate_score = parse_optional_float(candidate.get(score_column))
    if existing_score is None and candidate_score is None:
        return existing
    if existing_score is None:
        return candidate
    if candidate_score is None:
        return existing
    if candidate_score > existing_score:
        return candidate
    return existing


def normalize_entity_value(
    value: Any,
    *,
    uppercase: bool,
    trim_whitespace: bool,
) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if trim_whitespace:
        normalized = normalized.strip()
    if not normalized:
        return None
    if uppercase:
        normalized = normalized.upper()
    return normalized


def parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    write_csv_atomically(path, rows, fieldnames, stringify)


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)
