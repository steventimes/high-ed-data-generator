use crate::config::BenchmarkConfig;
use crate::generator::{financial_aid_student_id, generate_run};
use crate::model::{AcademicRecord, FinancialAidRecord, FragmentationOperator, FragmentedVariant};
use anyhow::{anyhow, Context, Result};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Component, Path, PathBuf};

#[derive(Clone, Debug)]
pub struct RunOptions {
    pub config_path: PathBuf,
    pub output_dir: PathBuf,
    pub overwrite: bool,
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
struct IdentityCrosswalkCsvRow<'a> {
    canonical_student_id: &'a str,
    financial_aid_student_id: String,
}

#[derive(Clone, Serialize)]
struct FileHashes {
    academic_records: String,
    financial_aid_records: String,
    identity_crosswalk: String,
}

#[derive(Serialize)]
struct InvariantManifest {
    mutate_academic_records: bool,
    regenerate_population_per_variant: bool,
    corruption_applies_only_to: &'static str,
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

pub fn execute_run(options: &RunOptions) -> Result<PathBuf> {
    validate_output_path(&options.output_dir)?;
    let config = BenchmarkConfig::load(&options.config_path)?;
    // 覆盖旧运行目录前先读取配置快照，支持配置文件位于旧目录内部的场景。
    let config_snapshot = fs::read(&options.config_path)
        .with_context(|| format!("reading {}", options.config_path.display()))?;
    let generated = generate_run(&config)?;

    publish_output(&options.output_dir, options.overwrite, |staging| {
        write_generated_run(staging, &config, &config_snapshot, &generated)
    })?;
    Ok(options.output_dir.clone())
}

fn write_generated_run(
    output_dir: &Path,
    config: &BenchmarkConfig,
    config_snapshot: &[u8],
    generated: &crate::model::GeneratedRun,
) -> Result<()> {
    let snapshot_dir = output_dir.join("config_snapshot");
    let variants_dir = output_dir.join("variants");
    let manifests_dir = output_dir.join("manifests");
    fs::create_dir_all(&snapshot_dir)?;
    fs::create_dir_all(&variants_dir)?;
    fs::create_dir_all(&manifests_dir)?;
    fs::write(snapshot_dir.join("benchmark.yaml"), config_snapshot)
        .context("writing benchmark config snapshot")?;

    let baseline_dir = variants_dir.join("baseline");
    fs::create_dir_all(&baseline_dir)?;
    let baseline_academic = baseline_dir.join("academic_records.csv");
    let baseline_aid = baseline_dir.join("financial_aid_records.csv");
    let baseline_crosswalk = baseline_dir.join("identity_crosswalk.csv");
    write_academic_csv(&baseline_academic, &generated.baseline.academic_records)?;
    write_financial_aid_csv(&baseline_aid, &generated.baseline.financial_aid_records)?;
    write_identity_crosswalk(
        &baseline_crosswalk,
        &generated.baseline.academic_records,
        &[],
    )?;
    let baseline_hashes = FileHashes {
        academic_records: file_sha256(&baseline_academic)?,
        financial_aid_records: file_sha256(&baseline_aid)?,
        identity_crosswalk: file_sha256(&baseline_crosswalk)?,
    };
    let dataset_id = baseline_dataset_id(
        config.population.seed,
        &baseline_hashes.academic_records,
        &baseline_hashes.financial_aid_records,
        &baseline_hashes.identity_crosswalk,
    );

    let baseline_rates = FragmentationOperator::ALL
        .into_iter()
        .map(|operator| (operator, config.variants["baseline"].rate(operator)))
        .collect::<BTreeMap<_, _>>();
    let baseline_selections = FragmentationOperator::ALL
        .into_iter()
        .map(|operator| (operator, Vec::new()))
        .collect::<BTreeMap<_, _>>();
    write_manifest(
        "baseline",
        baseline_hashes.clone(),
        &manifests_dir,
        &baseline_hashes,
        &dataset_id,
        config.population.seed,
        &baseline_rates,
        &baseline_selections,
        1.0,
    )?;

    for variant in &generated.variants {
        write_variant(
            variant,
            &variants_dir,
            &manifests_dir,
            &baseline_academic,
            &generated.baseline.academic_records,
            &baseline_hashes,
            &dataset_id,
            config.population.seed,
        )?;
    }
    Ok(())
}

fn publish_output<F>(path: &Path, overwrite: bool, build: F) -> Result<()>
where
    F: FnOnce(&Path) -> Result<()>,
{
    validate_output_path(path)?;
    if path.exists() && !overwrite {
        return Err(anyhow!(
            "output directory already exists: {} (pass --overwrite to replace it)",
            path.display()
        ));
    }

    let parent = path
        .parent()
        .filter(|value| !value.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)
        .with_context(|| format!("creating output parent {}", parent.display()))?;
    let staging = tempfile::Builder::new()
        .prefix(".high-ed-staging-")
        .tempdir_in(parent)
        .with_context(|| format!("creating staging directory in {}", parent.display()))?;

    // 所有文件先写入同一文件系统的临时目录，失败时旧运行保持完整。
    build(staging.path())?;

    if !path.exists() {
        fs::rename(staging.path(), path)
            .with_context(|| format!("publishing output directory {}", path.display()))?;
        return Ok(());
    }
    if !overwrite {
        return Err(anyhow!(
            "output directory appeared during generation: {}",
            path.display()
        ));
    }

    let backup_slot = tempfile::Builder::new()
        .prefix(".high-ed-backup-")
        .tempdir_in(parent)
        .with_context(|| format!("creating backup slot in {}", parent.display()))?;
    let backup_path = backup_slot.keep();
    fs::remove_dir(&backup_path)?;
    fs::rename(path, &backup_path)
        .with_context(|| format!("backing up previous output {}", path.display()))?;

    if let Err(publish_error) = fs::rename(staging.path(), path) {
        if let Err(restore_error) = fs::rename(&backup_path, path) {
            return Err(anyhow!(
                "publishing {} failed: {publish_error}; restoring previous output failed: {restore_error}",
                path.display()
            ));
        }
        return Err(publish_error)
            .with_context(|| format!("publishing output directory {}", path.display()));
    }

    fs::remove_dir_all(&backup_path)
        .with_context(|| format!("removing previous output backup {}", backup_path.display()))?;
    Ok(())
}

fn validate_output_path(path: &Path) -> Result<()> {
    if path
        .components()
        .any(|component| component == Component::ParentDir)
    {
        return Err(anyhow!(
            "refusing output directory with parent components: {}",
            path.display()
        ));
    }
    if path.as_os_str().is_empty() || path == Path::new(".") || path == Path::new("/") {
        return Err(anyhow!(
            "refusing unsafe output directory {}",
            path.display()
        ));
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn write_variant(
    variant: &FragmentedVariant,
    variants_dir: &Path,
    manifests_dir: &Path,
    baseline_academic: &Path,
    baseline_academic_records: &[AcademicRecord],
    baseline_hashes: &FileHashes,
    dataset_id: &str,
    seed: u64,
) -> Result<()> {
    let variant_dir = variants_dir.join(&variant.name);
    fs::create_dir_all(&variant_dir)?;
    let academic_path = variant_dir.join("academic_records.csv");
    let aid_path = variant_dir.join("financial_aid_records.csv");
    let crosswalk_path = variant_dir.join("identity_crosswalk.csv");
    reuse_unchanged_file(baseline_academic, &academic_path)?;
    write_financial_aid_csv(&aid_path, &variant.financial_aid_records)?;
    write_identity_crosswalk(
        &crosswalk_path,
        baseline_academic_records,
        &variant.selected_row_ids[&FragmentationOperator::IdentifierMismatch],
    )?;
    let variant_hashes = FileHashes {
        academic_records: baseline_hashes.academic_records.clone(),
        financial_aid_records: file_sha256(&aid_path)?,
        identity_crosswalk: file_sha256(&crosswalk_path)?,
    };

    write_manifest(
        &variant.name,
        variant_hashes,
        manifests_dir,
        baseline_hashes,
        dataset_id,
        seed,
        &variant.corruption_percentages,
        &variant.selected_row_ids,
        variant.fragmentation_score,
    )
}

#[allow(clippy::too_many_arguments)]
fn write_manifest(
    variant: &str,
    variant_hashes: FileHashes,
    manifests_dir: &Path,
    baseline_hashes: &FileHashes,
    dataset_id: &str,
    seed: u64,
    corruption_percentages: &BTreeMap<FragmentationOperator, f64>,
    selected_row_ids: &BTreeMap<FragmentationOperator, Vec<String>>,
    fragmentation_score: f64,
) -> Result<()> {
    let manifest = VariantManifest {
        manifest_version: 1,
        variant: variant.to_string(),
        baseline_dataset_id: dataset_id.to_string(),
        baseline_file_hashes: baseline_hashes.clone(),
        variant_file_hashes: variant_hashes,
        random_seed: seed,
        corruption_percentages: stringify_rates(corruption_percentages),
        selected_row_ids: stringify_selections(selected_row_ids),
        fragmentation_score,
        invariants: InvariantManifest {
            mutate_academic_records: false,
            regenerate_population_per_variant: false,
            corruption_applies_only_to: "financial_aid_records",
        },
    };
    let path = manifests_dir.join(format!("{variant}_manifest.json"));
    let file = File::create(&path).with_context(|| format!("creating {}", path.display()))?;
    serde_json::to_writer_pretty(file, &manifest)?;
    Ok(())
}

fn reuse_unchanged_file(source: &Path, target: &Path) -> Result<()> {
    if fs::hard_link(source, target).is_ok() {
        return Ok(());
    }
    // 不支持硬链接的文件系统回退到复制，保持跨平台行为一致。
    fs::copy(source, target)
        .with_context(|| format!("copying {} to {}", source.display(), target.display()))?;
    Ok(())
}

fn write_identity_crosswalk(
    path: &Path,
    records: &[AcademicRecord],
    mismatched_ids: &[String],
) -> Result<()> {
    let mut writer =
        csv::Writer::from_path(path).with_context(|| format!("creating {}", path.display()))?;
    for record in records {
        let mismatched = mismatched_ids
            .binary_search_by(|candidate| candidate.as_str().cmp(&record.student_id))
            .is_ok();
        writer.serialize(IdentityCrosswalkCsvRow {
            canonical_student_id: &record.student_id,
            financial_aid_student_id: if mismatched {
                financial_aid_student_id(&record.student_id)
            } else {
                record.student_id.clone()
            },
        })?;
    }
    writer.flush()?;
    Ok(())
}

fn write_academic_csv(path: &Path, records: &[AcademicRecord]) -> Result<()> {
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

fn write_financial_aid_csv(path: &Path, records: &[FinancialAidRecord]) -> Result<()> {
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
                .map(|status| status.to_string())
                .unwrap_or_default(),
            disbursement_date: record
                .disbursement_date
                .map(|date| date.format("%Y-%m-%d").to_string())
                .unwrap_or_default(),
        })?;
    }
    writer.flush()?;
    Ok(())
}

fn file_sha256(path: &Path) -> Result<String> {
    let mut file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(to_hex(&hasher.finalize()))
}

fn baseline_dataset_id(
    seed: u64,
    academic_hash: &str,
    aid_hash: &str,
    crosswalk_hash: &str,
) -> String {
    let mut hasher = Sha256::new();
    hasher.update(seed.to_string().as_bytes());
    hasher.update(academic_hash.as_bytes());
    hasher.update(aid_hash.as_bytes());
    hasher.update(crosswalk_hash.as_bytes());
    to_hex(&hasher.finalize())
}

fn to_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn stringify_rates(values: &BTreeMap<FragmentationOperator, f64>) -> BTreeMap<String, f64> {
    values
        .iter()
        .map(|(operator, value)| (operator.as_str().to_string(), *value))
        .collect()
}

fn stringify_selections(
    values: &BTreeMap<FragmentationOperator, Vec<String>>,
) -> BTreeMap<String, Vec<String>> {
    values
        .iter()
        .map(|(operator, ids)| (operator.as_str().to_string(), ids.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::publish_output;
    use anyhow::{anyhow, Result};
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn failed_staged_write_preserves_the_previous_run() {
        let temp = tempdir().unwrap();
        let output = temp.path().join("run");
        fs::create_dir(&output).unwrap();
        fs::write(output.join("previous.txt"), "complete previous run").unwrap();

        let error = publish_output(&output, true, |staging| -> Result<()> {
            fs::write(staging.join("partial.txt"), "incomplete replacement")?;
            Err(anyhow!("forced write failure"))
        })
        .unwrap_err();

        assert!(error.to_string().contains("forced write failure"));
        assert_eq!(
            fs::read_to_string(output.join("previous.txt")).unwrap(),
            "complete previous run"
        );
        assert!(!output.join("partial.txt").exists());
    }
}
