import tempfile
import unittest
from pathlib import Path

try:
    import duckdb  # type: ignore

    DUCKDB_AVAILABLE = True
except Exception:
    DUCKDB_AVAILABLE = False

import sys

CONNECT_DIR = Path(__file__).resolve().parents[1]
if str(CONNECT_DIR) not in sys.path:
    sys.path.insert(0, str(CONNECT_DIR))

from fragmentation_scoring import (  # noqa: E402
    FragmentationScorer,
    build_join_diagnostic_sql,
    ratio_loss,
    weighted_average,
)
from workload_spec import JoinSpec, QuerySpec, default_workload  # noqa: E402

if DUCKDB_AVAILABLE:
    from query_receipt_layer import QueryReceiptLayer  # noqa: E402


class FormulaTests(unittest.TestCase):
    def test_ratio_loss(self):
        self.assertEqual(ratio_loss(100.0, 100.0, 3.0), 0.0)
        self.assertEqual(ratio_loss(400.0, 100.0, 3.0), 1.0)
        self.assertEqual(ratio_loss(50.0, 100.0, 3.0), 0.0)
        self.assertIsNone(ratio_loss(None, 100.0, 3.0))

    def test_efficiency_renormalization(self):
        value = weighted_average(
            {"RTL": 0.6, "SBL": None, "STL": 0.2},
            {"RTL": 0.5, "SBL": 0.3, "STL": 0.2},
        )
        expected = (0.6 * 0.5 + 0.2 * 0.2) / (0.5 + 0.2)
        self.assertAlmostEqual(value, expected, places=8)


class JoinBuilderTests(unittest.TestCase):
    def test_join_sql_builder(self):
        join = JoinSpec(
            name="sis_to_xwalk",
            left_relation_sql="SELECT student_id FROM sis_enrollments",
            right_relation_sql="SELECT student_id FROM identity_crosswalk_integration",
            join_condition_sql="l.student_id = r.student_id",
            left_key_exprs=("l.student_id",),
        )
        sqls = build_join_diagnostic_sql(join)
        self.assertIn("select distinct l.student_id", sqls["eligible"].lower())
        self.assertIn("where exists", sqls["matched"].lower())
        self.assertIn("left join", sqls["observed"].lower())
        self.assertIn("select distinct *", sqls["observed"].lower())
        self.assertIn("select count(*)", sqls["expected"].lower())

    def test_requires_expected_rows_for_one_to_many(self):
        with self.assertRaises(ValueError):
            JoinSpec(
                name="one_to_many_missing_expected",
                left_relation_sql="SELECT student_id FROM sis_enrollments",
                right_relation_sql="SELECT student_id, course_id FROM registrar_course_enrollments",
                join_condition_sql="l.student_id = r.student_id",
                left_key_exprs=("l.student_id",),
                expected_cardinality="one_to_many",
            )


class WorkloadSpecTests(unittest.TestCase):
    def test_default_workload_routes_cross_system_joins_via_crosswalk(self):
        workload = default_workload()
        self.assertEqual(len(workload), 3)
        by_name = {item.name: item for item in workload}
        self.assertIn("q2_aid_bridge", by_name)
        self.assertIn("q3_lms_bridge", by_name)
        self.assertIn(
            "identity_crosswalk_integration", by_name["q2_aid_bridge"].sql.lower()
        )
        self.assertIn(
            "identity_crosswalk_integration", by_name["q3_lms_bridge"].sql.lower()
        )
        self.assertGreaterEqual(len(by_name["q2_aid_bridge"].joins), 2)
        self.assertGreaterEqual(len(by_name["q3_lms_bridge"].joins), 2)


