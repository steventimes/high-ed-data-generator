use anyhow::{anyhow, Context, Result};
use chrono::NaiveDate;
use fragmentation_application::build_run;
use fragmentation_domain::{
    AidStatus, EnrollmentStatus, ExperimentSpec, FragmentationOperator, VariantSpec, VARIANT_NAMES,
    VERIFICATION_STATUSES,
};
use serde::Serialize;
use serde_yaml::{Mapping, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct RunOptions {
    pub schema_path: PathBuf,
    pub experiment_path: PathBuf,
    pub out_root: PathBuf,
    pub run_id: Option<String>,
    pub overwrite: bool,
}

#[derive(Clone, Debug)]
pub struct CompletedRun {
    pub run_dir: PathBuf,
}

#[derive(Serialize)]
struct AcademicCsvRow<'a> {
    student_id: &'a str,
    gpa: String,
    enrollment_status: String,
    semester: &'a str,
}

#[derive(Serialize)]
struct FinancialAidCsvRow<'a> {
    student_id: &'a str,
    aid_amount: String,
    aid_status: String,
    disbursement_date: String,
}

#[derive(Serialize)]
struct FileHashes {
    academic_records: String,
    financial_aid_records: String,
}

#[derive(Serialize)]
struct InvariantManifest {
    mutate_academic_records: bool,
    regenerate_population_per_variant: bool,
    corruption_applies_only_to: String,
}

#[derive(Serialize)]
struct VariantManifest {
    manifest_version: u32,
    variant: String,
    baseline_dataset_id: String,
    baseline_file_hashes: FileHashes,
    variant_file_hashes: FileHashes,
    random_seed: u64,
    corruption_percentages: BTreeMap<String, f64>,
    selected_row_ids: BTreeMap<String, Vec<String>>,
    fragmentation_score: f64,
    invariants: InvariantManifest,
}

pub fn execute_run(options: &RunOptions) -> Result<CompletedRun> {
    let spec = load_configs(&options.schema_path, &options.experiment_path)?;
    let generated = build_run(&spec)?;
    let run_id = options
        .run_id
        .clone()
        .unwrap_or_else(|| format!("seed_{}", spec.baseline.baseline_seed));
    let run_dir = options.out_root.join(run_id);

    if run_dir.exists() {
        if options.overwrite {
            fs::remove_dir_all(&run_dir)
                .with_context(|| format!("removing existing run dir {}", run_dir.display()))?;
        } else {
            return Err(anyhow!(
                "run dir already exists: {} (pass --overwrite to replace it)",
                run_dir.display()
            ));
        }
    }

    fs::create_dir_all(run_dir.join("config_snapshot"))?;
    fs::create_dir_all(run_dir.join("variants"))?;
    fs::create_dir_all(run_dir.join("manifests"))?;

    fs::copy(
        &options.schema_path,
        run_dir.join("config_snapshot").join("schema_registry.yaml"),
    )
    .with_context(|| "copying schema config snapshot")?;
    fs::copy(
        &options.experiment_path,
        run_dir.join("config_snapshot").join("experiment.yaml"),
    )
    .with_context(|| "copying experiment config snapshot")?;

    let baseline_variant_dir = run_dir.join("variants").join("baseline");
    fs::create_dir_all(&baseline_variant_dir)?;
    let baseline_academic_path = baseline_variant_dir.join("academic_records.csv");
    let baseline_aid_path = baseline_variant_dir.join("financial_aid_records.csv");
    write_academic_csv(
        &baseline_academic_path,
        &generated.baseline.academic_records,
    )?;
    write_financial_aid_csv(
        &baseline_aid_path,
        &generated.baseline.financial_aid_records,
    )?;
    let baseline_hashes = FileHashes {
        academic_records: file_sha256(&baseline_academic_path)?,
        financial_aid_records: file_sha256(&baseline_aid_path)?,
    };
    let baseline_dataset_id = baseline_dataset_id(
        spec.baseline.baseline_seed,
        &baseline_hashes.academic_records,
        &baseline_hashes.financial_aid_records,
    );

    for variant in generated.variants {
        let variant_dir = run_dir.join("variants").join(&variant.name);
        let academic_path = variant_dir.join("academic_records.csv");
        let aid_path = variant_dir.join("financial_aid_records.csv");
        let variant_hashes = if variant.name == "baseline" {
            FileHashes {
                academic_records: baseline_hashes.academic_records.clone(),
                financial_aid_records: baseline_hashes.financial_aid_records.clone(),
            }
        } else {
            fs::create_dir_all(&variant_dir)?;
            write_academic_csv(&academic_path, &variant.academic_records)?;
            write_financial_aid_csv(&aid_path, &variant.financial_aid_records)?;
            FileHashes {
                academic_records: file_sha256(&academic_path)?,
                financial_aid_records: file_sha256(&aid_path)?,
            }
        };
        let manifest = VariantManifest {
            manifest_version: 1,
            variant: variant.name.clone(),
            baseline_dataset_id: baseline_dataset_id.clone(),
            baseline_file_hashes: FileHashes {
                academic_records: baseline_hashes.academic_records.clone(),
                financial_aid_records: baseline_hashes.financial_aid_records.clone(),
            },
            variant_file_hashes: variant_hashes,
            random_seed: spec.baseline.baseline_seed,
            corruption_percentages: stringify_operator_map(&variant.corruption_percentages),
            selected_row_ids: stringify_selection_map(&variant.selected_row_ids),
            fragmentation_score: variant.fragmentation_score,
            invariants: InvariantManifest {
                mutate_academic_records: false,
                regenerate_population_per_variant: false,
                corruption_applies_only_to: "financial_aid_records".to_string(),
            },
        };
        write_json(
            &run_dir
                .join("manifests")
                .join(format!("{}_manifest.json", variant.name)),
            &manifest,
        )?;
    }

    Ok(CompletedRun { run_dir })
}

