from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.sql_runtime import validate_read_only_sql

DEFAULT_REGISTRY = Path(__file__).with_name("query_registry.json")
_QUESTION_FIELDS = {
    "question_id",
    "question",
    "institution_role",
    "decision_type",
    "entity_key",
    "evaluation_title",
    "reference_sql",
    "weighting_policy",
    "temporal_evaluation",
    "remediated_causes",
}
_ALLOWED_REMEDIATED_CAUSES = frozenset(
    {"publication_delay", "identity_mismatch", "semantic_drift"}
)
_WEIGHTING_POLICY_FIELDS = {"type", "default_weight", "bands"}
_WEIGHT_BAND_FIELDS = {"max_gpa", "weight"}
_TEMPORAL_EVALUATION_FIELDS = {
    "current_reference_sql",
    "replay_reference_sql",
    "snapshot",
}
_TEMPORAL_SNAPSHOTS = {"current", "replayed"}


@dataclass(frozen=True)
class WeightBand:
    max_gpa: float
    weight: float


@dataclass(frozen=True)
class WeightingPolicy:
    policy_type: str
    default_weight: float = 1.0
    bands: tuple[WeightBand, ...] = ()


@dataclass(frozen=True)
class TemporalEvaluation:
    current_reference_sql: str
    replay_reference_sql: str
    snapshot: str = "replayed"


@dataclass(frozen=True)
class QuestionSpec:
    question_id: str
    question: str
    institution_role: str | None = None
    decision_type: str | None = None
    entity_key: str = "student_id"
    evaluation_title: str | None = None
    reference_sql: str | None = None
    weighting_policy: WeightingPolicy | None = None
    temporal_evaluation: TemporalEvaluation | None = None
    remediated_causes: frozenset[str] = frozenset()

    @property
    def display_title(self) -> str:
        return self.evaluation_title or self.question


def load_questions(path: Path = DEFAULT_REGISTRY) -> list[QuestionSpec]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json,
        )
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Question registry does not exist: {path}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("queries"), list):
        raise TypeError("Question registry must be an object containing a queries list")
    _reject_unknown_fields(payload, {"queries"}, "Question registry")

    questions = [
        _parse_question(item, index)
        for index, item in enumerate(payload["queries"], start=1)
    ]
    validate_questions(questions)
    return questions


def resolve_questions(
    *,
    registry_path: Path = DEFAULT_REGISTRY,
    inline_questions: list[str] | None = None,
) -> list[QuestionSpec]:
    registry = load_questions(registry_path)
    requested = [
        question.strip() for question in inline_questions or [] if question.strip()
    ]
    if not requested:
        return registry

    by_text = {normalize_question(question.question): question for question in registry}
    resolved = []
    for text in requested:
        # 只按压缩空白和大小写匹配，保留原始问句用于提示词。
        match = by_text.get(normalize_question(text))
        if match is None:
            raise ValueError(f"Inline question is not registered: {text}")
        else:
            resolved.append(
                QuestionSpec(
                    question_id=match.question_id,
                    question=text,
                    institution_role=match.institution_role,
                    decision_type=match.decision_type,
                    entity_key=match.entity_key,
                    evaluation_title=match.evaluation_title,
                    reference_sql=match.reference_sql,
                    weighting_policy=match.weighting_policy,
                    temporal_evaluation=match.temporal_evaluation,
                    remediated_causes=match.remediated_causes,
                )
            )
    validate_questions(resolved)
    return resolved


def normalize_question(value: str) -> str:
    return " ".join(value.casefold().split())


