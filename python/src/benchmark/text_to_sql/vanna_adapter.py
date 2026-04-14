from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from benchmark.duckdb_runner import canonical_sql, run_sql_on_variant
from benchmark.evaluation.metrics import write_csv
from benchmark.question_registry import QuestionSpec, load_questions

DEFAULT_VARIANTS = [
    "baseline",
    "low_fragmentation",
    "medium_fragmentation",
    "high_fragmentation",
]


@dataclass(frozen=True)
class TextToSqlTarget:
    label: str
    variant_dir: Path


@dataclass(frozen=True)
class TextToSqlResult:
    question_id: str
    question: str
    entity_key: str
    target_label: str
    variant_dir: str
    model: str
    generated_sql: str | None
    success: bool
    error: str | None
    reference_count: int
    generated_count: int | None
    missing_count: int | None
    extra_count: int | None
    matches_reference_ids: bool | None
    missing_entity_ids: list[str]
    extra_entity_ids: list[str]

    def to_record(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "entity_key": self.entity_key,
            "target_label": self.target_label,
            "variant_dir": self.variant_dir,
            "model": self.model,
            "generated_sql": self.generated_sql or "",
            "success": self.success,
            "error": self.error or "",
            "reference_count": self.reference_count,
            "generated_count": "" if self.generated_count is None else self.generated_count,
            "missing_count": "" if self.missing_count is None else self.missing_count,
            "extra_count": "" if self.extra_count is None else self.extra_count,
            "matches_reference_ids": ""
            if self.matches_reference_ids is None
            else self.matches_reference_ids,
            "missing_entity_ids": ";".join(self.missing_entity_ids),
            "extra_entity_ids": ";".join(self.extra_entity_ids),
        }


class SqlGenerator(Protocol):
    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        ...

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str:
        ...


class OpenAiSqlGenerator:
    def __init__(self, model: str) -> None:
        maybe_load_dotenv()
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise SystemExit(
                "Missing OpenAI dependency. Install it with:\n"
                "  python -m pip install 'openai>=1.0' python-dotenv"
            ) from exc
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit(
                "OPENAI_API_KEY is not set. Put it in .env or export it before running text_to_sql.sh."
            )
        self._client = OpenAI()
        self._model = model

    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        system_prompt = build_system_prompt(schema_context=schema_context, question=question)
        user_prompt = (
            "Generate one DuckDB SELECT query for this institutional question.\n"
            "Return only the SQL text, with no markdown and no explanation.\n\n"
            f"Question: {question.question}"
        )
        raw_text = self._call_model(system_prompt, user_prompt)
        return extract_sql(raw_text)

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str:
        system_prompt = build_system_prompt(schema_context=schema_context, question=question)
        user_prompt = (
            "The previous DuckDB SQL was invalid or did not satisfy the benchmark contract.\n"
            "Return one corrected SELECT statement only.\n\n"
            f"Question: {question.question}\n\n"
            f"Previous SQL:\n{previous_sql}\n\n"
            f"Execution or validation error:\n{error}"
        )
        raw_text = self._call_model(system_prompt, user_prompt)
        return extract_sql(raw_text)

    def _call_model(self, system_prompt: str, user_prompt: str) -> str:
        responses_error: Exception | None = None
        if hasattr(self._client, "responses"):
            try:
                response = self._client.responses.create(
                    model=self._model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                output_text = getattr(response, "output_text", None)
                if output_text:
                    return str(output_text)
            except Exception as exc:
                responses_error = exc

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return str(response.choices[0].message.content or "")
        except Exception:
            if responses_error is not None:
                raise responses_error
            raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run natural-language text-to-SQL experiments across benchmark variants. "
            "Generated SQL is executed on each variant and compared against a reference SQL oracle."
        )
    )
    parser.add_argument(
        "--questions-file",
        type=Path,
        help="question.txt or question.json. Defaults to question.json, question.txt, or questions.json if present.",
    )
    parser.add_argument("--question", action="append", default=[], help="Inline natural-language question.")
    parser.add_argument("--run-dir", type=Path, default=Path("artifacts/runs/local"))
    parser.add_argument(
        "--variants",
        default=",".join(DEFAULT_VARIANTS),
        help="Comma-separated variant names under --run-dir/variants.",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Explicit target in label=variant_dir form. Repeat to bypass --run-dir/--variants.",
    )
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5"))
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV. Defaults to <run-dir>/metrics/text_to_sql_experiments.csv.",
    )
    parser.add_argument(
        "--generated-results-dir",
        type=Path,
        help="Optional directory for generated SQL result CSVs.",
    )
    parser.add_argument("--print-canonical", action="store_true")
    return parser.parse_args()


