from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from text_to_sql import coerce_question_spec, load_questions_from_file, normalize_sql, parse_target_spec


class TextToSqlHelpersTest(unittest.TestCase):
    def test_normalize_sql_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_sql(" SELECT  *\nFROM sis_enrollments ; "),
            "select * from sis_enrollments",
        )

    def test_coerce_question_spec_supports_metadata(self) -> None:
        spec = coerce_question_spec(
            {
                "id": "Aid Bridge",
                "semantic_group": "Financial Aid",
                "question": "What is the average Pell amount by term?",
            },
            1,
        )
        self.assertEqual(spec.question_id, "aid_bridge")
        self.assertEqual(spec.semantic_group, "financial_aid")
        self.assertEqual(spec.question, "What is the average Pell amount by term?")

    def test_load_questions_from_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "questions.json"
            path.write_text(
                json.dumps(
                    [
                        "Show enrollment counts by term",
                        {
                            "id": "lms usage",
                            "group": "lms",
                            "question": "Average login count by term",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            specs = load_questions_from_file(path)

        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0].question_id, "file_q1")
        self.assertEqual(specs[1].question_id, "lms_usage")
        self.assertEqual(specs[1].semantic_group, "lms")

    def test_parse_target_spec_uses_label_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "demo.duckdb"
            db_path.write_text("", encoding="utf-8")
            target = parse_target_spec(f"baseline={db_path}")

        self.assertEqual(target.label, "baseline")
        self.assertTrue(str(target.path).endswith("demo.duckdb"))


if __name__ == "__main__":
    unittest.main()
