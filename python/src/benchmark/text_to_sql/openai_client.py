from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from benchmark.questions import QuestionSpec
from benchmark.sql_runtime import extract_sql
from benchmark.text_to_sql.prompts import build_system_prompt


class OpenAiSqlGenerator:
    def __init__(self, model: str, client: Any | None = None) -> None:
        load_dotenv()
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError(
                    "OpenAI support is not installed; install the text-to-sql extra"
                ) from error
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not set")
            client = OpenAI()
        self._client = client
        self._model = model

    def generate_sql(self, *, question: QuestionSpec, schema_context: str) -> str:
        system = build_system_prompt(schema_context=schema_context, question=question)
        user = (
            "Generate one DuckDB SELECT query for this institutional question. "
            "Return SQL only.\n\n"
            f"Question: {question.question}"
        )
        return extract_sql(self._call_model(system, user))

    def repair_sql(
        self,
        *,
        question: QuestionSpec,
        schema_context: str,
        previous_sql: str,
        error: str,
    ) -> str:
        system = build_system_prompt(schema_context=schema_context, question=question)
        user = (
            "Correct the previous DuckDB SQL and return one SELECT statement only.\n\n"
            f"Question: {question.question}\n\n"
            f"Previous SQL:\n{previous_sql}\n\n"
            f"Validation error:\n{error}"
        )
        return extract_sql(self._call_model(system, user))

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
                if response.output_text:
                    return str(response.output_text)
            except Exception as error:  # noqa: BLE001
                # 兼容仅实现 Chat Completions 的旧客户端，同时保留 Responses 的原始错误。
                responses_error = error
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


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, separator, raw_value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key.isidentifier():
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