pub fn load_configs(schema_path: &Path, experiment_path: &Path) -> Result<ExperimentSpec> {
    let schema_yaml = fs::read_to_string(schema_path)
        .with_context(|| format!("reading schema config {}", schema_path.display()))?;
    let experiment_yaml = fs::read_to_string(experiment_path)
        .with_context(|| format!("reading experiment config {}", experiment_path.display()))?;
    let schema: Value = serde_yaml::from_str(&schema_yaml).context("parsing schema YAML")?;
    let experiment: Value =
        serde_yaml::from_str(&experiment_yaml).context("parsing experiment YAML")?;
    validate_verification_labels(&schema, "$.schema_registry")?;
    parse_experiment_spec(&schema, &experiment)
}

fn parse_experiment_spec(schema: &Value, experiment: &Value) -> Result<ExperimentSpec> {
    let schema_root = mapping(schema, "$.schema_registry")?;
    expect_u64(schema_root, "schema_version", 1)?;
    let experiment_root = mapping(experiment, "$.experiment")?;
    expect_u64(experiment_root, "experiment_version", 1)?;

    validate_schema_shape(schema_root)?;
    validate_experiment_invariants(experiment_root)?;

    let academic = entity_columns(schema_root, "academic_records")?;
    let aid = entity_columns(schema_root, "financial_aid_records")?;

    let gpa_column = child_mapping(academic, "gpa", "academic_records.gpa")?;
    let gpa_distribution = child_mapping(gpa_column, "distribution", "gpa.distribution")?;
    expect_string_value(gpa_distribution, "family", "clipped_normal")?;
    let enrollment_column = child_mapping(
        academic,
        "enrollment_status",
        "academic_records.enrollment_status",
    )?;
    let enrollment_distribution = child_mapping(
        enrollment_column,
        "distribution",
        "enrollment_status.distribution",
    )?;
    expect_string_value(enrollment_distribution, "family", "categorical")?;
    let enrollment_probabilities = mapping(
        key(enrollment_distribution, "probabilities")?,
        "enrollment_status.probabilities",
    )?;
    let semester_column = child_mapping(academic, "semester", "academic_records.semester")?;
    let semester = scalar_string(key(semester_column, "fixed_value")?, "semester.fixed_value")?;
    let term_anchor_date = parse_date(
        key(semester_column, "term_anchor_date")?,
        "semester.term_anchor_date",
    )?;

    let aid_amount_column = child_mapping(aid, "aid_amount", "financial_aid_records.aid_amount")?;
    let aid_amount_distribution =
        child_mapping(aid_amount_column, "distribution", "aid_amount.distribution")?;
    expect_string_value(aid_amount_distribution, "family", "zero_inflated_positive")?;
    expect_string_value(aid_amount_distribution, "positive_family", "gamma")?;

    let aid_status_column = child_mapping(aid, "aid_status", "financial_aid_records.aid_status")?;
    let aid_status_distribution =
        child_mapping(aid_status_column, "distribution", "aid_status.distribution")?;
    expect_string_value(
        aid_status_distribution,
        "family",
        "categorical_by_aid_amount",
    )?;
    expect_string_value(aid_status_distribution, "zero_value", "none")?;
    let aid_status_probabilities = mapping(
        key(aid_status_distribution, "positive_probabilities")?,
        "aid_status.positive_probabilities",
    )?;

    let disbursement_column = child_mapping(
        aid,
        "disbursement_date",
        "financial_aid_records.disbursement_date",
    )?;
    let disbursement_distribution = child_mapping(
        disbursement_column,
        "distribution",
        "disbursement_date.distribution",
    )?;
    expect_string_value(disbursement_distribution, "family", "within_term_window")?;
    let disbursement_anchor = parse_date(
        key(disbursement_distribution, "term_anchor_date")?,
        "disbursement_date.term_anchor_date",
    )?;
    if disbursement_anchor != term_anchor_date {
        return Err(anyhow!(
            "semester.term_anchor_date and disbursement_date.term_anchor_date must match"
        ));
    }

    let baseline = fragmentation_domain::BaselineParameters {
        population_size: as_usize(key(experiment_root, "population_size")?, "population_size")?,
        baseline_seed: as_u64(key(experiment_root, "baseline_seed")?, "baseline_seed")?,
        gpa_mean: as_f64(key(gpa_distribution, "mean")?, "gpa.mean")?,
        gpa_std: as_f64(key(gpa_distribution, "std")?, "gpa.std")?,
        gpa_min: as_f64(key(gpa_distribution, "min")?, "gpa.min")?,
        gpa_max: as_f64(key(gpa_distribution, "max")?, "gpa.max")?,
        full_time_probability: as_f64(
            key(enrollment_probabilities, "full_time")?,
            "enrollment_status.probabilities.full_time",
        )?,
        semester,
        aid_zero_probability: as_f64(
            key(aid_amount_distribution, "zero_probability")?,
            "aid_amount.zero_probability",
        )?,
        aid_recipient_mean: as_f64(
            key(aid_amount_distribution, "recipient_mean")?,
            "aid_amount.recipient_mean",
        )?,
        aid_gamma_shape: as_f64(
            key(aid_amount_distribution, "gamma_shape")?,
            "aid_amount.gamma_shape",
        )?,
        aid_active_probability: as_f64(
            key(aid_status_probabilities, "active")?,
            "aid_status.positive_probabilities.active",
        )?,
        aid_suspended_probability: as_f64(
            key(aid_status_probabilities, "suspended")?,
            "aid_status.positive_probabilities.suspended",
        )?,
        term_anchor_date,
        disbursement_offset_days_min: as_i64(
            key(disbursement_distribution, "offset_days_min")?,
            "disbursement_date.offset_days_min",
        )?,
        disbursement_offset_days_max: as_i64(
            key(disbursement_distribution, "offset_days_max")?,
            "disbursement_date.offset_days_max",
        )?,
    };

    let variants = parse_variants(mapping(key(experiment_root, "variants")?, "variants")?)?;
    let query = mapping(key(experiment_root, "query")?, "query")?;
    Ok(ExperimentSpec {
        baseline,
        variants,
        at_risk_gpa_threshold: as_f64(
            key(query, "at_risk_gpa_threshold")?,
            "query.at_risk_gpa_threshold",
        )?,
    })
}

