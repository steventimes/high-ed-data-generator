"""
fragmentation_scoring.py
------------------------

Scoring utilities for query-level fragmentation benchmarking.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from workload_spec import JoinSpec, QuerySpec


def clamp01(value: float) -> float:
    """Clamp numeric values into [0, 1]."""
    return max(0.0, min(1.0, value))


def ratio_loss(
    observed: Optional[float], baseline: Optional[float], gamma: float
) -> Optional[float]:
    """Compute bounded loss based on observed/baseline ratio."""
    if observed is None or baseline is None:
        return None
    if gamma <= 0:
        raise ValueError("gamma must be > 0.")
    if baseline <= 0:
        return 0.0 if observed <= 0 else 1.0
    ratio = observed / baseline
    return clamp01((ratio - 1.0) / gamma)


def weighted_average(
    metrics: Dict[str, Optional[float]], weights: Dict[str, float]
) -> float:
    """Compute weighted average across available metrics.

    Missing metrics (`None`) are dropped and remaining weights are renormalized.
    """
    weighted_sum = 0.0
    weight_sum = 0.0
    for name, value in metrics.items():
        if value is None:
            continue
        weight = weights.get(name, 0.0)
        if weight <= 0:
            continue
        weighted_sum += value * weight
        weight_sum += weight
    if weight_sum == 0:
        return 0.0
    return clamp01(weighted_sum / weight_sum)


@dataclass(frozen=True)
class ScoringConfig:
    """Configurable score weights and normalization constants."""

    alpha: float = 0.6

    w_jml: float = 0.5
    w_ccl: float = 0.3
    w_mns: float = 0.2

    w_rtl: float = 0.5
    w_sbl: float = 0.3
    w_stl: float = 0.2

    stl_w_source: float = 0.4
    stl_w_join: float = 0.4
    stl_w_mapping: float = 0.2

    runtime_gamma: float = 3.0
    scanned_bytes_gamma: float = 3.0
    runtime_floor_ms: float = 50.0

    source_max: int = 6
    join_max: int = 5
    mapping_max: int = 3


@dataclass(frozen=True)
class PlanStats:
    """Selected operator stats parsed from DuckDB EXPLAIN ANALYZE JSON."""

    join_count: int = 0
    scan_count: int = 0
    scanned_bytes: Optional[float] = None


@dataclass(frozen=True)
class JoinDiagnostics:
    """Per-join diagnostics used for JML and CCL."""

    join_name: str
    eligible_left_rows: int
    matched_left_rows: int
    match_rate: float
    expected_rows: float
    observed_rows: int
    cardinality_ratio: float
    cardinality_loss: float
    rows_for_weight: int


@dataclass
class QueryScoreResult:
    """Structured result for one scored query."""

    query_name: str
    frag_level: Optional[str]
    source_fanout: int
    runtime_ms: float
    baseline_runtime_ms: float
    row_count: int
    metrics: Dict[str, Optional[float]]
    receipt: Dict[str, Any]
    result_df: Any = None


class ReceiptExecutor(Protocol):
    """Protocol for QueryReceiptLayer-like executors."""

    con: Any

    def execute(
        self,
        query_name: str,
        sql: str,
        frag_level: Optional[str] = None,
        return_result: bool = True,
    ) -> Any: ...

    def get_latest_receipt(self, query_name: str) -> Optional[Dict[str, Any]]: ...


def _walk_json_nodes(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json_nodes(item)


def _parse_numeric_value(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*$", value)
        if match:
            return float(match.group(1))
    return None


def extract_plan_stats(plan_json: Any) -> PlanStats:
    """Extract join count, scan count, and optional scanned bytes from plan JSON."""
    if not isinstance(plan_json, (dict, list)):
        return PlanStats()

    join_count = 0
    scan_count = 0
    scanned_bytes: Optional[float] = None

    for node in _walk_json_nodes(plan_json):
        node_text = " ".join(
            str(node.get(key, ""))
            for key in ("name", "operator_name", "operator_type", "type", "extra_info")
        ).upper()
        if "JOIN" in node_text:
            join_count += 1
        if "SCAN" in node_text:
            scan_count += 1
        if scanned_bytes is None:
            for key, value in node.items():
                if "byte" not in str(key).lower():
                    continue
                numeric = _parse_numeric_value(value)
                if numeric is not None and numeric >= 0:
                    scanned_bytes = numeric
                    break

    return PlanStats(join_count=join_count, scan_count=scan_count, scanned_bytes=scanned_bytes)


def build_join_diagnostic_sql(join_spec: JoinSpec) -> Dict[str, str]:
    """Build SQL snippets for join diagnostics."""
    left_sql = f"({join_spec.left_relation_sql})"
    right_sql = f"({join_spec.right_relation_sql})"
    left_keys = ", ".join(join_spec.left_key_exprs)
    observed_left_sql = left_sql
    if join_spec.expected_cardinality in {"one_to_one", "many_to_one"}:
        observed_left_sql = f"""
        (
            SELECT DISTINCT *
            FROM {left_sql} AS l
        )
        """

    eligible_sql = f"""
    SELECT COUNT(*) AS cnt
    FROM (
        SELECT DISTINCT {left_keys}
        FROM {left_sql} AS l
    ) AS left_keys
    """
    matched_sql = f"""
    SELECT COUNT(*) AS cnt
    FROM (
        SELECT DISTINCT {left_keys}
        FROM {left_sql} AS l
        WHERE EXISTS (
            SELECT 1
            FROM {right_sql} AS r
            WHERE {join_spec.join_condition_sql}
        )
    ) AS matched_keys
    """
    observed_sql = f"""
    SELECT COUNT(*) AS cnt
    FROM {observed_left_sql} AS l
    LEFT JOIN {right_sql} AS r
        ON {join_spec.join_condition_sql}
    """
    expected_sql = join_spec.expected_rows_sql.strip() if join_spec.expected_rows_sql else eligible_sql

    return {
        "eligible": eligible_sql,
        "matched": matched_sql,
        "observed": observed_sql,
        "expected": expected_sql,
    }


def _fetch_scalar(connection: Any, sql: str) -> float:
    row = connection.execute(sql).fetchone()
    if not row:
        return 0.0
    value = row[0]
    numeric = _parse_numeric_value(value)
    return numeric if numeric is not None else 0.0


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _safe_alias(name: str) -> str:
    alias = re.sub(r"[^0-9a-zA-Z_]+", "_", name.strip())
    if not alias:
        alias = "col"
    if alias[0].isdigit():
        alias = f"col_{alias}"
    return f"{alias}_null_rate"


class FragmentationScorer:
    """Compute and persist per-query fragmentation metrics."""

    FRAGMENTATION_TABLE = "fragmentation_receipts"

    def __init__(
        self,
        target_qrl: ReceiptExecutor,
        baseline_qrl: ReceiptExecutor,
        config: Optional[ScoringConfig] = None,
    ) -> None:
        self.target_qrl = target_qrl
        self.baseline_qrl = baseline_qrl
        self.config = config or ScoringConfig()
        self._ensure_fragmentation_table()

    def _ensure_fragmentation_table(self) -> None:
        self.target_qrl.con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.FRAGMENTATION_TABLE} (
                query_name TEXT,
                frag_level TEXT,
                timestamp TIMESTAMP,
                source_fanout INTEGER,
                runtime_ms DOUBLE,
                baseline_runtime_ms DOUBLE,
                row_count BIGINT,
                accuracy_score DOUBLE,
                efficiency_score DOUBLE,
                fragmentation_score DOUBLE,
                receipt TEXT
            )
            """
        )

    def _compute_join_diagnostics(self, join_spec: JoinSpec) -> JoinDiagnostics:
        sqls = build_join_diagnostic_sql(join_spec)
        eligible_left_rows = int(_fetch_scalar(self.target_qrl.con, sqls["eligible"]))
        matched_left_rows = int(_fetch_scalar(self.target_qrl.con, sqls["matched"]))
        observed_rows = int(_fetch_scalar(self.target_qrl.con, sqls["observed"]))

        expected_rows = _fetch_scalar(self.target_qrl.con, sqls["expected"])
        if not join_spec.expected_rows_sql:
            expected_rows = float(eligible_left_rows)

        match_rate = 1.0 if eligible_left_rows == 0 else matched_left_rows / eligible_left_rows
        ratio_den = max(expected_rows, 1.0)
        cardinality_ratio = observed_rows / ratio_den
        if observed_rows <= 0 and expected_rows > 0:
            cardinality_loss = 1.0
        elif cardinality_ratio <= 0:
            cardinality_loss = 0.0
        else:
            cardinality_loss = clamp01(abs(math.log2(cardinality_ratio)))

        return JoinDiagnostics(
            join_name=join_spec.name,
            eligible_left_rows=eligible_left_rows,
            matched_left_rows=matched_left_rows,
            match_rate=match_rate,
            expected_rows=expected_rows,
            observed_rows=observed_rows,
            cardinality_ratio=cardinality_ratio,
            cardinality_loss=cardinality_loss,
            rows_for_weight=eligible_left_rows,
        )

    def _compute_missingness(
        self, query_sql: str, required_output_attrs: Sequence[str]
    ) -> Tuple[Dict[str, float], float]:
        if not required_output_attrs:
            return {}, 0.0

        aliases = [_safe_alias(attr) for attr in required_output_attrs]
        select_parts = []
        for attr, alias in zip(required_output_attrs, aliases):
            select_parts.append(
                "COALESCE(AVG(CASE WHEN q."
                + _quote_ident(attr)
                + " IS NULL THEN 1.0 ELSE 0.0 END), 0.0) AS "
                + _quote_ident(alias)
            )
        missingness_sql = f"""
        SELECT
            {", ".join(select_parts)}
        FROM ({query_sql}) AS q
        """
        row = self.target_qrl.con.execute(missingness_sql).fetchone()
        if row is None:
            return {attr: 0.0 for attr in required_output_attrs}, 0.0

        null_rates: Dict[str, float] = {}
        for idx, attr in enumerate(required_output_attrs):
            numeric = _parse_numeric_value(row[idx]) if idx < len(row) else None
            null_rates[attr] = float(numeric) if numeric is not None else 0.0
        mns = sum(null_rates.values()) / len(null_rates)
        return null_rates, clamp01(mns)

    def _compute_runtime_loss(
        self, runtime_ms: float, baseline_runtime_ms: float
    ) -> Optional[float]:
        if min(runtime_ms, baseline_runtime_ms) < self.config.runtime_floor_ms:
            return None
        return ratio_loss(runtime_ms, baseline_runtime_ms, self.config.runtime_gamma)

    def _compute_stl(
        self, source_fanout: int, join_count: int, semantic_mappings: int
    ) -> float:
        source_den = max(self.config.source_max - 1, 1)
        join_den = max(self.config.join_max, 1)
        mapping_den = max(self.config.mapping_max, 1)

        src_norm = clamp01(max(0.0, source_fanout - 1) / source_den)
        join_norm = clamp01(join_count / join_den)
        mapping_norm = clamp01(semantic_mappings / mapping_den)

        return clamp01(
            self.config.stl_w_source * src_norm
            + self.config.stl_w_join * join_norm
            + self.config.stl_w_mapping * mapping_norm
        )

    def score_query(
        self, query_spec: QuerySpec, frag_level: Optional[str], return_result: bool = False
    ) -> QueryScoreResult:
        self.target_qrl.execute(
            query_name=query_spec.name,
            sql=query_spec.sql,
            frag_level=frag_level,
            return_result=False,
        )
        target_receipt = self.target_qrl.get_latest_receipt(query_spec.name)
        if target_receipt is None:
            raise RuntimeError(f"No target receipt found for query: {query_spec.name}")

        self.baseline_qrl.execute(
            query_name=query_spec.name,
            sql=query_spec.sql,
            frag_level="baseline",
            return_result=False,
        )
        baseline_receipt = self.baseline_qrl.get_latest_receipt(query_spec.name)
        if baseline_receipt is None:
            raise RuntimeError(f"No baseline receipt found for query: {query_spec.name}")

        target_payload = target_receipt.get("receipt") or {}
        baseline_payload = baseline_receipt.get("receipt") or {}
        target_plan_stats = extract_plan_stats(target_payload.get("plan"))
        baseline_plan_stats = extract_plan_stats(baseline_payload.get("plan"))

        runtime_ms = float(target_receipt.get("runtime_ms") or 0.0)
        baseline_runtime_ms = float(baseline_receipt.get("runtime_ms") or 0.0)
        source_fanout = int(target_receipt.get("source_fanout") or 0)
        row_count = int(target_receipt.get("row_count") or 0)

        join_details = [self._compute_join_diagnostics(join) for join in query_spec.joins]
        weighted_rows = sum(item.rows_for_weight for item in join_details)
        if weighted_rows > 0:
            jml = sum(
                item.rows_for_weight * (1.0 - item.match_rate) for item in join_details
            ) / weighted_rows
            ccl = sum(
                item.rows_for_weight * item.cardinality_loss for item in join_details
            ) / weighted_rows
        else:
            jml = 0.0
            ccl = 0.0

        missingness_sql = query_spec.missingness_sql or query_spec.sql
        missingness_attrs = query_spec.missingness_attrs or query_spec.required_output_attrs
        post_join_null_rates, mns = self._compute_missingness(
            missingness_sql, missingness_attrs
        )

        rtl = self._compute_runtime_loss(runtime_ms, baseline_runtime_ms)
        sbl = ratio_loss(
            target_plan_stats.scanned_bytes,
            baseline_plan_stats.scanned_bytes,
            self.config.scanned_bytes_gamma,
        )
        stl = self._compute_stl(
            source_fanout=source_fanout,
            join_count=len(query_spec.joins),
            semantic_mappings=query_spec.semantic_mappings,
        )

        accuracy_score = weighted_average(
            {"JML": jml, "CCL": ccl, "MNS": mns},
            {
                "JML": self.config.w_jml,
                "CCL": self.config.w_ccl,
                "MNS": self.config.w_mns,
            },
        )
        efficiency_score = weighted_average(
            {"RTL": rtl, "SBL": sbl, "STL": stl},
            {
                "RTL": self.config.w_rtl,
                "SBL": self.config.w_sbl,
                "STL": self.config.w_stl,
            },
        )
        fragmentation_score = clamp01(
            self.config.alpha * accuracy_score + (1.0 - self.config.alpha) * efficiency_score
        )

        metrics: Dict[str, Optional[float]] = {
            "JML": clamp01(jml),
            "CCL": clamp01(ccl),
            "MNS": clamp01(mns),
            "RTL": rtl,
            "SBL": sbl,
            "STL": clamp01(stl),
            "accuracy_score": accuracy_score,
            "efficiency_score": efficiency_score,
            "fragmentation_score": fragmentation_score,
        }

        receipt_obj: Dict[str, Any] = {
            "query_name": query_spec.name,
            "anchor_table": query_spec.anchor_table,
            "sources_touched": source_fanout,
            "joins": [asdict(item) for item in join_details],
            "required_output_attrs": list(query_spec.required_output_attrs),
            "missingness_attrs": list(missingness_attrs),
            "post_join_null_rates": post_join_null_rates,
            "semantic_mappings": query_spec.semantic_mappings,
            "runtime_ms": runtime_ms,
            "baseline_runtime_ms": baseline_runtime_ms,
            "scanned_bytes": target_plan_stats.scanned_bytes,
            "baseline_scanned_bytes": baseline_plan_stats.scanned_bytes,
            "metrics": metrics,
        }

        timestamp = datetime.now(UTC)
        self.target_qrl.con.execute(
            f"INSERT INTO {self.FRAGMENTATION_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query_spec.name,
                frag_level,
                timestamp,
                source_fanout,
                runtime_ms,
                baseline_runtime_ms,
                row_count,
                accuracy_score,
                efficiency_score,
                fragmentation_score,
                json.dumps(receipt_obj),
            ),
        )

        result_df = None
        if return_result:
            result_df = self.target_qrl.con.execute(query_spec.sql).df()

        return QueryScoreResult(
            query_name=query_spec.name,
            frag_level=frag_level,
            source_fanout=source_fanout,
            runtime_ms=runtime_ms,
            baseline_runtime_ms=baseline_runtime_ms,
            row_count=row_count,
            metrics=metrics,
            receipt=receipt_obj,
            result_df=result_df,
        )

    def score_workload(
        self, query_specs: Sequence[QuerySpec], frag_level: Optional[str], return_result: bool = False
    ) -> List[QueryScoreResult]:
        results: List[QueryScoreResult] = []
        for query_spec in query_specs:
            results.append(self.score_query(query_spec, frag_level=frag_level, return_result=return_result))
        return results

    @staticmethod
    def workload_score(results: Sequence[QueryScoreResult], query_specs: Sequence[QuerySpec]) -> float:
        weights = {item.name: item.weight for item in query_specs}
        weighted_sum = 0.0
        weight_sum = 0.0
        for result in results:
            metric = result.metrics.get("fragmentation_score")
            if metric is None:
                continue
            weight = weights.get(result.query_name, 1.0)
            weighted_sum += metric * weight
            weight_sum += weight
        if weight_sum == 0:
            return 0.0
        return clamp01(weighted_sum / weight_sum)
