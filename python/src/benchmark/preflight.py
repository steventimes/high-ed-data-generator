from __future__ import annotations

from pathlib import Path

import duckdb

from benchmark.questions import QuestionSpec, load_questions
from benchmark.sql_runtime import (
    _CSV_SCHEMAS,
    SqlValidationError,
    _materialize_optional_table,
    _materialize_temporal_snapshots,
    validate_read_only_sql,
)


def preflight_registry(path: Path) -> None:
    """在生成前校验注册表及其所有 SQL 对空 schema 的绑定。"""
    _validate_sql_bindings(_registered_sql(load_questions(path)))


def _validate_sql_bindings(
    labeled_sql: list[tuple[str, str, str]],
) -> None:
    connection = duckdb.connect(database=":memory:")
    try:
        # 复用运行时的唯一 schema 定义，但只创建空表，不读取生成目录或 CSV。
        for table_name in _CSV_SCHEMAS:
            _materialize_optional_table(connection, table_name, None)
        _materialize_temporal_snapshots(connection, ())

        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
        for location, sql, entity_key in labeled_sql:
            validated_sql = validate_read_only_sql(sql).rstrip(";")
            try:
                # DESCRIBE 完成列/类型绑定并返回结果 schema，不会执行查询。
                described = connection.execute(f"DESCRIBE {validated_sql}").fetchall()
            except duckdb.Error as error:
                raise SqlValidationError(
                    f"{location} does not bind to the benchmark schema: {error}"
                ) from error

            columns = [str(row[0]) for row in described]
            normalized_columns = [column.casefold() for column in columns]
            if len(normalized_columns) != len(set(normalized_columns)):
                raise SqlValidationError(
                    f"{location} result contains duplicate column names; "
                    "use unique aliases"
                )
            if entity_key not in columns:
                raise SqlValidationError(
                    f"{location} result is missing required columns: {entity_key}"
                )
    finally:
        connection.close()


def _registered_sql(questions: list[QuestionSpec]) -> list[tuple[str, str, str]]:
    statements: list[tuple[str, str, str]] = []
    for question in questions:
        for field_name in ("reference_sql", "decision_population_sql"):
            sql = getattr(question, field_name, None)
            if sql is not None:
                statements.append(
                    (
                        f"Question {question.question_id} {field_name}",
                        sql,
                        question.entity_key,
                    )
                )

        temporal = question.temporal_evaluation
        if temporal is None:
            continue
        for field_name in (
            "current_reference_sql",
            "replay_reference_sql",
            "decision_population_sql",
        ):
            sql = getattr(temporal, field_name, None)
            if sql is not None:
                statements.append(
                    (
                        f"Question {question.question_id} temporal_evaluation.{field_name}",
                        sql,
                        question.entity_key,
                    )
                )
    return statements