fn validate_schema_shape(root: &Mapping) -> Result<()> {
    let entities = mapping(key(root, "entities")?, "entities")?;
    let entity_names = map_keys(entities)?;
    expect_exact_names(
        "entities",
        &entity_names,
        &["academic_records", "financial_aid_records"],
    )?;

    let academic = entity_columns(root, "academic_records")?;
    expect_exact_names(
        "academic_records.columns",
        &map_keys(academic)?,
        &["student_id", "gpa", "enrollment_status", "semester"],
    )?;
    let aid = entity_columns(root, "financial_aid_records")?;
    expect_exact_names(
        "financial_aid_records.columns",
        &map_keys(aid)?,
        &[
            "student_id",
            "aid_amount",
            "aid_status",
            "disbursement_date",
        ],
    )?;
    expect_allowed_values(
        key(academic, "enrollment_status")?,
        "enrollment_status",
        &["full_time", "part_time"],
    )?;
    expect_allowed_values(
        key(aid, "aid_status")?,
        "aid_status",
        &["active", "suspended", "none"],
    )?;
    Ok(())
}

fn validate_experiment_invariants(root: &Mapping) -> Result<()> {
    let invariants = mapping(
        key(root, "baseline_to_fragment_invariant")?,
        "baseline_to_fragment_invariant",
    )?;
    expect_bool_value(invariants, "mutate_academic_records", false)?;
    expect_bool_value(invariants, "regenerate_population_per_variant", false)?;
    expect_string_value(
        invariants,
        "corruption_applies_only_to",
        "financial_aid_records",
    )?;
    Ok(())
}