def run_text_to_sql_experiment(
    *,
    questions: list[QuestionSpec],
    targets: list[TextToSqlTarget],
    generator: SqlGenerator,
    model: str,
    max_retries: int,
    generated_results_dir: Path | None = None,
) -> list[TextToSqlResult]:
    if not targets:
        return []

    schema_context = build_schema_context()
    validation_target = targets[0]
    results: list[TextToSqlResult] = []

    for question in questions:
        reference_sql = reference_sql_for_question(question)
        reference_rows_by_target = {
            target.label: run_sql_on_variant(target.variant_dir, reference_sql) for target in targets
        }
        reference_ids_by_target = {
            target.label: entity_ids(reference_rows_by_target[target.label], question.entity_key)
            for target in targets
        }

        generated_sql, generation_error = generate_question_sql(
            question=question,
            generator=generator,
            schema_context=schema_context,
            targets=targets,
            reference_ids_by_target=reference_ids_by_target,
            max_retries=max_retries,
        )

        for target in targets:
            reference_rows = reference_rows_by_target[target.label]
            reference_ids = reference_ids_by_target[target.label]
            if generation_error is not None or generated_sql is None:
                results.append(
                    TextToSqlResult(
                        question_id=question.question_id,
                        question=question.question,
                        entity_key=question.entity_key,
                        target_label=target.label,
                        variant_dir=str(target.variant_dir),
                        model=model,
                        generated_sql=generated_sql,
                        success=False,
                        error=generation_error,
                        reference_count=len(reference_rows),
                        generated_count=None,
                        missing_count=None,
                        extra_count=None,
                        matches_reference_ids=None,
                        missing_entity_ids=[],
                        extra_entity_ids=[],
                    )
                )
                continue

            output_csv = None
            if generated_results_dir is not None:
                output_csv = (
                    generated_results_dir
                    / f"{safe_slug(question.question_id)}__{safe_slug(target.label)}.csv"
                )

            results.append(
                run_generated_sql_against_target(
                    question=question,
                    target=target,
                    model=model,
                    generated_sql=generated_sql,
                    reference_rows=reference_rows,
                    reference_ids=reference_ids,
                    output_csv=output_csv,
                )
            )
    return results


def generate_question_sql(
    *,
    question: QuestionSpec,
    generator: SqlGenerator,
    schema_context: str,
    targets: list[TextToSqlTarget],
    reference_ids_by_target: dict[str, set[str]],
    max_retries: int,
) -> tuple[str | None, str | None]:
    generated_sql: str | None = None
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        if attempt == 0 or generated_sql is None or last_error is None:
            generated_sql = generator.generate_sql(question=question, schema_context=schema_context)
        else:
            generated_sql = generator.repair_sql(
                question=question,
                schema_context=schema_context,
                previous_sql=generated_sql,
                error=last_error,
            )
        try:
            validate_generated_sql_against_targets(
                question=question,
                generated_sql=generated_sql,
                targets=targets,
                reference_ids_by_target=reference_ids_by_target,
            )
            return generated_sql, None
        except Exception as exc:
            last_error = str(exc)
    return generated_sql, last_error or "SQL generation failed"


