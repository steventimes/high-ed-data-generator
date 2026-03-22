"""
query_receipt_layer.py
----------------------

This module defines a small Query Receipt Layer (QRL) for DuckDB.
The QRL wraps query execution, collects basic metrics about each
query, and writes a receipt into a `receipts` table stored in the
same database.  Receipts include the query name, timestamp, source
fan‑out (number of distinct tables referenced), execution time, the
row count of the result and the full JSON representation of DuckDB’s
`EXPLAIN ANALYZE` plan.

More advanced metrics (join coverage, schema drift, semantic
alignment, entity ambiguity, etc.) can be added by extending the
`execute` method.  The JSON plan contains estimated and actual
cardinalities for each operator which can be mined for coverage
statistics.

Example usage:

```python
from query_receipt_layer import QueryReceiptLayer

# connect to existing database
qrl = QueryReceiptLayer("/path/to/edu.duckdb")

# run a query
result_df = qrl.execute(
    query_name="retention",
    sql="SELECT s.student_id, s.academic_year, s.term, s.enrollment_status FROM sis_enrollments AS s"
)

# examine receipts table
receipts = qrl.con.execute("SELECT * FROM receipts").df()
print(receipts)
```
"""

import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Dict, Optional, Tuple

import duckdb
import pandas as pd