fn parse_variants(variants: &Mapping) -> Result<BTreeMap<String, VariantSpec>> {
    let variant_names = map_keys(variants)?;
    expect_exact_names("variants", &variant_names, &VARIANT_NAMES)?;

    let mut parsed = BTreeMap::new();
    for variant_name in VARIANT_NAMES {
        let value = key(variants, variant_name)?;
        let variant_map = mapping(value, variant_name)?;
        let corruption_value = key(variant_map, "corruption")?;
        let corruption = parse_corruption(corruption_value, variant_name)?;
        parsed.insert(
            variant_name.to_string(),
            VariantSpec {
                name: variant_name.to_string(),
                corruption,
            },
        );
    }
    Ok(parsed)
}

fn parse_corruption(
    value: &Value,
    variant_name: &str,
) -> Result<BTreeMap<FragmentationOperator, f64>> {
    match value {
        Value::Sequence(items) if items.is_empty() => Ok(BTreeMap::new()),
        Value::Mapping(map) => {
            let mut parsed = BTreeMap::new();
            for (key_value, rate_value) in map {
                let key_text = scalar_string(key_value, "corruption operator")?;
                let operator = match key_text.as_str() {
                    "drop_row" => FragmentationOperator::DropRow,
                    "null_aid_amount" => FragmentationOperator::NullAidAmount,
                    "null_aid_status" => FragmentationOperator::NullAidStatus,
                    other => {
                        return Err(anyhow!(
                            "unsupported corruption operator {other} in {variant_name}"
                        ))
                    }
                };
                parsed.insert(
                    operator,
                    as_f64(rate_value, &format!("{variant_name}.{key_text}"))?,
                );
            }
            Ok(parsed)
        }
        _ => Err(anyhow!(
            "variant {variant_name} corruption must be an empty list or mapping"
        )),
    }
}

fn entity_columns<'a>(root: &'a Mapping, entity_name: &str) -> Result<&'a Mapping> {
    let entities = mapping(key(root, "entities")?, "entities")?;
    let entity = mapping(key(entities, entity_name)?, entity_name)?;
    mapping(key(entity, "columns")?, &format!("{entity_name}.columns"))
}

fn expect_allowed_values(column: &Value, name: &str, expected: &[&str]) -> Result<()> {
    let column_map = mapping(column, name)?;
    let values = sequence(
        key(column_map, "allowed_values")?,
        &format!("{name}.allowed_values"),
    )?;
    let actual = values
        .iter()
        .map(|value| scalar_string(value, &format!("{name}.allowed_values")))
        .collect::<Result<Vec<_>>>()?;
    let expected_set = expected
        .iter()
        .map(|value| value.to_string())
        .collect::<BTreeSet<_>>();
    let actual_set = actual.into_iter().collect::<BTreeSet<_>>();
    if actual_set != expected_set {
        return Err(anyhow!(
            "{name}.allowed_values must be exactly {:?}",
            expected
        ));
    }
    Ok(())
}

