from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from benchmark.questions import TemporalEvaluation

_SECONDS_PER_DAY = 86_400.0
_TEMPORAL_FIELDS = {
    "contract_version",
    "timezone",
    "logical_time",
    "snapshots",
    "current_record_count",
    "late_record_count",
}
_SNAPSHOT_FIELDS = {"published_at", "event_time_watermark"}
TEMPORAL_METRIC_FIELDS = (
    "current_freshness_lag_days",
    "replayed_freshness_lag_days",
    "replay_delay_days",
    "stale_missed_count",
    "stale_extra_count",
    "stale_action_count",
    "stale_action_denominator",
    "stale_action_rate",
)


@dataclass(frozen=True)
class TemporalContext:
    current_published_at: datetime
    current_event_time_watermark: datetime
    replayed_published_at: datetime
    replayed_event_time_watermark: datetime

    @property
    def current_freshness_lag_days(self) -> float:
        return (
            self.current_published_at - self.current_event_time_watermark
        ).total_seconds() / _SECONDS_PER_DAY

    @property
    def replayed_freshness_lag_days(self) -> float:
        return (
            self.replayed_published_at - self.replayed_event_time_watermark
        ).total_seconds() / _SECONDS_PER_DAY

    @property
    def replay_delay_days(self) -> float:
        return (
            self.replayed_published_at - self.current_published_at
        ).total_seconds() / _SECONDS_PER_DAY


@dataclass(frozen=True)
class StaleActionMetrics:
    stale_missed_count: int
    stale_extra_count: int
    stale_action_count: int
    stale_action_denominator: int
    stale_action_rate: float | None


class SqlExecutor(Protocol):
    def __call__(
        self, sql: str, *, required_columns: tuple[str, ...]
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class TemporalMetrics:
    current_freshness_lag_days: float | None = None
    replayed_freshness_lag_days: float | None = None
    replay_delay_days: float | None = None
    stale_missed_count: int | None = None
    stale_extra_count: int | None = None
    stale_action_count: int | None = None
    stale_action_denominator: int | None = None
    stale_action_rate: float | None = None

    def to_record(self) -> dict[str, float | int | None]:
        return {
            "current_freshness_lag_days": self.current_freshness_lag_days,
            "replayed_freshness_lag_days": self.replayed_freshness_lag_days,
            "replay_delay_days": self.replay_delay_days,
            "stale_missed_count": self.stale_missed_count,
            "stale_extra_count": self.stale_extra_count,
            "stale_action_count": self.stale_action_count,
            "stale_action_denominator": self.stale_action_denominator,
            "stale_action_rate": self.stale_action_rate,
        }


def parse_temporal_context(manifest: dict[str, Any]) -> TemporalContext | None:
    temporal = manifest.get("temporal")
    if temporal is None:
        manifest_version = manifest.get("manifest_version")
        if isinstance(manifest_version, int) and manifest_version >= 3:
            raise ValueError("Manifest version 3 must define temporal")
        return None
    if manifest.get("manifest_version") != 3:
        raise ValueError("Temporal contract requires manifest_version 3")

    temporal = _required_object(temporal, "temporal")
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
    current = _parse_snapshot(snapshots["current"], "current")
    replayed = _parse_snapshot(snapshots["replayed"], "replayed")
    if replayed[0] < current[0]:
        raise ValueError(
            "temporal replayed published_at must not be earlier than current published_at"
        )
    if replayed[1] < current[1]:
        raise ValueError("temporal replayed event_time_watermark must not regress")
    return TemporalContext(
        current_published_at=current[0],
        current_event_time_watermark=current[1],
        replayed_published_at=replayed[0],
        replayed_event_time_watermark=replayed[1],
    )


def compare_temporal_action_rows(
    current_rows: list[dict[str, Any]],
    replay_rows: list[dict[str, Any]],
    *,
    entity_key: str,
) -> StaleActionMetrics:
    current_ids = _unique_entity_ids(current_rows, entity_key, "current")
    replay_ids = _unique_entity_ids(replay_rows, entity_key, "replayed")
    stale_missed = replay_ids - current_ids
    stale_extra = current_ids - replay_ids
    denominator = len(current_ids | replay_ids)
    count = len(stale_missed) + len(stale_extra)
    return StaleActionMetrics(
        stale_missed_count=len(stale_missed),
        stale_extra_count=len(stale_extra),
        stale_action_count=count,
        stale_action_denominator=denominator,
        stale_action_rate=None if denominator == 0 else count / denominator,
    )


def compute_temporal_metrics(
    *,
    manifest: dict[str, Any],
    temporal_evaluation: TemporalEvaluation | None,
    entity_key: str,
    execute: SqlExecutor | None = None,
) -> TemporalMetrics:
    context = parse_temporal_context(manifest)
    if context is None:
        return TemporalMetrics()

    common = {
        "current_freshness_lag_days": context.current_freshness_lag_days,
        "replayed_freshness_lag_days": context.replayed_freshness_lag_days,
        "replay_delay_days": context.replay_delay_days,
    }
    if temporal_evaluation is None:
        return TemporalMetrics(**common)
    if execute is None:
        raise ValueError(
            "Temporal evaluation requires a SQL executor for current and replay snapshots"
        )

    required_columns = (entity_key,)
    current_rows = execute(
        temporal_evaluation.current_reference_sql,
        required_columns=required_columns,
    )
    replay_rows = execute(
        temporal_evaluation.replay_reference_sql,
        required_columns=required_columns,
    )
    stale = compare_temporal_action_rows(
        current_rows, replay_rows, entity_key=entity_key
    )
    return TemporalMetrics(
        **common,
        stale_missed_count=stale.stale_missed_count,
        stale_extra_count=stale.stale_extra_count,
        stale_action_count=stale.stale_action_count,
        stale_action_denominator=stale.stale_action_denominator,
        stale_action_rate=stale.stale_action_rate,
    )


def _parse_snapshot(payload: Any, name: str) -> tuple[datetime, datetime]:
    location = f"temporal.snapshots.{name}"
    snapshot = _required_object(payload, location)
    _require_exact_fields(snapshot, _SNAPSHOT_FIELDS, location)
    published_at = _parse_utc_timestamp(
        snapshot["published_at"], f"{location}.published_at"
    )
    watermark = _parse_utc_timestamp(
        snapshot["event_time_watermark"],
        f"{location}.event_time_watermark",
    )
    if watermark > published_at:
        raise ValueError(f"{location} watermark must not be later than published_at")
    return published_at, watermark


def _parse_utc_timestamp(value: Any, location: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{location} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{location} must be a valid RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{location} must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def _unique_entity_ids(
    rows: list[dict[str, Any]], entity_key: str, snapshot: str
) -> set[str]:
    identifiers: set[str] = set()
    for row in rows:
        value = row.get(entity_key)
        if value is None or not str(value).strip():
            raise ValueError(f"{snapshot} result contains an empty {entity_key}")
        identifier = str(value).strip().upper()
        if identifier in identifiers:
            raise ValueError(
                f"{snapshot} result contains duplicate {entity_key}: {identifier}"
            )
        identifiers.add(identifier)
    return identifiers


def _required_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{location} must be an object")
    return value


def _require_exact_fields(
    payload: dict[str, Any], required: set[str], location: str
) -> None:
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")
    unknown = sorted(set(payload) - required)
    if unknown:
        raise ValueError(f"{location} contains unknown fields: {', '.join(unknown)}")


def _non_negative_int(value: Any, location: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{location} must be an integer")
    if value < 0:
        raise ValueError(f"{location} must not be negative")