def validate_generated_sql_against_targets(
    *,
    question: QuestionSpec,
    generated_sql: str,
    targets: list[TextToSqlTarget],
    reference_ids_by_target: dict[str, set[str]],
) -> None:
    mismatches: list[str] = []
    for target in targets:
        generated_rows = run_sql_on_variant(target.variant_dir, generated_sql)
        generated_ids = entity_ids(generated_rows, question.entity_key)
        reference_ids = reference_ids_by_target[target.label]
        missing_ids = sorted(reference_ids - generated_ids)
        extra_ids = sorted(generated_ids - reference_ids)
        if missing_ids or extra_ids:
            mismatch_parts = []
            if missing_ids:
                mismatch_parts.append(
                    f"missing={len(missing_ids)} sample={','.join(missing_ids[:5])}"
                )
            if extra_ids:
                mismatch_parts.append(
                    f"extra={len(extra_ids)} sample={','.join(extra_ids[:5])}"
                )
            mismatches.append(f"{target.label}: " + "; ".join(mismatch_parts))
    if mismatches:
        raise ValueError(
            "Generated SQL does not preserve the benchmark action set across variants. "
            "Missing joined financial-aid rows should not be turned into positive matches unless the question explicitly asks for missing records. "
            + " | ".join(mismatches)
        )


def run_generated_sql_against_target(
    *,
    question: QuestionSpec,
    target: TextToSqlTarget,
    model: str,
    generated_sql: str,
    reference_rows: list[dict[str, Any]],
    reference_ids: set[str],
    output_csv: Path | None,
) -> TextToSqlResult:
    try:
        generated_rows = run_sql_on_variant(target.variant_dir, generated_sql, output_csv=output_csv)
        generated_ids = entity_ids(generated_rows, question.entity_key)
        missing = sorted(reference_ids - generated_ids)
        extra = sorted(generated_ids - reference_ids)
        return TextToSqlResult(
            question_id=question.question_id,
            question=question.question,
            entity_key=question.entity_key,
            target_label=target.label,
            variant_dir=str(target.variant_dir),
            model=model,
            generated_sql=generated_sql,
            success=True,
            error=None,
            reference_count=len(reference_rows),
            generated_count=len(generated_rows),
            missing_count=len(missing),
            extra_count=len(extra),
            matches_reference_ids=not missing and not extra,
            missing_entity_ids=missing,
            extra_entity_ids=extra,
        )
    except Exception as exc:
        return TextToSqlResult(
            question_id=question.question_id,
            question=question.question,
            entity_key=question.entity_key,
            target_label=target.label,
            variant_dir=str(target.variant_dir),
            model=model,
            generated_sql=generated_sql,
            success=False,
            error=str(exc),
            reference_count=len(reference_rows),
            generated_count=None,
            missing_count=None,
            extra_count=None,
            matches_reference_ids=None,
            missing_entity_ids=[],
            extra_entity_ids=[],
        )


def resolve_targets(args: argparse.Namespace) -> list[TextToSqlTarget]:
    if args.target:
        return [parse_target_spec(value) for value in args.target]
    targets = []
    for variant in [part.strip() for part in args.variants.split(",") if part.strip()]:
        targets.append(TextToSqlTarget(label=variant, variant_dir=args.run_dir / "variants" / variant))
    if not targets:
        raise ValueError("At least one target variant is required")
    missing = [target.variant_dir for target in targets if not target.variant_dir.exists()]
    if missing:
        raise FileNotFoundError("Missing variant directories: " + ", ".join(str(path) for path in missing))
    return targets


def parse_target_spec(value: str) -> TextToSqlTarget:
    label, sep, raw_path = value.partition("=")
    if not sep or not label.strip() or not raw_path.strip():
        raise ValueError("--target must be in label=variant_dir form")
    return TextToSqlTarget(label=label.strip(), variant_dir=Path(raw_path.strip()))


def build_schema_context() -> str:
    return "\n".join(
        [
            "Tables available in DuckDB:",
            "- academic_records(student_id, gpa, enrollment_status, semester)",
            "- financial_aid_records(student_id, aid_amount, aid_status, disbursement_date)",
            "",
            "student_id is the shared entity key across both tables.",
            "The benchmark semester is Fall 2024.",
            "The same SQL should be valid across the clean baseline and fragmented variants.",
        ]
    )


