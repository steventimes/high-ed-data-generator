from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parent
CONNECT_DIR = ROOT / "connect"
if str(CONNECT_DIR) not in sys.path:
    sys.path.insert(0, str(CONNECT_DIR))

try:
    from tabulate import tabulate
except ImportError:  # pragma: no cover - fallback is exercised manually
    tabulate = None


IGNORED_TABLES = {
    "receipts",
    "fragmentation_receipts",
    "text_to_sql_experiments",
}

DEFAULT_MAX_NULL_COLUMNS = 6
DEFAULT_MAX_RESULT_ROWS = 5


@dataclass(frozen=True)
class DatabaseTarget:
    label: str
    path: Path


@dataclass(frozen=True)
class QuestionSpec:
    question_id: str
    semantic_group: str
    question: str


@dataclass
class ExperimentResult:
    question_id: str
    semantic_group: str
    question: str
    target_label: str
    db_path: str
    model: str
    generated_sql: Optional[str]
    normalized_sql: Optional[str]
    sql_hash: Optional[str]
    success: bool
    error: Optional[str]
    runtime_ms: Optional[float]
    row_count: Optional[int]
    source_fanout: Optional[int]
    attempts: int
    receipt_query_name: Optional[str]
    result_preview: Optional[str]
    receipt_timestamp: Optional[str]
    receipt_payload_json: Optional[str]

    def to_record(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "semantic_group": self.semantic_group,
            "question": self.question,
            "target_label": self.target_label,
            "db_path": self.db_path,
            "model": self.model,
            "generated_sql": self.generated_sql,
            "normalized_sql": self.normalized_sql,
            "sql_hash": self.sql_hash,
            "success": self.success,
            "error": self.error,
            "runtime_ms": self.runtime_ms,
            "row_count": self.row_count,
            "source_fanout": self.source_fanout,
            "attempts": self.attempts,
            "receipt_query_name": self.receipt_query_name,
            "result_preview": self.result_preview,
            "receipt_timestamp": self.receipt_timestamp,
            "receipt_payload_json": self.receipt_payload_json,
        }


@dataclass(frozen=True)
class TableProfile:
    table_name: str
    row_count: int
    columns: tuple[tuple[str, str], ...]
    sparsest_columns: tuple[tuple[str, float], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and compare text-to-SQL queries against one or more DuckDB "
            "targets using Vanna OpenAI + the local Query Receipt Layer."
        )
    )
    parser.add_argument(
        "--db",
        dest="db_targets",
        action="append",
        required=True,
        help=(
            "DuckDB target in label=path form. Repeat for multiple fragmentation "
            "levels, e.g. baseline=./db/edu_baseline.duckdb"
        ),
    )
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="Natural-language question variant. Repeat to compare semantics.",
    )
    parser.add_argument(
        "--questions-file",
        type=str,
        help=(
            "Optional JSON, JSONL, or TXT file of question variants. JSON entries "
            "may be strings or objects with question/question_id/semantic_group."
        ),
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("OPENAI_MODEL", "gpt-5"),
        help="OpenAI model name passed to Vanna. Defaults to OPENAI_MODEL or gpt-5.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="How many repair attempts to allow after SQL execution errors.",
    )
    parser.add_argument(
        "--profile-mode",
        choices=("schema", "stats"),
        default="stats",
        help=(
            "Schema includes tables/columns only. Stats also includes per-table row "
            "counts and sparsity summaries so fragmentation can influence SQL choice."
        ),
    )
    parser.add_argument(
        "--max-null-columns",
        type=int,
        default=DEFAULT_MAX_NULL_COLUMNS,
        help="How many high-null columns to include per table in stats mode.",
    )
    parser.add_argument(
        "--max-preview-rows",
        type=int,
        default=DEFAULT_MAX_RESULT_ROWS,
        help="How many result rows to include in the experiment preview.",
    )
    parser.add_argument(
        "--include-table",
        action="append",
        default=[],
        help="Optional allowlist table name. Repeat to narrow schema context.",
    )
    parser.add_argument(
        "--exclude-table",
        action="append",
        default=[],
        help="Optional extra table exclusion. Repeat as needed.",
    )
    parser.add_argument(
        "--show-sql",
        action="store_true",
        help="Print the generated SQL for each question/target pair.",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Optional output path. Supports .csv and .json.",
    )
    parser.add_argument(
        "--no-execute",
        action="store_true",
        help="Only generate SQL. Skip execution and receipt logging.",
    )
    return parser.parse_args()


