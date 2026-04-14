use chrono::{Duration, NaiveDate};
use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha20Rng;
use rand_distr::{Distribution, Gamma, Normal};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use thiserror::Error;

pub const VARIANT_NAMES: [&str; 4] = [
    "baseline",
    "low_fragmentation",
    "medium_fragmentation",
    "high_fragmentation",
];

pub const VERIFICATION_STATUSES: [&str; 4] = [
    "source_verified",
    "derived_from_sources",
    "experiment_assumption",
    "scenario_constant",
];

#[derive(Debug, Error)]
pub enum DomainError {
    #[error("invalid domain configuration: {0}")]
    Validation(String),
    #[error("could not construct distribution: {0}")]
    Distribution(String),
}

pub type Result<T> = std::result::Result<T, DomainError>;

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
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EnrollmentStatus::FullTime => f.write_str("full_time"),
            EnrollmentStatus::PartTime => f.write_str("part_time"),
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
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AidStatus::Active => f.write_str("active"),
            AidStatus::Suspended => f.write_str("suspended"),
            AidStatus::None => f.write_str("none"),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FragmentationOperator {
    DropRow,
    NullAidAmount,
    NullAidStatus,
}

impl FragmentationOperator {
    pub const ALL: [FragmentationOperator; 3] = [
        FragmentationOperator::DropRow,
        FragmentationOperator::NullAidAmount,
        FragmentationOperator::NullAidStatus,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            FragmentationOperator::DropRow => "drop_row",
            FragmentationOperator::NullAidAmount => "null_aid_amount",
            FragmentationOperator::NullAidStatus => "null_aid_status",
        }
    }
}