def build_system_prompt(*, schema_context: str, question: QuestionSpec) -> str:
    metadata_lines = []
    if question.institution_role:
        metadata_lines.append(f"- Institution role: {question.institution_role}")
    if question.decision_type:
        metadata_lines.append(f"- Decision type: {question.decision_type}")
    metadata_lines.append(f"- Entity key: {question.entity_key}")

    return (
        "You are a text-to-SQL system for a DuckDB benchmark database. "
        "Use only the provided tables and columns. Return exactly one SELECT statement. "
        "Do not include markdown fences, prose, comments, DDL, INSERT, UPDATE, DELETE, or PRAGMA statements.\n\n"
        "Benchmark semantics:\n"
        "- academic_records.enrollment_status allowed values: full_time, part_time.\n"
        "- financial_aid_records.aid_status allowed values: active, suspended, none.\n"
        "- In this benchmark, academically at risk means GPA below 2.5 unless the question says otherwise.\n"
        "- Use a LEFT JOIN from academic_records to financial_aid_records so missing aid records remain observable.\n"
        "- Dropped or missing financial_aid_records rows are fragmentation artifacts, not valid positive matches by default.\n"
        "- Do not use aid_status IS NULL, COALESCE(..., 0)=0, or similar null logic to turn missing joined aid rows into matches unless the question explicitly asks for missing records.\n"
        "- Return the entity key in the result so benchmark comparison can be performed.\n"
        "- If the question asks for suspended financial aid, use aid_status = 'suspended'.\n"
        "- If the question asks for students without active aid or with a financial-aid disruption, use aid_status <> 'active'.\n"
        "- If the question is about a disruption or aid status category, require an observed aid row with non-null aid_status before classifying the student.\n"
        "- The SQL must be valid DuckDB syntax.\n\n"
        f"{schema_context}\n\n"
        + question_specific_semantic_hint(question)
        + "\n\n"
        "Question metadata:\n"
        + "\n".join(metadata_lines)
    )


def question_specific_semantic_hint(question: QuestionSpec) -> str:
    reference_sql = (question.reference_sql or "").lower()
    hints: list[str] = ["Question-specific benchmark hints:"]
    if "aid_status = 'suspended'" in reference_sql:
        hints.append("- Use only observed rows with aid_status = 'suspended'.")
    if "aid_status = 'none'" in reference_sql:
        hints.append("- Treat 'no active aid award on file' as an observed aid row with aid_status = 'none', not a missing joined row.")
    if "aid_status <> 'active'" in reference_sql:
        hints.append("- Treat a financial-aid disruption as an observed non-active aid row, not a NULL join result.")
    if "aid_amount is not null" in reference_sql:
        hints.append("- Preserve the observed-aid requirement by checking aid_amount IS NOT NULL.")
    if "aid_status is not null" in reference_sql:
        hints.append("- Preserve the observed-aid requirement by checking aid_status IS NOT NULL.")
    if len(hints) == 1:
        hints.append("- Keep the SQL faithful to the institutional meaning of the question across all fragmentation variants.")
    return "\n".join(hints)


def reference_sql_for_question(question: QuestionSpec) -> str:
    return question.reference_sql or canonical_sql()