@unittest.skipUnless(DUCKDB_AVAILABLE, "duckdb is not installed in this environment")
class EndToEndScorerTests(unittest.TestCase):
    def _create_db(self, db_path: Path, fragmented: bool) -> None:
        con = duckdb.connect(str(db_path))
        con.execute(
            """
            CREATE TABLE sis_enrollments (
                student_id TEXT,
                academic_year TEXT,
                term TEXT,
                term_gpa DOUBLE
            )
            """
        )
        con.execute(
            """
            CREATE TABLE identity_crosswalk_integration (
                student_id TEXT,
                sis_user_id TEXT,
                erp_person_id TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE financial_aid_wide (
                erp_person_id TEXT,
                academic_year TEXT,
                term TEXT,
                pell_amount DOUBLE,
                unmet_need DOUBLE
            )
            """
        )
        con.execute(
            """
            CREATE TABLE lms_activity_wide (
                sis_user_id TEXT,
                academic_year TEXT,
                term TEXT,
                login_count DOUBLE,
                page_views DOUBLE
            )
            """
        )

        con.executemany(
            "INSERT INTO sis_enrollments VALUES (?, ?, ?, ?)",
            [
                ("S1", "2024-2025", "Fall", 3.2),
                ("S2", "2024-2025", "Fall", 3.4),
            ],
        )
        if fragmented:
            con.executemany(
                "INSERT INTO identity_crosswalk_integration VALUES (?, ?, ?)",
                [("S1", "sis_s1", "ERP1")],
            )
            con.executemany(
                "INSERT INTO financial_aid_wide VALUES (?, ?, ?, ?, ?)",
                [("ERP1", "2024-2025", "Fall", 1000.0, 500.0)],
            )
            con.executemany(
                "INSERT INTO lms_activity_wide VALUES (?, ?, ?, ?, ?)",
                [("sis_s1", "2024-2025", "Fall", 40.0, 400.0)],
            )
        else:
            con.executemany(
                "INSERT INTO identity_crosswalk_integration VALUES (?, ?, ?)",
                [
                    ("S1", "sis_s1", "ERP1"),
                    ("S2", "sis_s2", "ERP2"),
                ],
            )
            con.executemany(
                "INSERT INTO financial_aid_wide VALUES (?, ?, ?, ?, ?)",
                [
                    ("ERP1", "2024-2025", "Fall", 1000.0, 500.0),
                    ("ERP2", "2024-2025", "Fall", 800.0, 200.0),
                ],
            )
            con.executemany(
                "INSERT INTO lms_activity_wide VALUES (?, ?, ?, ?, ?)",
                [
                    ("sis_s1", "2024-2025", "Fall", 40.0, 400.0),
                    ("sis_s2", "2024-2025", "Fall", 25.0, 220.0),
                ],
            )
        con.close()

    def test_scorer_increases_for_fragmented_join(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            baseline_db = tmp_path / "baseline.duckdb"
            target_db = tmp_path / "target.duckdb"

            self._create_db(baseline_db, fragmented=False)
            self._create_db(target_db, fragmented=True)

            target_qrl = QueryReceiptLayer(str(target_db))
            baseline_qrl = QueryReceiptLayer(str(baseline_db))
            scorer = FragmentationScorer(target_qrl=target_qrl, baseline_qrl=baseline_qrl)

            query_spec = QuerySpec(
                name="q_join_health",
                anchor_table="sis_enrollments",
                sql="""
                SELECT
                    s.student_id,
                    s.academic_year,
                    s.term,
                    f.pell_amount
                FROM sis_enrollments AS s
                LEFT JOIN identity_crosswalk_integration AS x
                    ON x.student_id = s.student_id
                LEFT JOIN financial_aid_wide AS f
                    ON f.erp_person_id = x.erp_person_id
                   AND f.academic_year = s.academic_year
                   AND (f.term = s.term OR f.term IS NULL)
                ORDER BY s.student_id
                """,
                required_output_attrs=("pell_amount",),
                semantic_mappings=1,
                joins=(
                    JoinSpec(
                        name="sis_to_crosswalk",
                        left_relation_sql="SELECT student_id FROM sis_enrollments",
                        right_relation_sql="SELECT student_id FROM identity_crosswalk_integration",
                        join_condition_sql="l.student_id = r.student_id",
                        left_key_exprs=("l.student_id",),
                    ),
                    JoinSpec(
                        name="crosswalk_to_financial",
                        left_relation_sql="""
                        SELECT
                            s.student_id,
                            s.academic_year,
                            s.term,
                            x.erp_person_id
                        FROM sis_enrollments AS s
                        LEFT JOIN identity_crosswalk_integration AS x
                            ON x.student_id = s.student_id
                        """,
                        right_relation_sql="""
                        SELECT erp_person_id, academic_year, term
                        FROM financial_aid_wide
                        """,
                        join_condition_sql=(
                            "l.erp_person_id = r.erp_person_id "
                            "AND l.academic_year = r.academic_year "
                            "AND (l.term = r.term OR r.term IS NULL)"
                        ),
                        left_key_exprs=("l.student_id", "l.academic_year", "l.term"),
                    ),
                ),
            )

            result = scorer.score_query(query_spec=query_spec, frag_level="test", return_result=False)
            self.assertGreater(result.metrics["JML"], 0.0)
            self.assertGreater(result.metrics["MNS"], 0.0)
            self.assertGreater(result.metrics["fragmentation_score"], 0.0)

            receipt_count = target_qrl.con.execute(
                "SELECT COUNT(*) FROM fragmentation_receipts"
            ).fetchone()[0]
            self.assertEqual(receipt_count, 1)

            target_qrl.close()
            baseline_qrl.close()

    def test_ccl_not_inflated_by_duplicate_left_rows_for_one_to_one_join(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            baseline_db = tmp_path / "baseline.duckdb"
            target_db = tmp_path / "target.duckdb"

            self._create_db(baseline_db, fragmented=False)
            self._create_db(target_db, fragmented=False)

            target_qrl = QueryReceiptLayer(str(target_db))
            baseline_qrl = QueryReceiptLayer(str(baseline_db))
            scorer = FragmentationScorer(target_qrl=target_qrl, baseline_qrl=baseline_qrl)

            query_spec = QuerySpec(
                name="q_crosswalk_cardinality",
                anchor_table="sis_enrollments",
                sql="""
                SELECT
                    s.student_id,
                    x.erp_person_id
                FROM sis_enrollments AS s
                LEFT JOIN identity_crosswalk_integration AS x
                    ON x.student_id = s.student_id
                ORDER BY s.student_id
                """,
                joins=(
                    JoinSpec(
                        name="sis_to_crosswalk",
                        left_relation_sql="SELECT student_id FROM sis_enrollments",
                        right_relation_sql="SELECT student_id, erp_person_id FROM identity_crosswalk_integration",
                        join_condition_sql="l.student_id = r.student_id",
                        left_key_exprs=("l.student_id",),
                    ),
                ),
            )

            result = scorer.score_query(query_spec=query_spec, frag_level="test", return_result=False)
            self.assertAlmostEqual(result.metrics["CCL"], 0.0, places=8)

            target_qrl.close()
            baseline_qrl.close()

    def test_missingness_sql_measures_pre_aggregation_null_rates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            baseline_db = tmp_path / "baseline.duckdb"
            target_db = tmp_path / "target.duckdb"

            self._create_db(baseline_db, fragmented=False)
            self._create_db(target_db, fragmented=True)

            target_qrl = QueryReceiptLayer(str(target_db))
            baseline_qrl = QueryReceiptLayer(str(baseline_db))
            scorer = FragmentationScorer(target_qrl=target_qrl, baseline_qrl=baseline_qrl)

            query_spec = QuerySpec(
                name="q_aggregated_missingness",
                anchor_table="sis_enrollments",
                sql="""
                SELECT
                    s.academic_year,
                    s.term,
                    AVG(f.pell_amount) AS avg_pell_amount
                FROM sis_enrollments AS s
                LEFT JOIN identity_crosswalk_integration AS x
                    ON x.student_id = s.student_id
                LEFT JOIN financial_aid_wide AS f
                    ON f.erp_person_id = x.erp_person_id
                   AND f.academic_year = s.academic_year
                   AND (f.term = s.term OR f.term IS NULL)
                GROUP BY s.academic_year, s.term
                ORDER BY s.academic_year, s.term
                """,
                required_output_attrs=("avg_pell_amount",),
                missingness_sql="""
                SELECT
                    s.student_id,
                    s.academic_year,
                    s.term,
                    f.pell_amount
                FROM sis_enrollments AS s
                LEFT JOIN identity_crosswalk_integration AS x
                    ON x.student_id = s.student_id
                LEFT JOIN financial_aid_wide AS f
                    ON f.erp_person_id = x.erp_person_id
                   AND f.academic_year = s.academic_year
                   AND (f.term = s.term OR f.term IS NULL)
                """,
                missingness_attrs=("pell_amount",),
                joins=(
                    JoinSpec(
                        name="crosswalk_to_financial",
                        left_relation_sql="""
                        SELECT
                            s.student_id,
                            s.academic_year,
                            s.term,
                            x.erp_person_id
                        FROM sis_enrollments AS s
                        LEFT JOIN identity_crosswalk_integration AS x
                            ON x.student_id = s.student_id
                        """,
                        right_relation_sql="""
                        SELECT erp_person_id, academic_year, term
                        FROM financial_aid_wide
                        """,
                        join_condition_sql=(
                            "l.erp_person_id = r.erp_person_id "
                            "AND l.academic_year = r.academic_year "
                            "AND (l.term = r.term OR r.term IS NULL)"
                        ),
                        left_key_exprs=("l.student_id", "l.academic_year", "l.term"),
                    ),
                ),
            )

            result = scorer.score_query(query_spec=query_spec, frag_level="test", return_result=False)
            self.assertGreater(result.metrics["MNS"], 0.0)

            target_qrl.close()
            baseline_qrl.close()


if __name__ == "__main__":
    unittest.main()