impl fmt::Display for FragmentationOperator {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct BaselineParameters {
    pub population_size: usize,
    pub baseline_seed: u64,
    pub gpa_mean: f64,
    pub gpa_std: f64,
    pub gpa_min: f64,
    pub gpa_max: f64,
    pub full_time_probability: f64,
    pub semester: String,
    pub aid_zero_probability: f64,
    pub aid_recipient_mean: f64,
    pub aid_gamma_shape: f64,
    pub aid_active_probability: f64,
    pub aid_suspended_probability: f64,
    pub term_anchor_date: NaiveDate,
    pub disbursement_offset_days_min: i64,
    pub disbursement_offset_days_max: i64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ExperimentSpec {
    pub baseline: BaselineParameters,
    pub variants: BTreeMap<String, VariantSpec>,
    pub at_risk_gpa_threshold: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct VariantSpec {
    pub name: String,
    pub corruption: BTreeMap<FragmentationOperator, f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct BaselinePopulation {
    pub academic_records: Vec<AcademicRecord>,
    pub financial_aid_records: Vec<FinancialAidRecord>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct FragmentedVariant {
    pub name: String,
    pub academic_records: Vec<AcademicRecord>,
    pub financial_aid_records: Vec<FinancialAidRecord>,
    pub selected_row_ids: BTreeMap<FragmentationOperator, Vec<String>>,
    pub corruption_percentages: BTreeMap<FragmentationOperator, f64>,
    pub fragmentation_score: f64,
}

pub fn validate_baseline_parameters(params: &BaselineParameters) -> Result<()> {
    if params.population_size == 0 {
        return Err(DomainError::Validation(
            "population_size must be greater than zero".to_string(),
        ));
    }
    if params.gpa_min > params.gpa_max {
        return Err(DomainError::Validation(
            "gpa min must be <= gpa max".to_string(),
        ));
    }
    if params.gpa_std <= 0.0 {
        return Err(DomainError::Validation(
            "gpa std must be greater than zero".to_string(),
        ));
    }
    validate_probability("full_time_probability", params.full_time_probability)?;
    validate_probability("aid_zero_probability", params.aid_zero_probability)?;
    validate_probability("aid_active_probability", params.aid_active_probability)?;
    validate_probability(
        "aid_suspended_probability",
        params.aid_suspended_probability,
    )?;
    let positive_status_sum = params.aid_active_probability + params.aid_suspended_probability;
    if (positive_status_sum - 1.0).abs() > 1e-9 {
        return Err(DomainError::Validation(
            "aid active/suspended probabilities must sum to 1.0".to_string(),
        ));
    }
    if params.aid_recipient_mean <= 0.0 {
        return Err(DomainError::Validation(
            "aid recipient mean must be greater than zero".to_string(),
        ));
    }
    if params.aid_gamma_shape <= 0.0 {
        return Err(DomainError::Validation(
            "aid gamma shape must be greater than zero".to_string(),
        ));
    }
    if params.disbursement_offset_days_min > params.disbursement_offset_days_max {
        return Err(DomainError::Validation(
            "disbursement offset min must be <= offset max".to_string(),
        ));
    }
    Ok(())
}

pub fn validate_probability(name: &str, value: f64) -> Result<()> {
    if !(0.0..=1.0).contains(&value) {
        return Err(DomainError::Validation(format!(
            "{name} must be between 0.0 and 1.0"
        )));
    }
    Ok(())
}

pub fn validate_variant_spec(variant: &VariantSpec) -> Result<()> {
    if !VARIANT_NAMES.contains(&variant.name.as_str()) {
        return Err(DomainError::Validation(format!(
            "unsupported variant name {}",
            variant.name
        )));
    }
    if variant.name == "baseline" && !variant.corruption.is_empty() {
        return Err(DomainError::Validation(
            "baseline variant must not define corruption".to_string(),
        ));
    }
    for (operator, rate) in &variant.corruption {
        if !FragmentationOperator::ALL.contains(operator) {
            return Err(DomainError::Validation(format!(
                "unsupported corruption operator {operator}"
            )));
        }
        validate_probability(operator.as_str(), *rate)?;
    }
    Ok(())
}

pub fn generate_baseline(params: &BaselineParameters) -> Result<BaselinePopulation> {
    validate_baseline_parameters(params)?;
    let mut rng = ChaCha20Rng::seed_from_u64(params.baseline_seed);
    let normal = Normal::new(params.gpa_mean, params.gpa_std)
        .map_err(|err| DomainError::Distribution(err.to_string()))?;
    let gamma_scale = params.aid_recipient_mean / params.aid_gamma_shape;
    let gamma = Gamma::new(params.aid_gamma_shape, gamma_scale)
        .map_err(|err| DomainError::Distribution(err.to_string()))?;

    let mut academic_records = Vec::with_capacity(params.population_size);
    let mut financial_aid_records = Vec::with_capacity(params.population_size);

    for index in 1..=params.population_size {
        let student_id = format_student_id(index);
        let gpa = round_cents(clip(
            normal.sample(&mut rng),
            params.gpa_min,
            params.gpa_max,
        ));
        let enrollment_status = if rng.gen_bool(params.full_time_probability) {
            EnrollmentStatus::FullTime
        } else {
            EnrollmentStatus::PartTime
        };
        academic_records.push(AcademicRecord {
            student_id: student_id.clone(),
            gpa,
            enrollment_status,
            semester: params.semester.clone(),
        });

        let receives_no_aid = rng.gen_bool(params.aid_zero_probability);
        let (aid_amount, aid_status) = if receives_no_aid {
            (0.0, AidStatus::None)
        } else {
            let sampled = round_cents(gamma.sample(&mut rng));
            let status = if rng.gen_bool(params.aid_active_probability) {
                AidStatus::Active
            } else {
                AidStatus::Suspended
            };
            (sampled, status)
        };
        let offset_days = rng
            .gen_range(params.disbursement_offset_days_min..=params.disbursement_offset_days_max);
        financial_aid_records.push(FinancialAidRecord {
            student_id,
            aid_amount: Some(aid_amount),
            aid_status: Some(aid_status),
            disbursement_date: Some(params.term_anchor_date + Duration::days(offset_days)),
        });
    }

    let baseline = BaselinePopulation {
        academic_records,
        financial_aid_records,
    };
    validate_baseline_linkage(&baseline)?;
    Ok(baseline)
}

pub fn validate_baseline_linkage(baseline: &BaselinePopulation) -> Result<()> {
    if baseline.academic_records.len() != baseline.financial_aid_records.len() {
        return Err(DomainError::Validation(
            "clean baseline must have one financial aid row per academic row".to_string(),
        ));
    }
    let academic_ids: HashSet<&str> = baseline
        .academic_records
        .iter()
        .map(|record| record.student_id.as_str())
        .collect();
    let aid_ids: HashSet<&str> = baseline
        .financial_aid_records
        .iter()
        .map(|record| record.student_id.as_str())
        .collect();
    if academic_ids != aid_ids {
        return Err(DomainError::Validation(
            "baseline financial aid student ids must exactly match academic ids".to_string(),
        ));
    }
    for record in &baseline.financial_aid_records {
        if record.aid_amount.is_none()
            || record.aid_status.is_none()
            || record.disbursement_date.is_none()
        {
            return Err(DomainError::Validation(
                "baseline financial aid fields must be complete".to_string(),
            ));
        }
    }
    Ok(())
}

pub fn derive_variant(
    baseline: &BaselinePopulation,
    variant: &VariantSpec,
    baseline_seed: u64,
) -> Result<FragmentedVariant> {
    validate_variant_spec(variant)?;
    let academic_records = baseline.academic_records.clone();
    let student_ids = academic_records
        .iter()
        .map(|record| record.student_id.clone())
        .collect::<Vec<_>>();
    let corruption_percentages = corruption_rates_with_zeroes(&variant.corruption);
    let mut selected_row_ids = BTreeMap::new();
    for operator in FragmentationOperator::ALL {
        let rate = *corruption_percentages.get(&operator).unwrap_or(&0.0);
        let seed = derive_step_seed_u64(baseline_seed, &variant.name, operator);
        selected_row_ids.insert(operator, select_student_ids(&student_ids, rate, seed)?);
    }

    let drop_rows: HashSet<String> = selected_row_ids
        .get(&FragmentationOperator::DropRow)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .collect();
    let null_amount_rows: HashSet<String> = selected_row_ids
        .get(&FragmentationOperator::NullAidAmount)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .collect();
    let null_status_rows: HashSet<String> = selected_row_ids
        .get(&FragmentationOperator::NullAidStatus)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .collect();

    let mut financial_aid_records = Vec::with_capacity(baseline.financial_aid_records.len());
    for record in &baseline.financial_aid_records {
        if drop_rows.contains(&record.student_id) {
            continue;
        }
        let mut observed = record.clone();
        if null_amount_rows.contains(&record.student_id) {
            observed.aid_amount = None;
        }
        if null_status_rows.contains(&record.student_id) {
            observed.aid_status = None;
        }
        financial_aid_records.push(observed);
    }

    let fragmentation_score = fragmentation_score(&academic_records, &financial_aid_records)?;
    Ok(FragmentedVariant {
        name: variant.name.clone(),
        academic_records,
        financial_aid_records,
        selected_row_ids,
        corruption_percentages,
        fragmentation_score,
    })
}

pub fn derive_step_seed_u64(baseline_seed: u64, level: &str, step: FragmentationOperator) -> u64 {
    let input = format!("{baseline_seed}{level}{}", step.as_str());
    let digest = Sha256::digest(input.as_bytes());
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&digest[..8]);
    u64::from_be_bytes(bytes)
}

pub fn select_student_ids(student_ids: &[String], rate: f64, seed: u64) -> Result<Vec<String>> {
    validate_probability("corruption rate", rate)?;
    let mut ids = student_ids.to_vec();
    ids.sort();
    let count = ((ids.len() as f64) * rate).round() as usize;
    let mut rng = ChaCha20Rng::seed_from_u64(seed);
    ids.shuffle(&mut rng);
    let mut selected = ids.into_iter().take(count).collect::<Vec<_>>();
    selected.sort();
    Ok(selected)
}

pub fn corruption_rates_with_zeroes(
    rates: &BTreeMap<FragmentationOperator, f64>,
) -> BTreeMap<FragmentationOperator, f64> {
    let mut with_zeroes = BTreeMap::new();
    for operator in FragmentationOperator::ALL {
        with_zeroes.insert(operator, *rates.get(&operator).unwrap_or(&0.0));
    }
    with_zeroes
}

pub fn fragmentation_score(
    academic_records: &[AcademicRecord],
    financial_aid_records: &[FinancialAidRecord],
) -> Result<f64> {
    if academic_records.is_empty() {
        return Err(DomainError::Validation(
            "academic records must not be empty".to_string(),
        ));
    }
    let aid_by_student = financial_aid_records
        .iter()
        .map(|record| (record.student_id.as_str(), record))
        .collect::<HashMap<_, _>>();
    let mut score_sum = 0.0;
    for academic in academic_records {
        if let Some(aid) = aid_by_student.get(academic.student_id.as_str()) {
            let row_exists = 1.0;
            let amount_present = if aid.aid_amount.is_some() { 1.0 } else { 0.0 };
            let status_present = if aid.aid_status.is_some() { 1.0 } else { 0.0 };
            score_sum += (row_exists + amount_present + status_present) / 3.0;
        }
    }
    Ok(score_sum / academic_records.len() as f64)
}

pub fn format_student_id(index: usize) -> String {
    format!("S{index:04}")
}

pub fn clip(value: f64, min: f64, max: f64) -> f64 {
    value.max(min).min(max)
}

pub fn round_cents(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn params() -> BaselineParameters {
        BaselineParameters {
            population_size: 10,
            baseline_seed: 20260410,
            gpa_mean: 2.8,
            gpa_std: 0.6,
            gpa_min: 0.0,
            gpa_max: 4.0,
            full_time_probability: 0.62,
            semester: "Fall 2024".to_string(),
            aid_zero_probability: 0.28,
            aid_recipient_mean: 14100.0,
            aid_gamma_shape: 2.0,
            aid_active_probability: 0.95,
            aid_suspended_probability: 0.05,
            term_anchor_date: NaiveDate::from_ymd_opt(2024, 9, 1).unwrap(),
            disbursement_offset_days_min: -10,
            disbursement_offset_days_max: 30,
        }
    }

    #[test]
    fn clips_gpa_to_bounds() {
        assert_eq!(clip(4.8, 0.0, 4.0), 4.0);
        assert_eq!(clip(-0.5, 0.0, 4.0), 0.0);
    }

    #[test]
    fn zero_inflation_sets_none_status() {
        let mut params = params();
        params.aid_zero_probability = 1.0;
        let baseline = generate_baseline(&params).unwrap();
        assert!(baseline
            .financial_aid_records
            .iter()
            .all(|record| record.aid_amount == Some(0.0)
                && record.aid_status == Some(AidStatus::None)));
    }

    #[test]
    fn deterministic_seed_is_stable_and_step_specific() {
        let a = derive_step_seed_u64(
            20260410,
            "low_fragmentation",
            FragmentationOperator::DropRow,
        );
        let b = derive_step_seed_u64(
            20260410,
            "low_fragmentation",
            FragmentationOperator::DropRow,
        );
        let c = derive_step_seed_u64(
            20260410,
            "low_fragmentation",
            FragmentationOperator::NullAidAmount,
        );
        assert_eq!(a, b);
        assert_ne!(a, c);
    }

    #[test]
    fn corruption_operator_semantics_are_applied() {
        let baseline = generate_baseline(&params()).unwrap();
        let mut corruption = BTreeMap::new();
        corruption.insert(FragmentationOperator::DropRow, 0.2);
        corruption.insert(FragmentationOperator::NullAidAmount, 0.1);
        corruption.insert(FragmentationOperator::NullAidStatus, 0.1);
        let variant = VariantSpec {
            name: "low_fragmentation".to_string(),
            corruption,
        };
        let derived = derive_variant(&baseline, &variant, params().baseline_seed).unwrap();
        assert_eq!(derived.academic_records, baseline.academic_records);
        assert!(derived.financial_aid_records.len() < baseline.financial_aid_records.len());
        assert!(derived
            .financial_aid_records
            .iter()
            .any(|record| record.aid_amount.is_none() || record.aid_status.is_none()));
    }

    #[test]
    fn fragmentation_score_uses_academic_domain() {
        let academic = vec![
            AcademicRecord {
                student_id: "S0001".to_string(),
                gpa: 2.0,
                enrollment_status: EnrollmentStatus::FullTime,
                semester: "Fall 2024".to_string(),
            },
            AcademicRecord {
                student_id: "S0002".to_string(),
                gpa: 3.0,
                enrollment_status: EnrollmentStatus::PartTime,
                semester: "Fall 2024".to_string(),
            },
        ];
        let aid = vec![FinancialAidRecord {
            student_id: "S0001".to_string(),
            aid_amount: Some(100.0),
            aid_status: None,
            disbursement_date: Some(NaiveDate::from_ymd_opt(2024, 9, 1).unwrap()),
        }];
        let score = fragmentation_score(&academic, &aid).unwrap();
        assert!((score - (2.0 / 3.0) / 2.0).abs() < 1e-9);
    }
}