fn validate_verification_labels(value: &Value, path: &str) -> Result<()> {
    match value {
        Value::Mapping(map) => {
            for (key_value, child) in map {
                let key_text = scalar_string(key_value, path).unwrap_or_default();
                let child_path = format!("{path}.{key_text}");
                if key_text == "verification_status" {
                    let status = scalar_string(child, &child_path)?;
                    if !VERIFICATION_STATUSES.contains(&status.as_str()) {
                        return Err(anyhow!(
                            "invalid verification_status {status} at {child_path}"
                        ));
                    }
                }
                if key_text == "verification_statuses" {
                    let statuses = mapping(child, &child_path)?;
                    for (status_key, status_value) in statuses {
                        let status_name = scalar_string(status_key, &child_path)?;
                        let status = scalar_string(status_value, &child_path)?;
                        if !VERIFICATION_STATUSES.contains(&status.as_str()) {
                            return Err(anyhow!(
                                "invalid verification status {status} for {status_name} at {child_path}"
                            ));
                        }
                    }
                }
                validate_verification_labels(child, &child_path)?;
            }
        }
        Value::Sequence(items) => {
            for (index, child) in items.iter().enumerate() {
                validate_verification_labels(child, &format!("{path}[{index}]"))?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn write_academic_csv(path: &Path, records: &[fragmentation_domain::AcademicRecord]) -> Result<()> {
    let mut writer =
        csv::Writer::from_path(path).with_context(|| format!("creating {}", path.display()))?;
    for record in records {
        writer.serialize(AcademicCsvRow {
            student_id: &record.student_id,
            gpa: format!("{:.2}", record.gpa),
            enrollment_status: record.enrollment_status.to_string(),
            semester: &record.semester,
        })?;
    }
    writer.flush()?;
    Ok(())
}

fn write_financial_aid_csv(
    path: &Path,
    records: &[fragmentation_domain::FinancialAidRecord],
) -> Result<()> {
    let mut writer =
        csv::Writer::from_path(path).with_context(|| format!("creating {}", path.display()))?;
    for record in records {
        writer.serialize(FinancialAidCsvRow {
            student_id: &record.student_id,
            aid_amount: record
                .aid_amount
                .map(|value| format!("{value:.2}"))
                .unwrap_or_default(),
            aid_status: record
                .aid_status
                .map(format_aid_status)
                .unwrap_or_default()
                .to_string(),
            disbursement_date: record
                .disbursement_date
                .map(|value| value.format("%Y-%m-%d").to_string())
                .unwrap_or_default(),
        })?;
    }
    writer.flush()?;
    Ok(())
}

fn format_aid_status(status: AidStatus) -> &'static str {
    match status {
        AidStatus::Active => "active",
        AidStatus::Suspended => "suspended",
        AidStatus::None => "none",
    }
}

#[allow(dead_code)]
fn format_enrollment_status(status: EnrollmentStatus) -> &'static str {
    match status {
        EnrollmentStatus::FullTime => "full_time",
        EnrollmentStatus::PartTime => "part_time",
    }
}

fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let file = File::create(path).with_context(|| format!("creating {}", path.display()))?;
    serde_json::to_writer_pretty(file, value)?;
    Ok(())
}

fn file_sha256(path: &Path) -> Result<String> {
    let mut file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 8192];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn baseline_dataset_id(seed: u64, academic_hash: &str, aid_hash: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(seed.to_string().as_bytes());
    hasher.update(academic_hash.as_bytes());
    hasher.update(aid_hash.as_bytes());
    hex::encode(hasher.finalize())
}

fn stringify_operator_map(input: &BTreeMap<FragmentationOperator, f64>) -> BTreeMap<String, f64> {
    input
        .iter()
        .map(|(operator, value)| (operator.as_str().to_string(), *value))
        .collect()
}

fn stringify_selection_map(
    input: &BTreeMap<FragmentationOperator, Vec<String>>,
) -> BTreeMap<String, Vec<String>> {
    input
        .iter()
        .map(|(operator, value)| (operator.as_str().to_string(), value.clone()))
        .collect()
}

fn mapping<'a>(value: &'a Value, context: &str) -> Result<&'a Mapping> {
    value
        .as_mapping()
        .ok_or_else(|| anyhow!("{context} must be a mapping"))
}

