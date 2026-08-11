from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Self

import duckdb
from sqlglot import exp, parse
from sqlglot.errors import ParseError

_ALLOWED_TABLES = {
    "academic_records",
    "financial_aid_records",
    "identity_crosswalk",
}
_EXPECTED_COLUMNS = ["student_id"]
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
        has_condition = any(join.args.get(key) for key in ("on", "using", "method"))
        if kind == "cross" or not has_condition:
            # 大表笛卡尔积会在模型校验阶段成平方级放大，必须在执行前拒绝。
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
        names = {function.sql_name().casefold()}
        if function.name:
            names.add(function.name.casefold())
        if names & _VOLATILE_FUNCTIONS:
            # 同一 SQL 会在校验和落盘阶段执行多次，因此禁止依赖时间或随机数。
            raise SqlValidationError(
                "Benchmark SQL must be deterministic; volatile functions are not allowed"
            )
    return normalized


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


class VariantSqlRuntime:
    """在同一变体上复用 DuckDB 连接和固定 CSV schema。"""

    def __init__(self, variant_dir: Path | str) -> None:
        self.variant_dir = Path(variant_dir)
        self._connection: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeError("Variant SQL runtime is already open")
        academic_csv = self.variant_dir / "academic_records.csv"
        aid_csv = self.variant_dir / "financial_aid_records.csv"
        crosswalk_csv = self.variant_dir / "identity_crosswalk.csv"
        for path in (academic_csv, aid_csv):
            if not path.is_file():
                raise FileNotFoundError(f"Missing benchmark CSV: {path}")

        connection = duckdb.connect(database=":memory:")
        try:
            register_csv_views(
                connection,
                academic_csv,
                aid_csv,
                crosswalk_csv if crosswalk_csv.is_file() else None,
            )
        except Exception:
            connection.close()
            raise
        self._connection = connection
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def execute(
        self,
        sql: str,
        output_csv: Path | str | None = None,
        *,
        required_columns: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        if self._connection is None:
            raise RuntimeError("Variant SQL runtime is closed")
        validated_sql = validate_read_only_sql(sql).rstrip(";")
        result = self._connection.execute(validated_sql).fetchall()
        columns = [description[0] for description in self._connection.description]

        missing_columns = [
            column for column in required_columns if column not in columns
        ]
        if missing_columns:
            raise ValueError(
                "SQL result is missing required columns: " + ", ".join(missing_columns)
            )
        rows = [dict(zip(columns, row)) for row in result]
        if output_csv is not None:
            write_rows(Path(output_csv), rows, columns)
        return rows


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
    crosswalk_csv: Path | None = None,
) -> None:
    connection.execute(
        f"""
        CREATE VIEW academic_records AS
        SELECT *
        FROM read_csv(
            '{_sql_literal(academic_csv)}',
            header = true,
            nullstr = '',
            columns = {{
                'student_id': 'VARCHAR',
                'gpa': 'DOUBLE',
                'enrollment_status': 'VARCHAR',
                'semester': 'VARCHAR'
            }}
        );
        """
    )
    connection.execute(
        f"""
        CREATE VIEW financial_aid_records AS
        SELECT *
        FROM read_csv(
            '{_sql_literal(aid_csv)}',
            header = true,
            nullstr = '',
            columns = {{
                'student_id': 'VARCHAR',
                'aid_amount': 'DOUBLE',
                'aid_status': 'VARCHAR',
                'disbursement_date': 'DATE'
            }}
        );
        """
    )

    if crosswalk_csv is not None:
        connection.execute(
            f"""
            CREATE VIEW identity_crosswalk AS
            SELECT *
            FROM read_csv(
                '{_sql_literal(crosswalk_csv)}',
                header = true,
                columns = {{
                    'canonical_student_id': 'VARCHAR',
                    'financial_aid_student_id': 'VARCHAR'
                }}
            );
            """
        )


def write_rows(
    path: Path,
    rows: list[dict[str, Any]],
    columns: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = columns or (list(rows[0]) if rows else _EXPECTED_COLUMNS)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: "" if row.get(key) is None else str(row[key])
                    for key in fieldnames
                }
            )


def _sql_literal(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")
