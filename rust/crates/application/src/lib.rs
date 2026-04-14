use fragmentation_domain::{
    derive_variant, generate_baseline, validate_baseline_parameters, validate_variant_spec,
    BaselinePopulation, DomainError, ExperimentSpec, FragmentedVariant, VARIANT_NAMES,
};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ApplicationError {
    #[error(transparent)]
    Domain(#[from] DomainError),
    #[error("experiment is missing required variant {0}")]
    MissingVariant(String),
}

pub type Result<T> = std::result::Result<T, ApplicationError>;

#[derive(Clone, Debug)]
pub struct GeneratedRun {
    pub baseline: BaselinePopulation,
    pub variants: Vec<FragmentedVariant>,
}

pub fn build_run(spec: &ExperimentSpec) -> Result<GeneratedRun> {
    validate_baseline_parameters(&spec.baseline)?;
    for name in VARIANT_NAMES {
        let variant = spec
            .variants
            .get(name)
            .ok_or_else(|| ApplicationError::MissingVariant(name.to_string()))?;
        validate_variant_spec(variant)?;
    }

    let baseline = generate_baseline(&spec.baseline)?;
    let mut variants = Vec::with_capacity(VARIANT_NAMES.len());
    for name in VARIANT_NAMES {
        let variant = spec
            .variants
            .get(name)
            .ok_or_else(|| ApplicationError::MissingVariant(name.to_string()))?;
        variants.push(derive_variant(
            &baseline,
            variant,
            spec.baseline.baseline_seed,
        )?);
    }

    Ok(GeneratedRun { baseline, variants })
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    use fragmentation_domain::{BaselineParameters, FragmentationOperator, VariantSpec};
    use std::collections::BTreeMap;

    fn spec() -> ExperimentSpec {
        let baseline = BaselineParameters {
            population_size: 20,
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
        };

        let mut variants = BTreeMap::new();
        variants.insert(
            "baseline".to_string(),
            VariantSpec {
                name: "baseline".to_string(),
                corruption: BTreeMap::new(),
            },
        );
        for (name, rate) in [
            ("low_fragmentation", 0.1),
            ("medium_fragmentation", 0.2),
            ("high_fragmentation", 0.3),
        ] {
            let mut corruption = BTreeMap::new();
            corruption.insert(FragmentationOperator::DropRow, rate);
            corruption.insert(FragmentationOperator::NullAidAmount, rate / 2.0);
            corruption.insert(FragmentationOperator::NullAidStatus, rate / 2.0);
            variants.insert(
                name.to_string(),
                VariantSpec {
                    name: name.to_string(),
                    corruption,
                },
            );
        }

        ExperimentSpec {
            baseline,
            variants,
            at_risk_gpa_threshold: 2.5,
        }
    }

    #[test]
    fn generated_variants_keep_academic_records_fixed() {
        let run = build_run(&spec()).unwrap();
        for variant in &run.variants {
            assert_eq!(variant.academic_records, run.baseline.academic_records);
        }
    }
}