def slugify(value: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", value.strip()).strip("_").lower()
    return slug or "item"


def _parse_question(item: Any, index: int) -> QuestionSpec:
    if not isinstance(item, dict):
        raise TypeError(f"Query {index} must be an object")
    _reject_unknown_fields(item, _QUESTION_FIELDS, f"Query {index}")

    question = _required_text(item, "question", index)
    question_id = _required_text(item, "question_id", index)
    entity_key = _optional_text(item.get("entity_key")) or "student_id"

    reference_sql = _optional_text(item.get("reference_sql"))
    if reference_sql is not None:
        reference_sql = validate_read_only_sql(reference_sql.strip().rstrip(";") + ";")

    temporal_evaluation = _parse_temporal_evaluation(
        item.get("temporal_evaluation"), index
    )
    if temporal_evaluation is not None:
        if temporal_evaluation.snapshot == "current":
            expected_reference_sql = temporal_evaluation.current_reference_sql
            expected_field = "current_reference_sql"
        else:
            expected_reference_sql = temporal_evaluation.replay_reference_sql
            expected_field = "replay_reference_sql"
        if reference_sql != expected_reference_sql:
            raise ValueError(f"Query {index} reference_sql must match {expected_field}")
    return QuestionSpec(
        question_id=question_id,
        question=question,
        institution_role=_optional_text(item.get("institution_role")),
        decision_type=_optional_text(item.get("decision_type")),
        entity_key=entity_key,
        evaluation_title=_optional_text(item.get("evaluation_title")),
        reference_sql=reference_sql,
        weighting_policy=_parse_weighting_policy(item.get("weighting_policy"), index),
        temporal_evaluation=temporal_evaluation,
        remediated_causes=_parse_remediated_causes(
            item.get("remediated_causes"), index
        ),
    )


def _parse_temporal_evaluation(payload: Any, index: int) -> TemporalEvaluation | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise TypeError(f"Query {index} temporal_evaluation must be an object")
    _reject_unknown_fields(
        payload,
        _TEMPORAL_EVALUATION_FIELDS,
        f"Query {index} temporal_evaluation",
    )

    current_sql = _required_text(payload, "current_reference_sql", index)
    replay_sql = _required_text(payload, "replay_reference_sql", index)
    current_sql = validate_read_only_sql(current_sql.rstrip(";") + ";")
    replay_sql = validate_read_only_sql(replay_sql.rstrip(";") + ";")

    snapshot = _optional_text(payload.get("snapshot")) or "replayed"
    if snapshot not in _TEMPORAL_SNAPSHOTS:
        raise ValueError(
            f"Query {index} temporal_evaluation.snapshot must be current or replayed"
        )
    return TemporalEvaluation(
        current_reference_sql=current_sql,
        replay_reference_sql=replay_sql,
        snapshot=snapshot,
    )


def _parse_remediated_causes(payload: Any, index: int) -> frozenset[str]:
    if payload is None:
        return frozenset()
    if not isinstance(payload, list):
        raise TypeError(f"Query {index} remediated_causes must be a list")

    causes: list[str] = []
    for value in payload:
        cause = _optional_text(value)
        if cause is None:
            raise ValueError(f"Query {index} remediated_causes must not contain blanks")
        if cause not in _ALLOWED_REMEDIATED_CAUSES:
            raise ValueError(f"Query {index} has unsupported remediated cause: {cause}")
        causes.append(cause)
    if len(causes) != len(set(causes)):
        raise ValueError(f"Query {index} remediated_causes contains duplicates")
    return frozenset(causes)


def _parse_weighting_policy(payload: Any, index: int) -> WeightingPolicy | None:
    if payload in (None, ""):
        return None
    if not isinstance(payload, dict):
        raise TypeError(f"Query {index} weighting_policy must be an object")
    _reject_unknown_fields(
        payload,
        _WEIGHTING_POLICY_FIELDS,
        f"Query {index} weighting_policy",
    )

    policy_type = _optional_text(payload.get("type")) or "gpa_band"
    if policy_type != "gpa_band":
        raise ValueError(
            f"Query {index} has unsupported weighting policy: {policy_type}"
        )
    default_weight = _finite_float(
        payload.get("default_weight", 1.0),
        f"Query {index} weighting_policy.default_weight",
    )
    if default_weight <= 0:
        raise ValueError(
            f"Query {index} weighting_policy.default_weight must be positive"
        )

    raw_bands = payload.get("bands", [])
    if not isinstance(raw_bands, list):
        raise TypeError(f"Query {index} weighting_policy.bands must be a list")
    bands = []
    for band_index, band in enumerate(raw_bands, start=1):
        if not isinstance(band, dict):
            raise TypeError(
                f"Query {index} weighting band {band_index} must be an object"
            )
        _reject_unknown_fields(
            band,
            _WEIGHT_BAND_FIELDS,
            f"Query {index} weighting band {band_index}",
        )
        if "max_gpa" not in band or "weight" not in band:
            raise ValueError(
                f"Query {index} weighting band {band_index} must define max_gpa and weight"
            )
        max_gpa = _finite_float(
            band["max_gpa"], f"Query {index} weighting band {band_index} max_gpa"
        )
        weight = _finite_float(
            band["weight"], f"Query {index} weighting band {band_index} weight"
        )
        if not 0.0 <= max_gpa <= 4.0:
            raise ValueError(
                f"Query {index} weighting band {band_index} max_gpa must be within 0..4"
            )
        if weight <= 0:
            raise ValueError(
                f"Query {index} weighting band {band_index} weight must be positive"
            )
        bands.append(WeightBand(max_gpa=max_gpa, weight=weight))
    bands.sort(key=lambda band: band.max_gpa)
    if len({band.max_gpa for band in bands}) != len(bands):
        raise ValueError(
            f"Query {index} weighting bands contain duplicate max_gpa values"
        )
    return WeightingPolicy(
        policy_type=policy_type,
        default_weight=default_weight,
        bands=tuple(bands),
    )


def _required_text(item: dict[str, Any], key: str, index: int) -> str:
    value = _optional_text(item.get(key))
    if value is None:
        raise ValueError(f"Query {index} must define {key}")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Expected a string value")
    text = value.strip()
    return text or None


def validate_questions(questions: list[QuestionSpec]) -> None:
    if not questions:
        raise ValueError("Question registry must contain at least one query")

    for index, question in enumerate(questions, start=1):
        if not isinstance(question, QuestionSpec):
            raise TypeError(f"Question {index} must be a QuestionSpec")
        for field_name, value in (
            ("question_id", question.question_id),
            ("question", question.question),
            ("entity_key", question.entity_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Question {index} must define {field_name}")

        invalid_causes = sorted(
            set(question.remediated_causes) - _ALLOWED_REMEDIATED_CAUSES
        )
        if invalid_causes:
            raise ValueError(
                f"Question {index} has unsupported remediated cause: "
                + ", ".join(invalid_causes)
            )

        if question.reference_sql is not None:
            if not isinstance(question.reference_sql, str):
                raise TypeError(f"Question {index} reference_sql must be a string")
            validate_read_only_sql(question.reference_sql)
        temporal = question.temporal_evaluation
        if temporal is not None:
            if not isinstance(temporal, TemporalEvaluation):
                raise TypeError(
                    f"Question {index} temporal_evaluation must be TemporalEvaluation"
                )
            if temporal.snapshot not in _TEMPORAL_SNAPSHOTS:
                raise ValueError(
                    f"Question {index} temporal_evaluation.snapshot must be current or replayed"
                )
            validate_read_only_sql(temporal.current_reference_sql)
            validate_read_only_sql(temporal.replay_reference_sql)
            selected_sql = (
                temporal.current_reference_sql
                if temporal.snapshot == "current"
                else temporal.replay_reference_sql
            )
            if question.reference_sql != selected_sql:
                raise ValueError(
                    f"Question {index} reference_sql must match selected temporal SQL"
                )

    ids = [question.question_id for question in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("Question registry contains duplicate question_id values")
    normalized = [normalize_question(question.question) for question in questions]
    if len(normalized) != len(set(normalized)):
        raise ValueError("Question registry contains duplicate question text")

    # ID 会进入结果文件名；规范化后冲突会让后一个问题静默覆盖前一个结果。
    filename_ids = [slugify(question.question_id) for question in questions]
    if len(filename_ids) != len(set(filename_ids)):
        raise ValueError("Question IDs collide after filename normalization")


def _reject_unknown_fields(
    payload: dict[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{location} contains unknown fields: {', '.join(unknown)}")


def _finite_float(value: Any, location: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{location} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise TypeError(f"{location} must be a number") from error
    if not math.isfinite(number):
        raise ValueError(f"{location} must be finite")
    return number


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"Duplicate JSON key: {key}")
        payload[key] = value
    return payload