fn sequence<'a>(value: &'a Value, context: &str) -> Result<&'a Vec<Value>> {
    value
        .as_sequence()
        .ok_or_else(|| anyhow!("{context} must be a sequence"))
}

fn key<'a>(map: &'a Mapping, key: &str) -> Result<&'a Value> {
    map.get(&Value::String(key.to_string()))
        .ok_or_else(|| anyhow!("missing key {key}"))
}

fn child_mapping<'a>(map: &'a Mapping, key_name: &str, context: &str) -> Result<&'a Mapping> {
    mapping(key(map, key_name)?, context)
}

fn map_keys(map: &Mapping) -> Result<BTreeSet<String>> {
    map.keys()
        .map(|value| scalar_string(value, "mapping key"))
        .collect::<Result<BTreeSet<_>>>()
}

fn expect_exact_names(context: &str, actual: &BTreeSet<String>, expected: &[&str]) -> Result<()> {
    let expected_set = expected
        .iter()
        .map(|value| value.to_string())
        .collect::<BTreeSet<_>>();
    if *actual != expected_set {
        return Err(anyhow!("{context} must be exactly {:?}", expected));
    }
    Ok(())
}

fn expect_u64(map: &Mapping, key_name: &str, expected: u64) -> Result<()> {
    let actual = as_u64(key(map, key_name)?, key_name)?;
    if actual != expected {
        return Err(anyhow!("{key_name} must be {expected}"));
    }
    Ok(())
}

fn expect_string_value(map: &Mapping, key_name: &str, expected: &str) -> Result<()> {
    let actual = scalar_string(key(map, key_name)?, key_name)?;
    if actual != expected {
        return Err(anyhow!("{key_name} must be {expected}"));
    }
    Ok(())
}

fn expect_bool_value(map: &Mapping, key_name: &str, expected: bool) -> Result<()> {
    let actual = as_bool(key(map, key_name)?, key_name)?;
    if actual != expected {
        return Err(anyhow!("{key_name} must be {expected}"));
    }
    Ok(())
}

fn scalar_string(value: &Value, context: &str) -> Result<String> {
    value
        .as_str()
        .map(ToOwned::to_owned)
        .ok_or_else(|| anyhow!("{context} must be a string"))
}

fn as_f64(value: &Value, context: &str) -> Result<f64> {
    value
        .as_f64()
        .ok_or_else(|| anyhow!("{context} must be a number"))
}

fn as_i64(value: &Value, context: &str) -> Result<i64> {
    value
        .as_i64()
        .ok_or_else(|| anyhow!("{context} must be an integer"))
}

fn as_u64(value: &Value, context: &str) -> Result<u64> {
    value
        .as_u64()
        .ok_or_else(|| anyhow!("{context} must be an unsigned integer"))
}

fn as_usize(value: &Value, context: &str) -> Result<usize> {
    let parsed = as_u64(value, context)?;
    usize::try_from(parsed).map_err(|_| anyhow!("{context} is too large for this platform"))
}

fn as_bool(value: &Value, context: &str) -> Result<bool> {
    value
        .as_bool()
        .ok_or_else(|| anyhow!("{context} must be a bool"))
}

