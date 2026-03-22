"""Declarative workload specifications for fragmentation scoring.

The default workload targets the schema described in `higher_ed_schema.md`,
including integration-layer linkage via `identity_crosswalk_integration` and
wide bridge tables for cross-system joins.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


ALLOWED_CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}


@dataclass(frozen=True)
class JoinSpec:
    """Declarative metadata for one join edge used in diagnostics."""

    name: str
    left_relation_sql: str
    right_relation_sql: str
    join_condition_sql: str
    left_key_exprs: Sequence[str]
    expected_cardinality: str = "one_to_one"
    expected_rows_sql: Optional[str] = None

    def __post_init__(self) -> None:
        if self.expected_cardinality not in ALLOWED_CARDINALITIES:
            raise ValueError(
                f"Unsupported expected_cardinality={self.expected_cardinality!r}. "
                f"Allowed values: {sorted(ALLOWED_CARDINALITIES)}"
            )
        if not self.left_key_exprs:
            raise ValueError("JoinSpec.left_key_exprs must not be empty.")
        if self.expected_cardinality in {"one_to_many", "many_to_many"} and not self.expected_rows_sql:
            raise ValueError(
                "JoinSpec.expected_rows_sql is required for one_to_many or many_to_many joins."
            )


@dataclass(frozen=True)
class QuerySpec:
    """Declarative metadata and SQL for one scored query."""

    name: str
    sql: str
    joins: Sequence[JoinSpec] = field(default_factory=tuple)
    required_output_attrs: Sequence[str] = field(default_factory=tuple)
    missingness_sql: Optional[str] = None
    missingness_attrs: Sequence[str] = field(default_factory=tuple)
    semantic_mappings: int = 0
    weight: float = 1.0
    anchor_table: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("QuerySpec.name must not be empty.")
        if not self.sql.strip():
            raise ValueError("QuerySpec.sql must not be empty.")
        if self.weight <= 0:
            raise ValueError("QuerySpec.weight must be > 0.")
        if self.semantic_mappings < 0:
            raise ValueError("QuerySpec.semantic_mappings must be >= 0.")
        if self.missingness_sql is not None and not self.missingness_sql.strip():
            raise ValueError("QuerySpec.missingness_sql must not be empty when provided.")


def default_workload() -> List[QuerySpec]:
    """Return the default workload for high-ed fragmentation scoring."""
    return [
        QuerySpec(
            name="q1_enrollment_profile",
            anchor_table="sis_enrollments",
            sql="""
            SELECT
                academic_year,
                term,
                COUNT(*) AS enrolled_students,
                AVG(term_gpa) AS avg_term_gpa
            FROM sis_enrollments
            GROUP BY academic_year, term
            ORDER BY academic_year, term
            """,
            required_output_attrs=("enrolled_students", "avg_term_gpa"),
            semantic_mappings=0,
            weight=1.0,
            joins=(),
        ),
        QuerySpec(
            name="q2_aid_bridge",
            anchor_table="sis_enrollments",
            sql="""
            SELECT
                s.academic_year,
                s.term,
                AVG(f.pell_amount) AS avg_pell_amount,
                AVG(f.unmet_need) AS avg_unmet_need
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
            required_output_attrs=("avg_pell_amount", "avg_unmet_need"),
            missingness_sql="""
            SELECT
                s.student_id,
                s.academic_year,
                s.term,
                f.pell_amount,
                f.unmet_need
            FROM sis_enrollments AS s
            LEFT JOIN identity_crosswalk_integration AS x
                ON x.student_id = s.student_id
            LEFT JOIN financial_aid_wide AS f
                ON f.erp_person_id = x.erp_person_id
               AND f.academic_year = s.academic_year
               AND (f.term = s.term OR f.term IS NULL)
            """,
            missingness_attrs=("pell_amount", "unmet_need"),
            semantic_mappings=1,
            weight=1.2,
            joins=(
                JoinSpec(
                    name="sis_to_crosswalk",
                    left_relation_sql="""
                    SELECT DISTINCT student_id
                    FROM sis_enrollments
                    """,
                    right_relation_sql="""
                    SELECT student_id
                    FROM identity_crosswalk_integration
                    """,
                    join_condition_sql="l.student_id = r.student_id",
                    left_key_exprs=("l.student_id",),
                    expected_cardinality="one_to_one",
                ),
                JoinSpec(
                    name="crosswalk_to_financial_aid",
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
                    expected_cardinality="one_to_one",
                ),
            ),
        ),
        QuerySpec(
            name="q3_lms_bridge",
            anchor_table="sis_enrollments",
            sql="""
            SELECT
                s.academic_year,
                s.term,
                AVG(l.login_count) AS avg_login_count,
                AVG(l.page_views) AS avg_page_views
            FROM sis_enrollments AS s
            LEFT JOIN identity_crosswalk_integration AS x
                ON x.student_id = s.student_id
            LEFT JOIN lms_activity_wide AS l
                ON l.sis_user_id = x.sis_user_id
               AND l.academic_year = s.academic_year
               AND l.term = s.term
            GROUP BY s.academic_year, s.term
            ORDER BY s.academic_year, s.term
            """,
            required_output_attrs=("avg_login_count", "avg_page_views"),
            missingness_sql="""
            SELECT
                s.student_id,
                s.academic_year,
                s.term,
                l.login_count,
                l.page_views
            FROM sis_enrollments AS s
            LEFT JOIN identity_crosswalk_integration AS x
                ON x.student_id = s.student_id
            LEFT JOIN lms_activity_wide AS l
                ON l.sis_user_id = x.sis_user_id
               AND l.academic_year = s.academic_year
               AND l.term = s.term
            """,
            missingness_attrs=("login_count", "page_views"),
            semantic_mappings=1,
            weight=1.2,
            joins=(
                JoinSpec(
                    name="sis_to_crosswalk",
                    left_relation_sql="""
                    SELECT DISTINCT student_id
                    FROM sis_enrollments
                    """,
                    right_relation_sql="""
                    SELECT student_id
                    FROM identity_crosswalk_integration
                    """,
                    join_condition_sql="l.student_id = r.student_id",
                    left_key_exprs=("l.student_id",),
                    expected_cardinality="one_to_one",
                ),
                JoinSpec(
                    name="crosswalk_to_lms_activity",
                    left_relation_sql="""
                    SELECT
                        s.student_id,
                        s.academic_year,
                        s.term,
                        x.sis_user_id
                    FROM sis_enrollments AS s
                    LEFT JOIN identity_crosswalk_integration AS x
                        ON x.student_id = s.student_id
                    """,
                    right_relation_sql="""
                    SELECT sis_user_id, academic_year, term
                    FROM lms_activity_wide
                    """,
                    join_condition_sql=(
                        "l.sis_user_id = r.sis_user_id "
                        "AND l.academic_year = r.academic_year "
                        "AND l.term = r.term"
                    ),
                    left_key_exprs=("l.student_id", "l.academic_year", "l.term"),
                    expected_cardinality="one_to_one",
                ),
            ),
        ),
    ]