class QueryReceiptLayer:
    """Wrap DuckDB query execution and log receipts for each query."""

    RECEIPTS_TABLE = "receipts"

    def __init__(self, db_path: str) -> None:
        """Connect to the DuckDB database and ensure the receipts table exists."""
        self.con = duckdb.connect(db_path)
        # No special PRAGMA needed.  Earlier versions of DuckDB may not
        # support `create_or_replace_journal_mode`.
        self._ensure_receipts_table()

    def _ensure_receipts_table(self) -> None:
        """Create the receipts table if it does not already exist."""
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.RECEIPTS_TABLE} (
            query_name TEXT,
            frag_level TEXT,
            timestamp TIMESTAMP,
            source_fanout INTEGER,
            runtime_ms DOUBLE,
            row_count BIGINT,
            receipt TEXT
        )
        """
        self.con.execute(create_sql)

    @staticmethod
    def _extract_table_names(sql: str) -> Tuple[str, ...]:
        """Heuristically extract table names from a SQL statement.

        The implementation looks for tokens following FROM and JOIN
        keywords.  It strips optional aliases and punctuation.  This
        approach is simplistic but sufficient for the example
        workloads.  For more robust SQL parsing, consider using
        `sqlparse` or DuckDB’s own catalog inspection.
        """
        # Normalize whitespace and remove comments
        norm_sql = re.sub(r"\s+", " ", sql)
        # Find all occurrences of FROM or JOIN followed by a table name
        pattern = re.compile(r"\b(?:FROM|JOIN)\s+([^\s]+)", re.IGNORECASE)
        tables = []
        for match in pattern.finditer(norm_sql):
            token = match.group(1)
            # Remove trailing comma or semicolon
            token = token.strip(",;")
            # Strip alias if present (assumes alias follows immediately)
            if token.lower() in {"select", "on", "where"}:
                continue
            tables.append(token.split(".")[-1])  # handle schema.table
        return tuple(dict.fromkeys(tables))  # preserve order, remove duplicates

    def execute(
        self,
        query_name: str,
        sql: str,
        frag_level: Optional[str] = None,
        return_result: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Execute a SQL query, log a receipt and optionally return the result.

        Args:
            query_name: Logical name of the query (used in receipts)
            sql: The SQL statement to execute
            frag_level: Optional label describing the fragmentation level
            return_result: If True, return the query result as a pandas
                DataFrame; otherwise return None.

        Returns:
            A pandas DataFrame of the query result if `return_result` is
            True; otherwise None.
        """
        tables_used = self._extract_table_names(sql)
        source_fanout = len(tables_used)
        start_time = time.perf_counter()
        # Execute the query and optionally fetch result
        result_df: Optional[pd.DataFrame] = None
        row_count = 0
        try:
            # Fetch the result into DataFrame if requested
            if return_result:
                result_df = self.con.execute(sql).df()
                row_count = len(result_df)
            else:
                # Avoid Arrow dependency when only row_count is needed.
                count_sql = f"SELECT COUNT(*) FROM ({sql.strip().rstrip(';')}) AS _qrl_count"
                row = self.con.execute(count_sql).fetchone()
                row_count = int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            # Still log a receipt even if the query fails
            runtime_ms = (time.perf_counter() - start_time) * 1000.0
            self._log_receipt(
                query_name=query_name,
                frag_level=frag_level,
                source_fanout=source_fanout,
                runtime_ms=runtime_ms,
                row_count=row_count,
                plan_json=None,
                error=str(e),
            )
            raise
        runtime_ms = (time.perf_counter() - start_time) * 1000.0
        plan_json = self._fetch_plan_json(sql)
        self._log_receipt(
            query_name=query_name,
            frag_level=frag_level,
            source_fanout=source_fanout,
            runtime_ms=runtime_ms,
            row_count=row_count,
            plan_json=plan_json,
            error=None,
        )
        return result_df

    def _fetch_plan_json(self, sql: str) -> Optional[Any]:
        """Return EXPLAIN ANALYZE plan JSON if available for this query."""
        explain_variants = (
            f"EXPLAIN ANALYZE (FORMAT JSON) {sql}",
            f"EXPLAIN ANALYZE FORMAT JSON {sql}",
        )
        for explain_sql in explain_variants:
            try:
                plan_row = self.con.execute(explain_sql).fetchone()
            except Exception:
                continue
            plan_json = self._parse_plan_from_row(plan_row)
            if plan_json is not None:
                return plan_json
        return None

    @staticmethod
    def _parse_plan_from_row(row: Optional[Tuple[Any, ...]]) -> Optional[Any]:
        """Extract a JSON-like plan object from an EXPLAIN row."""
        if not row:
            return None
        for value in row:
            if isinstance(value, (dict, list)):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list)):
                    return parsed
        return None

    def _log_receipt(
        self,
        query_name: str,
        frag_level: Optional[str],
        source_fanout: int,
        runtime_ms: float,
        row_count: int,
        plan_json: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> None:
        """Insert a receipt record into the receipts table."""
        timestamp = datetime.now(UTC)
        receipt_obj = {
            "query_name": query_name,
            "tables_used": source_fanout,
            "runtime_ms": runtime_ms,
            "row_count": row_count,
            "error": error,
            "plan": plan_json,
        }
        # Insert into receipts table
        self.con.execute(
            f"INSERT INTO {self.RECEIPTS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                query_name,
                frag_level,
                timestamp,
                source_fanout,
                runtime_ms,
                row_count,
                json.dumps(receipt_obj),
            ),
        )

    def get_latest_receipt(self, query_name: str) -> Optional[Dict[str, Any]]:
        """Return the latest receipt row plus parsed JSON payload."""
        row = self.con.execute(
            f"""
            SELECT query_name, frag_level, timestamp, source_fanout, runtime_ms, row_count, receipt
            FROM {self.RECEIPTS_TABLE}
            WHERE query_name = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (query_name,),
        ).fetchone()
        if not row:
            return None
        query_name_v, frag_level, timestamp, source_fanout, runtime_ms, row_count, receipt_json = row
        receipt_obj: Dict[str, Any] = {}
        if isinstance(receipt_json, str):
            try:
                parsed = json.loads(receipt_json)
                if isinstance(parsed, dict):
                    receipt_obj = parsed
            except json.JSONDecodeError:
                receipt_obj = {}
        return {
            "query_name": query_name_v,
            "frag_level": frag_level,
            "timestamp": timestamp,
            "source_fanout": source_fanout,
            "runtime_ms": runtime_ms,
            "row_count": row_count,
            "receipt": receipt_obj,
        }

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        self.con.close()
