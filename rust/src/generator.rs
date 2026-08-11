use crate::config::{BenchmarkConfig, PopulationConfig};
use crate::model::{
    AcademicRecord, AidStatus, BaselinePopulation, EnrollmentStatus, FinancialAidRecord,
    FragmentationOperator, FragmentedVariant, GeneratedRun, VARIANT_NAMES,
};
use chrono::Duration;
use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha20Rng;
use rand_distr::{Distribution, Gamma, Normal};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum GeneratorError {
    #[error("invalid generator input: {0}")]
    Validation(String),
    #[error("could not construct probability distribution: {0}")]
    Distribution(String),
}

pub type Result<T> = std::result::Result<T, GeneratorError>;

pub fn generate_run(config: &BenchmarkConfig) -> Result<GeneratedRun> {
    config
        .validate()
        .map_err(|error| GeneratorError::Validation(error.to_string()))?;
    let baseline = generate_baseline(&config.population)?;
    let variants = VARIANT_NAMES
        .into_iter()
        .skip(1)
        .map(|name| {
            derive_variant(
                &baseline,
                name,
                &config.variants[name],
                config.population.seed,
            )
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(GeneratedRun { baseline, variants })
}

fn generate_baseline(config: &PopulationConfig) -> Result<BaselinePopulation> {
    let mut rng = ChaCha20Rng::seed_from_u64(config.seed);
    let normal = Normal::new(config.academic.gpa.mean, config.academic.gpa.std)
        .map_err(|error| GeneratorError::Distribution(error.to_string()))?;
    let aid = &config.financial_aid;
    let gamma = Gamma::new(aid.gamma_shape, aid.recipient_mean / aid.gamma_shape)
        .map_err(|error| GeneratorError::Distribution(error.to_string()))?;

    let mut academic_records = Vec::with_capacity(config.size);
    let mut financial_aid_records = Vec::with_capacity(config.size);
    for index in 1..=config.size {
        let student_id = format!("S{index:04}");
        let gpa = round_cents(
            normal
                .sample(&mut rng)
                .clamp(config.academic.gpa.min, config.academic.gpa.max),
        );
        let enrollment_status = if rng.gen_bool(config.academic.full_time_probability) {
            EnrollmentStatus::FullTime
        } else {
            EnrollmentStatus::PartTime
        };
        academic_records.push(AcademicRecord {
            student_id: student_id.clone(),
            gpa,
            enrollment_status,
            semester: config.academic.semester.clone(),
        });

        let (aid_amount, aid_status) = if rng.gen_bool(aid.zero_probability) {
            (0.0, AidStatus::None)
        } else {
            let amount = round_cents(gamma.sample(&mut rng));
            let status = if rng.gen_bool(aid.active_probability) {
                AidStatus::Active
            } else {
                AidStatus::Suspended
            };
            (amount, status)
        };
        let offset =
            rng.gen_range(aid.disbursement_offset_days.min..=aid.disbursement_offset_days.max);
        let duration = Duration::try_days(offset).ok_or_else(|| {
            GeneratorError::Validation("disbursement day offset is out of range".to_string())
        })?;
        let disbursement_date = config
            .academic
            .term_anchor_date
            .checked_add_signed(duration)
            .ok_or_else(|| {
                GeneratorError::Validation("disbursement date is out of range".to_string())
            })?;
        financial_aid_records.push(FinancialAidRecord {
            student_id,
            aid_amount: Some(aid_amount),
            aid_status: Some(aid_status),
            disbursement_date: Some(disbursement_date),
        });
    }

    let baseline = BaselinePopulation {
        academic_records,
        financial_aid_records,
    };
    validate_baseline(&baseline)?;
    Ok(baseline)
}

fn derive_variant(
    baseline: &BaselinePopulation,
    name: &str,
    corruption: &crate::config::CorruptionConfig,
    baseline_seed: u64,
) -> Result<FragmentedVariant> {
    let corruption_percentages = FragmentationOperator::ALL
        .into_iter()
        .map(|operator| (operator, corruption.rate(operator)))
        .collect::<BTreeMap<_, _>>();

    let mut selected_row_ids = BTreeMap::new();
    for operator in FragmentationOperator::ALL {
        // 每个“变体 × 操作”使用独立派生种子，新增其他操作时不会扰动既有抽样结果。
        let seed = derive_step_seed(baseline_seed, name, operator);
        selected_row_ids.insert(
            operator,
            select_student_ids(&baseline.academic_records, corruption.rate(operator), seed),
        );
    }

    let selected = |operator| {
        selected_row_ids[&operator]
            .iter()
            .map(String::as_str)
            .collect::<HashSet<_>>()
    };
    let dropped = selected(FragmentationOperator::DropRow);
    let null_amount = selected(FragmentationOperator::NullAidAmount);
    let null_status = selected(FragmentationOperator::NullAidStatus);
    let identifier_mismatch = selected(FragmentationOperator::IdentifierMismatch);

    let mut completeness = 0.0;
    let financial_aid_records = baseline
        .financial_aid_records
        .iter()
        .filter_map(|record| {
            let canonical_id = record.student_id.as_str();
            if dropped.contains(canonical_id) {
                return None;
            }
            let mut observed = record.clone();
            if null_amount.contains(canonical_id) {
                observed.aid_amount = None;
            }
            if null_status.contains(canonical_id) {
                observed.aid_status = None;
            }
            let mismatched = identifier_mismatch.contains(canonical_id);
            if mismatched {
                observed.student_id = financial_aid_student_id(canonical_id);
            } else {
                let amount = f64::from(observed.aid_amount.is_some());
                let status = f64::from(observed.aid_status.is_some());
                completeness += (1.0 + amount + status) / 3.0;
            }
            Some(observed)
        })
        .collect::<Vec<_>>();

    // 变体只保存真正发生变化的助学金表；学业表由输出层复用基线文件。
    let fragmentation_score = completeness / baseline.academic_records.len() as f64;
    Ok(FragmentedVariant {
        name: name.to_string(),
        financial_aid_records,
        selected_row_ids,
        corruption_percentages,
        fragmentation_score,
    })
}

fn validate_baseline(baseline: &BaselinePopulation) -> Result<()> {
    if baseline.academic_records.len() != baseline.financial_aid_records.len() {
        return Err(GeneratorError::Validation(
            "baseline must contain one financial-aid row per academic row".to_string(),
        ));
    }
    let academic_ids = baseline
        .academic_records
        .iter()
        .map(|record| record.student_id.as_str())
        .collect::<HashSet<_>>();
    let aid_ids = baseline
        .financial_aid_records
        .iter()
        .map(|record| record.student_id.as_str())
        .collect::<HashSet<_>>();
    if academic_ids != aid_ids {
        return Err(GeneratorError::Validation(
            "baseline student identifiers do not match".to_string(),
        ));
    }
    Ok(())
}

fn derive_step_seed(baseline_seed: u64, variant: &str, operator: FragmentationOperator) -> u64 {
    let digest =
        Sha256::digest(format!("{baseline_seed}{variant}{}", operator.as_str()).as_bytes());
    let mut bytes = [0_u8; 8];
    bytes.copy_from_slice(&digest[..8]);
    u64::from_be_bytes(bytes)
}

fn select_student_ids(records: &[AcademicRecord], rate: f64, seed: u64) -> Vec<String> {
    let count = (records.len() as f64 * rate).round() as usize;
    if count == 0 {
        return Vec::new();
    }
    if count == records.len() {
        return records
            .iter()
            .map(|record| record.student_id.clone())
            .collect();
    }

    // 洗牌轻量索引而不是深拷贝全部字符串；随机交换序列与原实现保持一致。
    let mut indices = (0..records.len()).collect::<Vec<_>>();
    indices.shuffle(&mut ChaCha20Rng::seed_from_u64(seed));
    let mut selected = indices
        .into_iter()
        .take(count)
        .map(|index| records[index].student_id.clone())
        .collect::<Vec<_>>();
    selected.sort();
    selected
}

pub fn financial_aid_student_id(canonical_id: &str) -> String {
    // 部门本地命名空间故意与学校 canonical ID 不同，用于测量直接跨库 join 的损失。
    format!("financial-aid::{canonical_id}")
}

fn round_cents(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}
