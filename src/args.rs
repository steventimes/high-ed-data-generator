use clap::{Parser, ValueEnum};
use std::path::PathBuf;

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum SchemaVersion {
    Slim,
    Wide,
    Both,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum TermCodeStyle {
    Packed,
    Split,
    Both,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
#[value(rename_all = "snake_case")]
pub enum MissingnessPattern {
    Mcar,
    MarByTerm,
    MarByStudentGroup,
    SystemOutageBurst,
}

#[derive(Parser, Debug, Clone)]
#[command(
    name = "higher-ed-synth",
    about = "Generate semester-fragmented synthetic higher-ed administrative datasets"
)]
pub struct Args {
    /// Number of students (SIS population)
    #[arg(long, default_value_t = 200)]
    pub students: usize,

    /// Start term code like 2023FA, 2024SP, 2024SU
    #[arg(long, default_value = "2023FA")]
    pub start_term: String,

    /// Number of sequential terms to generate (FA->SP->SU->FA cycle)
    #[arg(long, default_value_t = 4)]
    pub terms: usize,

    /// RNG seed for deterministic output
    #[arg(long, default_value_t = 42)]
    pub seed: u64,

    /// Output directory
    #[arg(long, default_value = "./out")]
    pub out_dir: PathBuf,

    /// Probability a student changes major in a given term when enrolled
    #[arg(long, default_value_t = 0.06)]
    pub major_change_rate: f64,

    /// Probability of term-to-term stopout transition
    #[arg(long, default_value_t = 0.05)]
    pub stopout_rate: f64,

    /// Probability of re-enrollment for stopped-out students
    #[arg(long, default_value_t = 0.40)]
    pub reenroll_after_stopout_rate: f64,

    /// Probability of withdrawal in a term, separate from stopout
    #[arg(long, default_value_t = 0.03)]
    pub withdrawal_rate: f64,

    /// Probability of transfer-out transition in a term
    #[arg(long, default_value_t = 0.015)]
    pub transfer_out_rate: f64,

    /// Probability of expected LMS data being absent for an enrolled student-term
    #[arg(long, default_value_t = 0.08)]
    pub lms_missing_rate: f64,

    /// Probability of expected aid data being absent for an enrolled student-term
    #[arg(long, default_value_t = 0.20)]
    pub fin_missing_rate: f64,

    /// Probability a student has a local hold event in a term
    #[arg(long, default_value_t = 0.10)]
    pub hold_rate: f64,

    /// Probability of cross-system key mismatch in crosswalk output
    #[arg(long, default_value_t = 0.01)]
    pub crosswalk_mismatch_rate: f64,

    /// Probability of missing linkage identifiers in crosswalk output
    #[arg(long, default_value_t = 0.01)]
    pub identifier_missing_rate: f64,

    /// Typical hold-clearance lag in days
    #[arg(long, default_value_t = 14)]
    pub hold_clearance_lag_days: u32,

    /// Probability a student has an aid application process in a term
    #[arg(long, default_value_t = 0.70)]
    pub aid_application_rate: f64,

    /// Controls whether packed term code appears in wide tables
    #[arg(long, value_enum, default_value_t = TermCodeStyle::Both)]
    pub term_code_style: TermCodeStyle,

    /// Missingness pattern for LMS and financial aid coverage
    #[arg(long, value_enum, default_value_t = MissingnessPattern::Mcar)]
    pub missingness_pattern: MissingnessPattern,

    /// Pretty-print JSON outputs
    #[arg(long, default_value_t = false)]
    pub pretty_json: bool,

    /// Output schema shape: slim, wide, or both
    #[arg(long, value_enum, default_value_t = SchemaVersion::Both)]
    pub schema_version: SchemaVersion,
}