def maybe_load_dotenv() -> None:
    dotenv_path = ROOT / ".env"
    if not dotenv_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=dotenv_path, override=False)


def ensure_openai_api_key() -> None:
    if os.getenv("OPENAI_API_KEY"):
        return
    raise SystemExit(
        "OPENAI_API_KEY is not set. Export it in your shell or add it to .env. "
        "Do not hardcode the key in source."
    )


def get_query_receipt_layer() -> Any:
    from query_receipt_layer import QueryReceiptLayer

    return QueryReceiptLayer


def import_vanna() -> dict[str, Any]:
    try:
        from vanna.core.llm import LlmMessage, LlmRequest
        from vanna.core.user import User
        from vanna.integrations.duckdb import DuckDBRunner
        from vanna.integrations.openai import OpenAILlmService
        from vanna.tools import RunSqlTool
    except ImportError as exc:
        raise SystemExit(
            "Missing Vanna dependencies. Install them with:\n"
            "  pip install 'vanna[duckdb,openai]' python-dotenv"
        ) from exc

    return {
        "DuckDBRunner": DuckDBRunner,
        "LlmMessage": LlmMessage,
        "LlmRequest": LlmRequest,
        "OpenAILlmService": OpenAILlmService,
        "RunSqlTool": RunSqlTool,
        "User": User,
    }


def parse_target_spec(value: str) -> DatabaseTarget:
    label, sep, raw_path = value.partition("=")
    if not sep:
        raise ValueError(
            f"Invalid --db value {value!r}. Use label=path, for example baseline=./db/edu_baseline.duckdb."
        )
    label = label.strip()
    raw_path = raw_path.strip()
    if not label:
        raise ValueError(f"Missing target label in --db value {value!r}.")
    if not raw_path:
        raise ValueError(f"Missing database path in --db value {value!r}.")
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"DuckDB target not found: {path}")
    return DatabaseTarget(label=label, path=path)


def parse_targets(values: Sequence[str]) -> list[DatabaseTarget]:
    seen_labels: set[str] = set()
    targets: list[DatabaseTarget] = []
    for value in values:
        target = parse_target_spec(value)
        if target.label in seen_labels:
            raise ValueError(f"Duplicate target label: {target.label}")
        seen_labels.add(target.label)
        targets.append(target)
    return targets


