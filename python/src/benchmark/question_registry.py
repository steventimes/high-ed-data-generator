from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
class QuestionSpec:
    question_id: str
    question: str
    institution_role: str | None = None
    decision_type: str | None = None
    entity_key: str = "student_id"
    is_ranked: bool = False
    top_k: int | None = None
    evaluation_title: str | None = None
    reference_sql: str | None = None
    weighting_policy: WeightingPolicy | None = None

    @property
    def display_title(self) -> str:
        return self.evaluation_title or self.question


def load_questions(path: Path | None, inline_questions: list[str]) -> list[QuestionSpec]:
    questions: list[QuestionSpec] = []
    resolved_path = path or discover_questions_file()
    if resolved_path is not None:
        questions.extend(load_questions_file(resolved_path))
    for index, question in enumerate(inline_questions, start=1):
        text = question.strip()
        if text:
            questions.append(QuestionSpec(question_id=f"cli_{safe_slug(text)}", question=text))
    if not questions:
        raise ValueError(
            "No questions found. Provide --questions-file question.json, --questions-file question.txt, or --question."
        )
    return apply_query_registry_metadata(questions)


def discover_questions_file() -> Path | None:
    for name in ("question.json", "question.txt", "questions.json"):
        path = Path(name)
        if path.exists():
            return path
    return None


def load_questions_file(path: Path) -> list[QuestionSpec]:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return load_questions_txt(path)
    if suffix == ".json":
        return load_questions_json(path)
    if suffix == ".jsonl":
        return load_questions_jsonl(path)
    raise ValueError("Question file must end with .txt, .json, or .jsonl")


def load_questions_txt(path: Path) -> list[QuestionSpec]:
    questions = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if text:
            questions.append(QuestionSpec(question_id=f"q{index}", question=text))
    return questions


def load_questions_json(path: Path) -> list[QuestionSpec]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, str):
        return [QuestionSpec(question_id="q1", question=payload.strip())]
    if isinstance(payload, dict):
        if "questions" in payload:
            return parse_question_items(payload["questions"])
        if "question" in payload or "query_text" in payload:
            return parse_question_items([payload])
    if isinstance(payload, list):
        return parse_question_items(payload)
    raise ValueError("JSON question file must be a string, list, or object with question/questions")


def load_questions_jsonl(path: Path) -> list[QuestionSpec]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            items.append(json.loads(text))
    return parse_question_items(items)


def parse_question_items(items: Any) -> list[QuestionSpec]:
    if not isinstance(items, list):
        raise ValueError("questions must be a list")
    questions = []
    for index, item in enumerate(items, start=1):
        questions.append(parse_question_item(item, index))
    return questions


def parse_question_item(item: Any, index: int) -> QuestionSpec:
    if isinstance(item, str):
        question = item.strip()
        question_id = safe_slug(question) or f"q{index}"
        metadata: dict[str, Any] = {}
    elif isinstance(item, dict):
        metadata = item
        question = str(item.get("question") or item.get("query_text") or "").strip()
        question_id = str(
            item.get("question_id")
            or item.get("query_id")
            or item.get("id")
            or safe_slug(question)
            or f"q{index}"
        ).strip()
    else:
        raise ValueError("each question must be a string or object")

    if not question:
        raise ValueError(f"question {index} is empty")

    return QuestionSpec(
        question_id=question_id,
        question=question,
        institution_role=clean_optional_str(metadata.get("institution_role")),
        decision_type=clean_optional_str(metadata.get("decision_type")),
        entity_key=clean_optional_str(metadata.get("entity_key")) or "student_id",
        is_ranked=bool(metadata.get("is_ranked", False)),
        top_k=parse_optional_int(metadata.get("top_k")),
        evaluation_title=clean_optional_str(metadata.get("evaluation_title")),
        reference_sql=clean_optional_sql(metadata.get("reference_sql")),
        weighting_policy=parse_weighting_policy(metadata.get("weighting_policy")),
    )


def parse_weighting_policy(payload: Any) -> WeightingPolicy | None:
    if payload in (None, ""):
        return None
    if not isinstance(payload, dict):
        raise ValueError("weighting_policy must be an object")

    policy_type = clean_optional_str(payload.get("type")) or "gpa_band"
    default_weight = float(payload.get("default_weight", 1.0))
    raw_bands = payload.get("bands", [])
    if not isinstance(raw_bands, list):
        raise ValueError("weighting_policy.bands must be a list")

    bands: list[WeightBand] = []
    for band in raw_bands:
        if not isinstance(band, dict):
            raise ValueError("each weighting band must be an object")
        bands.append(
            WeightBand(
                max_gpa=float(band["max_gpa"]),
                weight=float(band["weight"]),
            )
        )
    bands.sort(key=lambda band: band.max_gpa)
    return WeightingPolicy(
        policy_type=policy_type,
        default_weight=default_weight,
        bands=tuple(bands),
    )


def clean_optional_sql(value: Any) -> str | None:
    text = clean_optional_str(value)
    if text is None:
        return None
    return text.strip().rstrip(";") + ";"


def clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def apply_query_registry_metadata(
    questions: list[QuestionSpec],
    registry_path: Path | None = None,
) -> list[QuestionSpec]:
    registry_entries = load_query_registry(registry_path)
    if not registry_entries:
        return questions

    by_id = {entry.question_id: entry for entry in registry_entries}
    by_question = {normalize_question_text(entry.question): entry for entry in registry_entries}
    merged: list[QuestionSpec] = []

    for question in questions:
        match = by_id.get(question.question_id) or by_question.get(normalize_question_text(question.question))
        if match is None:
            merged.append(question)
            continue
        merged.append(
            QuestionSpec(
                question_id=match.question_id,
                question=question.question,
                institution_role=question.institution_role or match.institution_role,
                decision_type=question.decision_type or match.decision_type,
                entity_key=question.entity_key or match.entity_key,
                is_ranked=question.is_ranked or match.is_ranked,
                top_k=question.top_k if question.top_k is not None else match.top_k,
                evaluation_title=question.evaluation_title or match.evaluation_title,
                reference_sql=question.reference_sql or match.reference_sql,
                weighting_policy=question.weighting_policy or match.weighting_policy,
            )
        )
    return merged


def load_query_registry(path: Path | None = None) -> list[QuestionSpec]:
    resolved_path = path or discover_query_registry_file()
    if resolved_path is None or not resolved_path.exists():
        return []

    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        items = payload.get("queries") or payload.get("questions") or []
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("Query registry must be a list or object with queries/questions")
    return parse_question_items(items)


def discover_query_registry_file() -> Path | None:
    candidates = [
        Path("configs/query_registry.json"),
        Path("query_registry.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def normalize_question_text(value: str) -> str:
    return " ".join(value.lower().split())


def safe_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "item"
