use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;

pub const VARIANT_NAMES: [&str; 4] = [
    "baseline",
    "low_fragmentation",
    "medium_fragmentation",
    "high_fragmentation",
];

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AcademicRecord {
    pub student_id: String,
    pub gpa: f64,
    pub enrollment_status: EnrollmentStatus,
    pub semester: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FinancialAidRecord {
    pub student_id: String,
    pub aid_amount: Option<f64>,
    pub aid_status: Option<AidStatus>,
    pub disbursement_date: Option<NaiveDate>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EnrollmentStatus {
    FullTime,
    PartTime,
}

impl fmt::Display for EnrollmentStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::FullTime => formatter.write_str("full_time"),
            Self::PartTime => formatter.write_str("part_time"),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AidStatus {
    Active,
    Suspended,
    None,
}

impl fmt::Display for AidStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Active => formatter.write_str("active"),
            Self::Suspended => formatter.write_str("suspended"),
            Self::None => formatter.write_str("none"),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
pub enum FragmentationOperator {
    DropRow,
    NullAidAmount,
    NullAidStatus,
    IdentifierMismatch,
}

impl FragmentationOperator {
    pub const ALL: [Self; 4] = [
        Self::DropRow,
        Self::NullAidAmount,
        Self::NullAidStatus,
        Self::IdentifierMismatch,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::DropRow => "drop_row",
            Self::NullAidAmount => "null_aid_amount",
            Self::NullAidStatus => "null_aid_status",
            Self::IdentifierMismatch => "identifier_mismatch",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct BaselinePopulation {
    pub academic_records: Vec<AcademicRecord>,
    pub financial_aid_records: Vec<FinancialAidRecord>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct FragmentedVariant {
    pub name: String,
    pub financial_aid_records: Vec<FinancialAidRecord>,
    pub selected_row_ids: BTreeMap<FragmentationOperator, Vec<String>>,
    pub corruption_percentages: BTreeMap<FragmentationOperator, f64>,
    pub fragmentation_score: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct GeneratedRun {
    pub baseline: BaselinePopulation,
    pub variants: Vec<FragmentedVariant>,
}