def slugify(value: str, default: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", value.strip().lower()).strip("_")
    return slug or default


def load_questions(
    inline_questions: Sequence[str],
    questions_file: Optional[str],
) -> list[QuestionSpec]:
    questions: list[QuestionSpec] = []

    if questions_file:
        questions.extend(load_questions_from_file(Path(questions_file)))

    for index, question in enumerate(inline_questions, start=1):
        if not question.strip():
            continue
        questions.append(
            QuestionSpec(
                question_id=f"cli_q{index}",
                semantic_group="cli",
                question=question.strip(),
            )
        )

    if not questions:
        raise ValueError("Provide at least one --question or a --questions-file.")

    seen_ids: set[str] = set()
    deduped: list[QuestionSpec] = []
    for item in questions:
        candidate = item
        if candidate.question_id in seen_ids:
            suffix = 2
            while f"{candidate.question_id}_{suffix}" in seen_ids:
                suffix += 1
            candidate = QuestionSpec(
                question_id=f"{candidate.question_id}_{suffix}",
                semantic_group=candidate.semantic_group,
                question=candidate.question,
            )
        seen_ids.add(candidate.question_id)
        deduped.append(candidate)
    return deduped


def load_questions_from_file(path: Path) -> list[QuestionSpec]:
    if not path.exists():
        raise FileNotFoundError(f"Question file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("JSON question file must contain a list.")
        return [coerce_question_spec(item, index) for index, item in enumerate(payload, start=1)]

    if suffix == ".jsonl":
        rows = []
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            rows.append(coerce_question_spec(json.loads(stripped), index))
        return rows

    rows = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        rows.append(
            QuestionSpec(
                question_id=f"file_q{index}",
                semantic_group="file",
                question=stripped,
            )
        )
    return rows


def coerce_question_spec(value: Any, index: int) -> QuestionSpec:
    if isinstance(value, str):
        return QuestionSpec(
            question_id=f"file_q{index}",
            semantic_group="file",
            question=value.strip(),
        )

    if not isinstance(value, dict):
        raise ValueError(
            f"Question file entry #{index} must be a string or object, got {type(value).__name__}."
        )

    question = (
        value.get("question")
        or value.get("text")
        or value.get("prompt")
        or value.get("query")
    )
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"Question file entry #{index} is missing a non-empty question.")

    raw_id = (
        value.get("question_id")
        or value.get("id")
        or value.get("label")
        or f"file_q{index}"
    )
    raw_group = (
        value.get("semantic_group")
        or value.get("semantic_label")
        or value.get("group")
        or "file"
    )

    return QuestionSpec(
        question_id=slugify(str(raw_id), f"file_q{index}"),
        semantic_group=slugify(str(raw_group), "file"),
        question=question.strip(),
    )


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def normalize_sql(sql: str) -> str:
    collapsed = re.sub(r"\s+", " ", sql.strip())
    return collapsed.rstrip(";").strip().lower()


def hash_sql(normalized_sql: Optional[str]) -> Optional[str]:
    if not normalized_sql:
        return None
    return hashlib.sha256(normalized_sql.encode("utf-8")).hexdigest()[:12]


def list_user_tables(
    qrl: QueryReceiptLayer,
    include_tables: Sequence[str],
    exclude_tables: Sequence[str],
) -> list[str]:
    include_set = {name.strip() for name in include_tables if name.strip()}
    exclude_set = {name.strip() for name in exclude_tables if name.strip()}
    table_rows = qrl.con.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'main'
        ORDER BY table_name
        """
    ).fetchall()

    table_names = [row[0] for row in table_rows]
    filtered = []
    for table_name in table_names:
        if table_name in IGNORED_TABLES or table_name in exclude_set:
            continue
        if include_set and table_name not in include_set:
            continue
        filtered.append(table_name)
    return filtered


def build_table_profiles(
    qrl: QueryReceiptLayer,
    table_names: Sequence[str],
    *,
    profile_mode: str,
    max_null_columns: int,
) -> list[TableProfile]:
    if not table_names:
        return []

    placeholders = ", ".join("?" for _ in table_names)
    column_rows = qrl.con.execute(
        f"""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
          AND table_name IN ({placeholders})
        ORDER BY table_name, ordinal_position
        """,
        list(table_names),
    ).fetchall()

    columns_by_table: dict[str, list[tuple[str, str]]] = {name: [] for name in table_names}
    for table_name, column_name, data_type in column_rows:
        columns_by_table[table_name].append((column_name, data_type))

    profiles: list[TableProfile] = []
    for table_name in table_names:
        columns = tuple(columns_by_table.get(table_name, []))
        row_count = int(
            qrl.con.execute(f"SELECT COUNT(*) FROM {quote_ident(table_name)}").fetchone()[0]
        )
        sparsest_columns: tuple[tuple[str, float], ...] = ()
        if profile_mode == "stats" and row_count > 0 and columns:
            select_parts = []
            for column_name, _ in columns:
                alias = f"{column_name}_null_rate"
                select_parts.append(
                    "AVG(CASE WHEN "
                    + quote_ident(column_name)
                    + " IS NULL THEN 1.0 ELSE 0.0 END) AS "
                    + quote_ident(alias)
                )
            null_row = qrl.con.execute(
                f"SELECT {', '.join(select_parts)} FROM {quote_ident(table_name)}"
            ).fetchone()
            null_rates = []
            for index, (column_name, _) in enumerate(columns):
                raw_value = null_row[index] if null_row and index < len(null_row) else 0.0
                null_rate = float(raw_value or 0.0)
                if null_rate > 0:
                    null_rates.append((column_name, null_rate))
            null_rates.sort(key=lambda item: (-item[1], item[0]))
            sparsest_columns = tuple(null_rates[:max_null_columns])

        profiles.append(
            TableProfile(
                table_name=table_name,
                row_count=row_count,
                columns=columns,
                sparsest_columns=sparsest_columns,
            )
        )
    return profiles


def format_schema_context(
    profiles: Sequence[TableProfile],
    *,
    target: DatabaseTarget,
    profile_mode: str,
) -> str:
    lines = [
        f"Target label: {target.label}",
        f"Target database path: {target.path}",
        "",
        "Available tables and columns:",
    ]

    for profile in profiles:
        column_text = ", ".join(f"{name} {data_type}" for name, data_type in profile.columns)
        if profile_mode == "stats":
            lines.append(f"- {profile.table_name} ({profile.row_count} rows)")
        else:
            lines.append(f"- {profile.table_name}")
        lines.append(f"  columns: {column_text}")
        if profile_mode == "stats" and profile.sparsest_columns:
            sparse_text = ", ".join(
                f"{name} null~{rate:.0%}" for name, rate in profile.sparsest_columns
            )
            lines.append(f"  higher-null columns: {sparse_text}")

    join_hints = build_join_hints({profile.table_name for profile in profiles})
    if join_hints:
        lines.extend(["", "Join hints:"])
        for hint in join_hints:
            lines.append(f"- {hint}")

    return "\n".join(lines)


def build_join_hints(table_names: set[str]) -> list[str]:
    hints: list[str] = []
    if {"sis_enrollments", "identity_crosswalk_integration"} <= table_names:
        hints.append(
            "Join sis_enrollments.student_id = identity_crosswalk_integration.student_id "
            "to bridge SIS identities."
        )
    if {"identity_crosswalk_integration", "financial_aid_wide"} <= table_names:
        hints.append(
            "Join identity_crosswalk_integration.erp_person_id = financial_aid_wide.erp_person_id; "
            "align academic_year and term when term-level aid is needed."
        )
    if {"identity_crosswalk_integration", "lms_activity_wide"} <= table_names:
        hints.append(
            "Join identity_crosswalk_integration.sis_user_id = lms_activity_wide.sis_user_id; "
            "align academic_year and term for term-level LMS activity."
        )
    if {"financial_aid", "sis_enrollments"} <= table_names:
        hints.append(
            "financial_aid is usually student-year grain, so join on student_id and academic_year "
            "before assuming term-level detail."
        )
    if {"registrar_course_enrollments", "faculty_courses"} <= table_names:
        hints.append(
            "registrar_course_enrollments and faculty_courses can be joined on course_section_id, "
            "academic_year, and term."
        )
    if {"students_master", "student_demographics"} <= table_names:
        hints.append("students_master and student_demographics share student_id.")
    if "identity_crosswalk_integration" in table_names:
        hints.append(
            "identity_crosswalk_integration is the preferred bridge between student_id, erp_person_id, "
            "sis_user_id, and lms_user_id."
        )
    if any(name.endswith("_wide") for name in table_names):
        hints.append("Prefer *_wide tables when they already contain the measures needed at the right grain.")
    return hints


def build_system_prompt(schema_context: str, target: DatabaseTarget, profile_mode: str) -> str:
    profile_clause = (
        "Use the data profile to prefer the most complete and direct source when multiple "
        "semantically valid query paths exist."
        if profile_mode == "stats"
        else "Use the schema only; do not invent columns or tables."
    )
    return "\n".join(
        [
            "You are generating DuckDB SQL for a synthetic higher-education administrative database.",
            f"The current experiment target is '{target.label}'.",
            "",
            "Rules:",
            "- Produce exactly one SQL statement.",
            "- The statement must be a SELECT query valid in DuckDB.",
            "- Use only the tables and columns listed in the schema context.",
            "- If you need cross-system joins, prefer the documented bridge keys.",
            f"- {profile_clause}",
            "- If execution feedback says the SQL failed, repair the query and call the tool again.",
            "- Do not return prose when a tool call is possible; call run_sql with the SQL string.",
            "",
            schema_context,
        ]
    )


def build_user_prompt(question: QuestionSpec) -> str:
    return "\n".join(
        [
            f"Question ID: {question.question_id}",
            f"Semantic group: {question.semantic_group}",
            "",
            "Generate SQL for this user request:",
            question.question,
        ]
    )


def build_preview_from_dataframe(result_df: Any, max_rows: int) -> str:
    if result_df is None:
        return ""
    if hasattr(result_df, "head"):
        preview_df = result_df.head(max_rows)
        if hasattr(preview_df, "to_json"):
            return preview_df.to_json(orient="records")
    return str(result_df)


def extract_sql_from_response(response: Any) -> tuple[Optional[Any], Optional[str]]:
    tool_calls = getattr(response, "tool_calls", None) or []
    for tool_call in tool_calls:
        if getattr(tool_call, "name", None) != "run_sql":
            continue
        arguments = getattr(tool_call, "arguments", {}) or {}
        sql = arguments.get("sql")
        if isinstance(sql, str) and sql.strip():
            return tool_call, sql.strip()

    content = getattr(response, "content", None) or ""
    code_block_match = re.search(r"```sql\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
    if code_block_match:
        return None, code_block_match.group(1).strip()

    select_match = re.search(r"(select\b.*)", content, flags=re.IGNORECASE | re.DOTALL)
    if select_match:
        return None, select_match.group(1).strip().rstrip(";")

    return None, None


async def run_question_on_target(
    *,
    llm: Any,
    target: DatabaseTarget,
    question: QuestionSpec,
    schema_context: str,
    profile_mode: str,
    model: str,
    max_retries: int,
    execute_query: bool,
    max_preview_rows: int,
    vanna_exports: dict[str, Any],
) -> ExperimentResult:
    LlmMessage = vanna_exports["LlmMessage"]
    LlmRequest = vanna_exports["LlmRequest"]
    RunSqlTool = vanna_exports["RunSqlTool"]
    DuckDBRunner = vanna_exports["DuckDBRunner"]
    User = vanna_exports["User"]

    user = User(id="text-to-sql-experiment", username="experiment")
    tool_schema = RunSqlTool(
        sql_runner=DuckDBRunner(database_path=str(target.path))
    ).get_schema()
    system_prompt = build_system_prompt(schema_context, target, profile_mode)
    messages = [LlmMessage(role="user", content=build_user_prompt(question))]
    attempts = 0
    last_sql: Optional[str] = None
    last_error: Optional[str] = None
    receipt_query_name: Optional[str] = None
    receipt_payload_json: Optional[str] = None
    receipt_timestamp: Optional[str] = None
    runtime_ms: Optional[float] = None
    row_count: Optional[int] = None
    source_fanout: Optional[int] = None
    result_preview: Optional[str] = None

    qrl: Optional[QueryReceiptLayer] = None
    if execute_query:
        QueryReceiptLayer = get_query_receipt_layer()
        qrl = QueryReceiptLayer(str(target.path))

    try:
        while attempts <= max_retries:
            attempts += 1
            request = LlmRequest(
                messages=messages,
                tools=[tool_schema],
                user=user,
                stream=False,
                system_prompt=system_prompt,
            )
            response = await llm.send_request(request)
            tool_call, proposed_sql = extract_sql_from_response(response)
            if proposed_sql is None:
                last_error = "Model did not return a run_sql tool call or SQL text."
                assistant_content = getattr(response, "content", "") or ""
                messages.append(
                    LlmMessage(
                        role="assistant",
                        content=assistant_content,
                        tool_calls=getattr(response, "tool_calls", None),
                    )
                )
                messages.append(
                    LlmMessage(
                        role="user",
                        content=(
                            "You must call run_sql with one DuckDB SELECT query that answers the request. "
                            "Try again."
                        ),
                    )
                )
                continue

            last_sql = proposed_sql
            if not execute_query:
                return build_experiment_result(
                    question=question,
                    target=target,
                    model=model,
                    sql=last_sql,
                    success=True,
                    error=None,
                    runtime_ms=None,
                    row_count=None,
                    source_fanout=None,
                    attempts=attempts,
                    receipt_query_name=None,
                    result_preview=None,
                    receipt_timestamp=None,
                    receipt_payload_json=None,
                )

            assert qrl is not None
            receipt_query_name = (
                f"text_to_sql__{slugify(question.question_id, 'question')}__"
                f"{slugify(target.label, 'target')}__{attempts}_{uuid.uuid4().hex[:8]}"
            )
            try:
                result_df = qrl.execute(
                    query_name=receipt_query_name,
                    sql=last_sql,
                    frag_level=target.label,
                    return_result=True,
                )
                latest_receipt = qrl.get_latest_receipt(receipt_query_name)
                if latest_receipt:
                    runtime_ms = float(latest_receipt.get("runtime_ms") or 0.0)
                    row_count = int(latest_receipt.get("row_count") or 0)
                    source_fanout = int(latest_receipt.get("source_fanout") or 0)
                    timestamp = latest_receipt.get("timestamp")
                    receipt_timestamp = str(timestamp) if timestamp is not None else None
                    receipt_payload = latest_receipt.get("receipt")
                    receipt_payload_json = json.dumps(receipt_payload) if receipt_payload else None
                result_preview = build_preview_from_dataframe(result_df, max_preview_rows)
                return build_experiment_result(
                    question=question,
                    target=target,
                    model=model,
                    sql=last_sql,
                    success=True,
                    error=None,
                    runtime_ms=runtime_ms,
                    row_count=row_count,
                    source_fanout=source_fanout,
                    attempts=attempts,
                    receipt_query_name=receipt_query_name,
                    result_preview=result_preview,
                    receipt_timestamp=receipt_timestamp,
                    receipt_payload_json=receipt_payload_json,
                )
            except Exception as exc:
                last_error = str(exc)
                assistant_content = getattr(response, "content", "") or ""
                messages.append(
                    LlmMessage(
                        role="assistant",
                        content=assistant_content,
                        tool_calls=getattr(response, "tool_calls", None),
                    )
                )
                error_feedback = f"DuckDB execution error: {last_error}"
                if tool_call is not None:
                    messages.append(
                        LlmMessage(
                            role="tool",
                            content=error_feedback,
                            tool_call_id=tool_call.id,
                        )
                    )
                else:
                    messages.append(
                        LlmMessage(
                            role="user",
                            content=(
                                error_feedback
                                + " Return a corrected run_sql tool call with one DuckDB SELECT statement."
                            ),
                        )
                    )

        return build_experiment_result(
            question=question,
            target=target,
            model=model,
            sql=last_sql,
            success=False,
            error=last_error or "Unable to generate executable SQL.",
            runtime_ms=runtime_ms,
            row_count=row_count,
            source_fanout=source_fanout,
            attempts=attempts,
            receipt_query_name=receipt_query_name,
            result_preview=result_preview,
            receipt_timestamp=receipt_timestamp,
            receipt_payload_json=receipt_payload_json,
        )
    finally:
        if qrl is not None:
            qrl.close()


def build_experiment_result(
    *,
    question: QuestionSpec,
    target: DatabaseTarget,
    model: str,
    sql: Optional[str],
    success: bool,
    error: Optional[str],
    runtime_ms: Optional[float],
    row_count: Optional[int],
    source_fanout: Optional[int],
    attempts: int,
    receipt_query_name: Optional[str],
    result_preview: Optional[str],
    receipt_timestamp: Optional[str],
    receipt_payload_json: Optional[str],
) -> ExperimentResult:
    normalized = normalize_sql(sql) if sql else None
    return ExperimentResult(
        question_id=question.question_id,
        semantic_group=question.semantic_group,
        question=question.question,
        target_label=target.label,
        db_path=str(target.path),
        model=model,
        generated_sql=sql,
        normalized_sql=normalized,
        sql_hash=hash_sql(normalized),
        success=success,
        error=error,
        runtime_ms=runtime_ms,
        row_count=row_count,
        source_fanout=source_fanout,
        attempts=attempts,
        receipt_query_name=receipt_query_name,
        result_preview=result_preview,
        receipt_timestamp=receipt_timestamp,
        receipt_payload_json=receipt_payload_json,
    )


def render_results_table(results: Sequence[ExperimentResult]) -> str:
    rows = []
    for result in results:
        rows.append(
            [
                result.question_id,
                result.semantic_group,
                result.target_label,
                "yes" if result.success else "no",
                result.sql_hash or "-",
                "-" if result.runtime_ms is None else f"{result.runtime_ms:.2f}",
                "-" if result.row_count is None else str(result.row_count),
                "-" if result.source_fanout is None else str(result.source_fanout),
                result.attempts,
                result.error or "-",
            ]
        )

    headers = [
        "Question",
        "Group",
        "Target",
        "Success",
        "SQL Hash",
        "Runtime(ms)",
        "Rows",
        "Fanout",
        "Attempts",
        "Error",
    ]

    if tabulate is not None:
        return tabulate(rows, headers=headers, tablefmt="github")

    plain_lines = [" | ".join(headers)]
    for row in rows:
        plain_lines.append(" | ".join(str(item) for item in row))
    return "\n".join(plain_lines)


def print_sql_blocks(results: Sequence[ExperimentResult]) -> None:
    for result in results:
        print()
        print(f"[{result.target_label}] {result.question_id}: {result.question}")
        if result.generated_sql:
            print(result.generated_sql)
        else:
            print(result.error or "No SQL generated.")


def print_comparison_notes(results: Sequence[ExperimentResult]) -> None:
    by_question: dict[str, list[ExperimentResult]] = {}
    by_group: dict[str, list[ExperimentResult]] = {}

    for result in results:
        by_question.setdefault(result.question_id, []).append(result)
        by_group.setdefault(result.semantic_group, []).append(result)

    question_notes = []
    for question_id, items in sorted(by_question.items()):
        hashes = sorted({item.sql_hash for item in items if item.sql_hash})
        if len(hashes) > 1:
            rendered = ", ".join(
                f"{item.target_label}={item.sql_hash or 'n/a'}"
                for item in sorted(items, key=lambda row: row.target_label)
            )
            question_notes.append(f"- {question_id}: target-specific SQL variants detected ({rendered})")

    group_notes = []
    for group_name, items in sorted(by_group.items()):
        hashes = {item.sql_hash for item in items if item.sql_hash}
        if len(hashes) > 1:
            group_notes.append(
                f"- {group_name}: {len(hashes)} unique SQL shapes across {len(items)} experiments"
            )

    if question_notes:
        print("\nDifferences by question:")
        for line in question_notes:
            print(line)

    if group_notes:
        print("\nDifferences by semantic group:")
        for line in group_notes:
            print(line)


def write_results(path: Path, results: Sequence[ExperimentResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [result.to_record() for result in results]
    suffix = path.suffix.lower()

    if suffix == ".json":
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        return

    if suffix != ".csv":
        raise ValueError("Output path must end with .csv or .json")

    fieldnames = list(records[0].keys()) if records else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


async def async_main(args: argparse.Namespace) -> int:
    maybe_load_dotenv()
    ensure_openai_api_key()
    vanna_exports = import_vanna()

    targets = parse_targets(args.db_targets)
    questions = load_questions(args.question, args.questions_file)
    llm = vanna_exports["OpenAILlmService"](model=args.model)

    schema_context_cache: dict[str, str] = {}
    results: list[ExperimentResult] = []

    for target in targets:
        QueryReceiptLayer = get_query_receipt_layer()
        qrl = QueryReceiptLayer(str(target.path))
        try:
            table_names = list_user_tables(
                qrl,
                include_tables=args.include_table,
                exclude_tables=args.exclude_table,
            )
            profiles = build_table_profiles(
                qrl,
                table_names,
                profile_mode=args.profile_mode,
                max_null_columns=args.max_null_columns,
            )
            schema_context = format_schema_context(
                profiles,
                target=target,
                profile_mode=args.profile_mode,
            )
            schema_context_cache[target.label] = schema_context
        finally:
            qrl.close()

        for question in questions:
            result = await run_question_on_target(
                llm=llm,
                target=target,
                question=question,
                schema_context=schema_context_cache[target.label],
                profile_mode=args.profile_mode,
                model=args.model,
                max_retries=args.max_retries,
                execute_query=not args.no_execute,
                max_preview_rows=args.max_preview_rows,
                vanna_exports=vanna_exports,
            )
            results.append(result)

    print("\nText-to-SQL experiment summary:")
    print(render_results_table(results))
    print_comparison_notes(results)

    if args.show_sql:
        print("\nGenerated SQL:")
        print_sql_blocks(results)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        write_results(output_path, results)
        print(f"\nSaved experiment output to {output_path}")

    failed = sum(1 for result in results if not result.success)
    return 1 if failed else 0


def main() -> None:
    args = parse_args()
    try:
        raise SystemExit(asyncio.run(async_main(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
