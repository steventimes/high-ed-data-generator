use crate::model::{FragmentationOperator, VARIANT_NAMES};
use chrono::{Duration, NaiveDate};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("failed to read config {path}: {source}")]
    Read {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid YAML config: {0}")]
    Yaml(#[from] serde_yaml_bw::Error),
    #[error("invalid benchmark config: {0}")]
    Validation(String),
}

pub type Result<T> = std::result::Result<T, ConfigError>;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BenchmarkConfig {
    pub version: u32,
    pub population: PopulationConfig,
    pub variants: BTreeMap<String, CorruptionConfig>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PopulationConfig {
    pub size: usize,
    pub seed: u64,
    pub academic: AcademicConfig,
    pub financial_aid: FinancialAidConfig,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AcademicConfig {
    pub gpa: GpaConfig,
    pub full_time_probability: f64,
    pub semester: String,
    pub term_anchor_date: NaiveDate,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GpaConfig {
    pub mean: f64,
    pub std: f64,
    pub min: f64,
    pub max: f64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FinancialAidConfig {
    pub zero_probability: f64,
    pub recipient_mean: f64,
    pub gamma_shape: f64,
    pub active_probability: f64,
    pub suspended_probability: f64,
    pub disbursement_offset_days: DayRange,
    #[serde(default = "default_late_publication_delay_days")]
    pub late_publication_delay_days: i64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DayRange {
    pub min: i64,
    pub max: i64,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CorruptionConfig {
    pub drop_row: f64,
    pub null_aid_amount: f64,
    pub null_aid_status: f64,
    pub identifier_mismatch: f64,
    pub publication_delay: f64,
    pub aid_status_code_drift: f64,
}

impl BenchmarkConfig {
    pub fn from_yaml(source: &str) -> Result<Self> {
        let config: Self = serde_yaml_bw::from_str(source)?;
        config.validate()?;
        Ok(config)
    }

    pub fn load(path: &Path) -> Result<Self> {
        let source = fs::read_to_string(path).map_err(|source| ConfigError::Read {
            path: path.display().to_string(),
            source,
        })?;
        Self::from_yaml(&source)
    }

    pub fn validate(&self) -> Result<()> {
        if !matches!(self.version, 1 | 2) {
            return Err(validation("version must be 1 or 2"));
        }
        let population = &self.population;
        if population.size == 0 {
            return Err(validation("population.size must be greater than zero"));
        }
        let gpa = &population.academic.gpa;
        for (name, value) in [
            ("population.academic.gpa.mean", gpa.mean),
            ("population.academic.gpa.std", gpa.std),
            ("population.academic.gpa.min", gpa.min),
            ("population.academic.gpa.max", gpa.max),
        ] {
            validate_finite(name, value)?;
        }
        if gpa.std <= 0.0 {
            return Err(validation(
                "population.academic.gpa.std must be greater than zero",
            ));
        }
        if gpa.min > gpa.max {
            return Err(validation("population.academic.gpa.min must be <= max"));
        }
        if gpa.min < 0.0 || gpa.max > 4.0 {
            return Err(validation(
                "population.academic.gpa bounds must stay within the 0.0..4.0 scale",
            ));
        }
        if gpa.mean < gpa.min || gpa.mean > gpa.max {
            return Err(validation(
                "population.academic.gpa.mean must be within min..max",
            ));
        }
        if population.academic.semester.trim().is_empty() {
            return Err(validation("population.academic.semester must not be empty"));
        }

        validate_probability(
            "population.academic.full_time_probability",
            population.academic.full_time_probability,
        )?;
        let aid = &population.financial_aid;
        validate_probability(
            "population.financial_aid.zero_probability",
            aid.zero_probability,
        )?;
        validate_probability(
            "population.financial_aid.active_probability",
            aid.active_probability,
        )?;
        validate_probability(
            "population.financial_aid.suspended_probability",
            aid.suspended_probability,
        )?;
        if (aid.active_probability + aid.suspended_probability - 1.0).abs() > 1e-9 {
            return Err(validation(
                "financial-aid active and suspended probabilities must sum to 1.0",
            ));
        }
        validate_finite(
            "population.financial_aid.recipient_mean",
            aid.recipient_mean,
        )?;
        validate_finite("population.financial_aid.gamma_shape", aid.gamma_shape)?;
        if aid.recipient_mean <= 0.0 {
            return Err(validation(
                "population.financial_aid.recipient_mean must be greater than zero",
            ));
        }
        if aid.gamma_shape <= 0.0 {
            return Err(validation(
                "population.financial_aid.gamma_shape must be greater than zero",
            ));
        }
        if aid.disbursement_offset_days.min > aid.disbursement_offset_days.max {
            return Err(validation(
                "financial-aid disbursement day min must be <= max",
            ));
        }
        for offset in [
            aid.disbursement_offset_days.min,
            aid.disbursement_offset_days.max,
        ] {
            // 在配置阶段验证日期算术，避免极端偏移量在生成中触发 Chrono panic。
            let duration = Duration::try_days(offset).ok_or_else(|| {
                validation("financial-aid disbursement day offset is out of range")
            })?;
            if population
                .academic
                .term_anchor_date
                .checked_add_signed(duration)
                .is_none()
            {
                return Err(validation(
                    "financial-aid disbursement date is out of range",
                ));
            }
        }
        if aid.late_publication_delay_days <= 0 {
            return Err(validation(
                "financial-aid late publication delay must be greater than zero",
            ));
        }
        let replay_delay = Duration::try_days(aid.late_publication_delay_days)
            .ok_or_else(|| validation("financial-aid late publication delay is out of range"))?;
        let watermark = population
            .academic
            .term_anchor_date
            .checked_add_signed(
                Duration::try_days(aid.disbursement_offset_days.max).ok_or_else(|| {
                    validation("financial-aid disbursement day offset is out of range")
                })?,
            )
            .ok_or_else(|| validation("financial-aid disbursement date is out of range"))?;
        let current_snapshot = watermark
            .checked_add_signed(Duration::days(1))
            .ok_or_else(|| validation("financial-aid current snapshot date is out of range"))?;
        current_snapshot
            .checked_add_signed(replay_delay)
            .ok_or_else(|| {
                validation(
                    "financial-aid late publication delay produces an out-of-range replay date",
                )
            })?;

        let actual = self
            .variants
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        let expected = VARIANT_NAMES.into_iter().collect::<BTreeSet<_>>();
        if actual != expected {
            return Err(validation(format!(
                "variants must be exactly: {}",
                VARIANT_NAMES.join(", ")
            )));
        }
        for (name, corruption) in &self.variants {
            for operator in FragmentationOperator::ALL {
                validate_probability(
                    &format!("variants.{name}.{}", operator.as_str()),
                    corruption.rate(operator),
                )?;
            }
        }
        let baseline = &self.variants["baseline"];
        if FragmentationOperator::ALL
            .into_iter()
            .any(|operator| baseline.rate(operator) != 0.0)
        {
            return Err(validation("baseline corruption rates must all be zero"));
        }
        if self.version == 1 {
            // v1 只保留历史生成语义；启用延迟发布或状态漂移时必须显式升级配置。
            if aid.late_publication_delay_days != default_late_publication_delay_days() {
                return Err(validation(
                    "population.financial_aid.late_publication_delay_days requires config version 2 when it is not 7",
                ));
            }
            for (name, corruption) in &self.variants {
                if corruption.publication_delay != 0.0 {
                    return Err(validation(format!(
                        "variants.{name}.publication_delay requires config version 2"
                    )));
                }
                if corruption.aid_status_code_drift != 0.0 {
                    return Err(validation(format!(
                        "variants.{name}.aid_status_code_drift requires config version 2"
                    )));
                }
            }
        }
        Ok(())
    }
}

impl CorruptionConfig {
    pub fn rate(&self, operator: FragmentationOperator) -> f64 {
        match operator {
            FragmentationOperator::DropRow => self.drop_row,
            FragmentationOperator::NullAidAmount => self.null_aid_amount,
            FragmentationOperator::NullAidStatus => self.null_aid_status,
            FragmentationOperator::IdentifierMismatch => self.identifier_mismatch,
            FragmentationOperator::PublicationDelay => self.publication_delay,
            FragmentationOperator::AidStatusCodeDrift => self.aid_status_code_drift,
        }
    }
}

fn validate_probability(name: &str, value: f64) -> Result<()> {
    if !(0.0..=1.0).contains(&value) {
        return Err(validation(format!("{name} must be between 0.0 and 1.0")));
    }
    Ok(())
}

fn validate_finite(name: &str, value: f64) -> Result<()> {
    if !value.is_finite() {
        return Err(validation(format!("{name} must be finite")));
    }
    Ok(())
}

fn validation(message: impl Into<String>) -> ConfigError {
    ConfigError::Validation(message.into())
}

fn default_late_publication_delay_days() -> i64 {
    7
}
