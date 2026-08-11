from __future__ import annotations

from benchmark.questions import QuestionSpec


def build_schema_context() -> str:
    return (
        "Tables available in DuckDB:\n"
        "- academic_records(student_id, gpa, enrollment_status, semester)\n"
        "- financial_aid_records(student_id, aid_amount, aid_status, disbursement_date)\n"
        "- identity_crosswalk(canonical_student_id, financial_aid_student_id)\n\n"
        "academic_records.student_id is the institution canonical ID. "
        "financial_aid_records.student_id may use a department-local ID; "
        "identity_crosswalk is the governed mapping between them.\n"
        "The same SQL must work across the baseline and every fragmented variant."
    )


def build_system_prompt(*, schema_context: str, question: QuestionSpec) -> str:
    metadata = []
    if question.institution_role:
        metadata.append(f"- Institution role: {question.institution_role}")
    if question.decision_type:
        metadata.append(f"- Decision type: {question.decision_type}")
    metadata.append(f"- Entity key: {question.entity_key}")

    return (
        "You are a text-to-SQL system for a DuckDB benchmark database. "
        "Use only the provided tables and columns. Return exactly one SELECT statement. "
        "Do not include markdown, prose, comments, DDL, DML, PRAGMA, table functions, "
        "or external file access.\n\n"
        "Benchmark semantics:\n"
        "- academic_records.enrollment_status allowed values: full_time, part_time.\n"
        "- financial_aid_records.aid_status allowed values: active, suspended, none.\n"
        "- Academically at risk means GPA below 2.5 unless the question says otherwise.\n"
        "- Use a LEFT JOIN from academic_records to financial_aid_records so missing aid rows remain observable.\n"
        "- Missing aid rows are fragmentation artifacts, not positive matches by default.\n"
        "- Do not turn NULL joined rows into matches unless the question explicitly asks for missing records.\n"
        "- Return the entity key so benchmark comparison can be performed.\n"
        "- The SQL must be valid DuckDB syntax.\n\n"
        f"{schema_context}\n\n"
        f"{question_specific_semantic_hint(question)}\n\n"
        "Question metadata:\n" + "\n".join(metadata)
    )


def question_specific_semantic_hint(question: QuestionSpec) -> str:
    reference_sql = (question.reference_sql or "").lower()
    hints = ["Question-specific benchmark hints:"]
    if "identity_crosswalk" in reference_sql:
        hints.append(
            "- Use identity_crosswalk to resolve department-local identifiers "
            "to the canonical student ID before joining records."
        )
    if "aid_status = 'suspended'" in reference_sql:
        hints.append("- Use only observed rows with aid_status = 'suspended'.")
    if "aid_status = 'none'" in reference_sql:
        hints.append(
            "- Treat no active aid as an observed aid_status = 'none', not a missing joined row."
        )
    if "aid_status <> 'active'" in reference_sql:
        hints.append(
            "- Treat a financial-aid disruption as an observed non-active row, not a NULL join result."
        )
    if "aid_amount is not null" in reference_sql:
        hints.append("- Require aid_amount IS NOT NULL.")
    if "aid_status is not null" in reference_sql:
        hints.append("- Require aid_status IS NOT NULL.")
    if len(hints) == 1:
        hints.append(
            "- Keep the SQL faithful to the institutional meaning across all variants."
        )
    return "\n".join(hints)