fn parse_date(value: &Value, context: &str) -> Result<NaiveDate> {
    let text = scalar_string(value, context)?;
    NaiveDate::parse_from_str(&text, "%Y-%m-%d")
        .with_context(|| format!("{context} must be YYYY-MM-DD"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;
    use sha2::{Digest, Sha256};
    use std::collections::HashMap;
    use tempfile::tempdir;

    #[derive(Deserialize)]
    struct AcademicRow {
        student_id: String,
        gpa: f64,
    }

    #[derive(Deserialize)]
    struct AidRow {
        student_id: String,
        aid_amount: Option<String>,
        aid_status: Option<String>,
    }

    #[test]
    fn config_loader_rejects_unknown_verification_status() {
        let dir = tempdir().unwrap();
        let schema = dir.path().join("schema.yaml");
        let experiment = dir.path().join("experiment.yaml");
        fs::write(
            &schema,
            include_str!("../../../../configs/schema_registry.yaml")
                .replace("scenario_constant", "made_up_status"),
        )
        .unwrap();
        fs::write(
            &experiment,
            include_str!("../../../../configs/experiment.yaml"),
        )
        .unwrap();
        let error = load_configs(&schema, &experiment).unwrap_err();
        assert!(error.to_string().contains("invalid verification"));
    }

    #[test]
    fn execute_run_writes_manifest_and_csvs() {
        let dir = tempdir().unwrap();
        let schema = dir.path().join("schema.yaml");
        let experiment = dir.path().join("experiment.yaml");
        fs::write(
            &schema,
            include_str!("../../../../configs/schema_registry.yaml"),
        )
        .unwrap();
        fs::write(
            &experiment,
            include_str!("../../../../configs/experiment.yaml"),
        )
        .unwrap();

        let completed = execute_run(&RunOptions {
            schema_path: schema,
            experiment_path: experiment,
            out_root: dir.path().join("runs"),
            run_id: Some("test".to_string()),
            overwrite: false,
        })
        .unwrap();

        assert!(completed
            .run_dir
            .join("variants")
            .join("high_fragmentation")
            .join("financial_aid_records.csv")
            .exists());
        assert!(completed
            .run_dir
            .join("manifests")
            .join("high_fragmentation_manifest.json")
            .exists());
    }

    #[test]
    fn golden_seed_outputs_remain_stable() {
        let dir = tempdir().unwrap();
        let schema = dir.path().join("schema.yaml");
        let experiment = dir.path().join("experiment.yaml");
        fs::write(
            &schema,
            include_str!("../../../../configs/schema_registry.yaml"),
        )
        .unwrap();
        fs::write(
            &experiment,
            include_str!("../../../../configs/experiment.yaml"),
        )
        .unwrap();

        let completed = execute_run(&RunOptions {
            schema_path: schema,
            experiment_path: experiment,
            out_root: dir.path().join("runs"),
            run_id: Some("golden".to_string()),
            overwrite: false,
        })
        .unwrap();

        let manifest_path = completed
            .run_dir
            .join("manifests")
            .join("high_fragmentation_manifest.json");
        let manifest: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(manifest_path).unwrap()).unwrap();
        assert_eq!(
            manifest["baseline_file_hashes"]["academic_records"],
            "6f54d34ae6102aa256daecd3da15bbd58a44047378214382254d29043df0a8b4"
        );
        assert_eq!(
            manifest["baseline_file_hashes"]["financial_aid_records"],
            "2f7555aa65b4603663d8204b0585f5e0b8b06f50d0641d82fb950cf7ba071bc9"
        );

        let selected_json = serde_json::to_string(&manifest["selected_row_ids"]).unwrap();
        let selected_hash = hex::encode(Sha256::digest(selected_json.as_bytes()));
        assert_eq!(
            selected_hash,
            "ade66b9acc6868daaddf87a1ae246b08f9ee029a15042a31f25ab1fed9b8408e"
        );

        let baseline_dir = completed.run_dir.join("variants").join("baseline");
        assert_eq!(count_canonical_query_rows(&baseline_dir), 52);
    }

    fn count_canonical_query_rows(variant_dir: &Path) -> usize {
        let mut aid_reader =
            csv::Reader::from_path(variant_dir.join("financial_aid_records.csv")).unwrap();
        let aid_by_student = aid_reader
            .deserialize::<AidRow>()
            .map(|row| {
                let parsed = row.unwrap();
                (parsed.student_id.clone(), parsed)
            })
            .collect::<HashMap<_, _>>();

        let mut academic_reader =
            csv::Reader::from_path(variant_dir.join("academic_records.csv")).unwrap();
        academic_reader
            .deserialize::<AcademicRow>()
            .map(|row| row.unwrap())
            .filter(|academic| {
                if academic.gpa >= 2.5 {
                    return false;
                }
                let Some(aid) = aid_by_student.get(&academic.student_id) else {
                    return false;
                };
                !aid.aid_amount.as_deref().unwrap_or("").is_empty()
                    && !aid.aid_status.as_deref().unwrap_or("").is_empty()
                    && aid.aid_status.as_deref() != Some("active")
            })
            .count()
    }
}
