use clap::Parser;
use std::path::PathBuf;

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

    /// Probability a student changes major in a given term (if enrolled)
    #[arg(long, default_value_t = 0.04)]
    pub major_change_rate: f64,

    /// Probability a student stops out after a term
    #[arg(long, default_value_t = 0.03)]
    pub stopout_rate: f64,

    /// Probability an enrolled student is missing from Moodle extract
    #[arg(long, default_value_t = 0.10)]
    pub lms_missing_rate: f64,

    /// Probability an enrolled student is missing from financial aid extract
    #[arg(long, default_value_t = 0.45)]
    pub fin_missing_rate: f64,

    /// Probability a student has an advising hold record in a term
    #[arg(long, default_value_t = 0.12)]
    pub hold_rate: f64,

    /// Probability that some IDs in the crosswalk are wrong/swapped
    #[arg(long, default_value_t = 0.01)]
    pub crosswalk_mismatch_rate: f64,

    /// Pretty-print JSON outputs
    #[arg(long, default_value_t = false)]
    pub pretty_json: bool,
}