def extract_sql(text: str) -> str:
    stripped = text.strip()
    fence = re.search(r"```(?:sql)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()

    starts = [
        match.start()
        for match in re.finditer(r"(?im)^(?:with|select)\b", stripped)
    ]
    if not starts:
        with_match = re.search(r"\bwith\b", stripped, flags=re.IGNORECASE)
        if with_match:
            starts.append(with_match.start())
        select_match = re.search(r"\bselect\b", stripped, flags=re.IGNORECASE)
        if select_match:
            starts.append(select_match.start())
    if not starts:
        raise ValueError("Model output did not contain a SELECT statement")

    sql = stripped[min(starts) :].strip().rstrip(";")
    lowered = sql.lower()
    if not (lowered.startswith("select") or lowered.startswith("with ")):
        raise ValueError("Generated SQL must start with SELECT or WITH")

    forbidden = re.search(r"\b(insert|update|delete|drop|alter|create|pragma|copy)\b", sql, re.IGNORECASE)
    if forbidden:
        raise ValueError(f"Generated SQL contains forbidden keyword: {forbidden.group(1)}")
    return normalize_generated_sql(sql + ";")


def normalize_generated_sql(sql: str) -> str:
    normalized = sql.strip()
    if normalized.lower().startswith("with "):
        return normalized
    inferred_cte = infer_missing_initial_cte_name(normalized)
    if inferred_cte is not None:
        return f"WITH {inferred_cte} AS (\n{normalized}"
    return normalized


def infer_missing_initial_cte_name(sql: str) -> str | None:
    if re.search(r"\)\s*,\s*[A-Za-z_][A-Za-z0-9_]*\s+AS\s*\(", sql, flags=re.IGNORECASE) is None:
        return None
    defined_ctes = {
        match.group(1).lower()
        for match in re.finditer(r"[,]\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", sql, flags=re.IGNORECASE)
    }
    referenced_names = {
        match.group(1)
        for match in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql, flags=re.IGNORECASE)
    }
    excluded = {"academic_records", "financial_aid_records"} | defined_ctes
    candidates = sorted(name for name in referenced_names if name.lower() not in excluded)
    if len(candidates) == 1:
        return candidates[0]
    return None


def entity_ids(rows: list[dict[str, Any]], entity_key: str) -> set[str]:
    ids = set()
    for row in rows:
        if entity_key not in row:
            raise ValueError(f"Generated result must include {entity_key} so it can be compared")
        ids.add(str(row[entity_key]))
    return ids


def write_results(path: Path, results: list[TextToSqlResult]) -> None:
    records = [result.to_record() for result in results]
    fieldnames = list(records[0].keys()) if records else list(TextToSqlResult.__dataclass_fields__.keys())
    write_csv(path, records, fieldnames)


def maybe_load_dotenv(path: Path = Path(".env")) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        manual_load_dotenv(path)
        return
    load_dotenv(dotenv_path=path, override=False)


def manual_load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = clean_dotenv_value(value.strip())
        os.environ.setdefault(key, value)


def clean_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", value.strip()).strip("_").lower()
    return slug or "item"


def print_summary(results: list[TextToSqlResult], output: Path) -> None:
    print(f"Saved text-to-SQL comparison to {output}")
    for result in results:
        status = "ok" if result.success else "failed"
        if result.success:
            print(
                f"{result.question_id}/{result.target_label}: {status}, "
                f"reference={result.reference_count}, generated={result.generated_count}, "
                f"missing={result.missing_count}, extra={result.extra_count}"
            )
        else:
            print(f"{result.question_id}/{result.target_label}: {status}, error={result.error}")


def main() -> None:
    maybe_load_dotenv()
    args = parse_args()
    if args.print_canonical:
        print(canonical_sql())
        if (
            not args.question
            and args.questions_file is None
            and not args.target
            and args.variants == ",".join(DEFAULT_VARIANTS)
            and args.output is None
        ):
            return
    questions = load_questions(args.questions_file, args.question)
    targets = resolve_targets(args)
    output = args.output or (args.run_dir / "metrics" / "text_to_sql_experiments.csv")
    generated_results_dir = args.generated_results_dir
    if generated_results_dir is None and args.run_dir:
        generated_results_dir = args.run_dir / "metrics" / "text_to_sql_generated_results"

    generator = OpenAiSqlGenerator(model=args.model)
    results = run_text_to_sql_experiment(
        questions=questions,
        targets=targets,
        generator=generator,
        model=args.model,
        max_retries=args.max_retries,
        generated_results_dir=generated_results_dir,
    )
    write_results(output, results)
    print_summary(results, output)
    failed = sum(1 for result in results if not result.success)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
