use crate::config::BenchmarkConfig;
use crate::generator::{financial_aid_student_id, generate_run};
use crate::model::{
    AcademicRecord, AidStatus, FinancialAidRecord, FragmentationOperator, FragmentedVariant,
};
use anyhow::{anyhow, Context, Result};
use chrono::{Duration, NaiveDate};
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
struct FinancialAidPublicationEventCsvRow<'a> {
    event_id: String,
    financial_aid_student_id: &'a str,
    event_time: String,
    observed_at: String,
    published_at: &'a str,
    arrival_stream: &'static str,
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
    financial_aid_late_arrivals: String,
    identity_crosswalk: String,
    aid_status_crosswalk: String,
    financial_aid_publication_events: String,
}

#[derive(Serialize)]
struct InvariantManifest {
    mutate_academic_records: bool,
    regenerate_population_per_variant: bool,
    corruption_applies_only_to: &'static str,
}

#[derive(Clone, Serialize)]
struct SnapshotManifest {
    published_at: String,
    event_time_watermark: String,
}

#[derive(Clone, Serialize)]
struct SnapshotManifests {
    current: SnapshotManifest,
    replayed: SnapshotManifest,
}

#[derive(Clone, Serialize)]
struct TemporalManifest {
    contract_version: u32,
    timezone: &'static str,
    logical_time: bool,
    snapshots: SnapshotManifests,
    current_record_count: usize,
    late_record_count: usize,
}

#[derive(Clone)]
struct PublicationTimeline {
    event_time_watermark: NaiveDate,
    current_published_at: String,
    replayed_published_at: String,
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
    temporal: TemporalManifest,
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

    let timeline = publication_timeline(config)?;
    let baseline_dir = variants_dir.join("baseline");
    fs::create_dir_all(&baseline_dir)?;
    let baseline_academic = baseline_dir.join("academic_records.csv");
    let baseline_aid = baseline_dir.join("financial_aid_records.csv");
    let baseline_late_aid = baseline_dir.join("financial_aid_late_arrivals.csv");
    let baseline_publication_events = baseline_dir.join("financial_aid_publication_events.csv");
    let baseline_identity_crosswalk = baseline_dir.join("identity_crosswalk.csv");
    let baseline_status_crosswalk = baseline_dir.join("aid_status_crosswalk.csv");
    write_academic_csv(&baseline_academic, &generated.baseline.academic_records)?;
    write_financial_aid_csv(&baseline_aid, &generated.baseline.financial_aid_records)?;
    write_financial_aid_csv(&baseline_late_aid, &[])?;
    write_financial_aid_publication_events(
        &baseline_publication_events,
        &generated.baseline.financial_aid_records,
        &[],
        &timeline,
    )?;
    write_identity_crosswalk(
        &baseline_identity_crosswalk,
        &generated.baseline.academic_records,
        &[],
    )?;
    write_aid_status_crosswalk(&baseline_status_crosswalk)?;
    let baseline_hashes = FileHashes {
        academic_records: file_sha256(&baseline_academic)?,
        financial_aid_records: file_sha256(&baseline_aid)?,
        financial_aid_late_arrivals: file_sha256(&baseline_late_aid)?,
        identity_crosswalk: file_sha256(&baseline_identity_crosswalk)?,
        aid_status_crosswalk: file_sha256(&baseline_status_crosswalk)?,
        financial_aid_publication_events: file_sha256(&baseline_publication_events)?,
    };
    let dataset_id = baseline_dataset_id(config.population.seed, &baseline_hashes);

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
        temporal_manifest(&timeline, generated.baseline.financial_aid_records.len(), 0),
    )?;

    for variant in &generated.variants {
        write_variant(
            variant,
            &variants_dir,
            &manifests_dir,
            &baseline_academic,
            &baseline_status_crosswalk,
            &generated.baseline.academic_records,
            &baseline_hashes,
            &dataset_id,
            config.population.seed,
            &timeline,
        )?;
    }
    Ok(())
}

fn publish_output<F>(path: &Path, overwrite: bool, build: F) -> Result<()>
where
    F: FnOnce(&Path) -> Result<()>,
{
    publish_output_with_backup_cleanup(path, overwrite, build, |backup| fs::remove_dir_all(backup))
}

fn publish_output_with_backup_cleanup<F, C>(
    path: &Path,
    overwrite: bool,
    build: F,
    cleanup_backup: C,
) -> Result<()>
where
    F: FnOnce(&Path) -> Result<()>,
    C: FnOnce(&Path) -> std::io::Result<()>,
{
    validate_output_path(path)?;
    if existing_output_directory(path)? && !overwrite {
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

    // 构建期间目标可能被外部创建，因此发布前再次检查其真实文件类型。
    if !existing_output_directory(path)? {
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

    // 新输出已原子生效后，旧备份清理只属于收尾；失败时保留恢复线索，不能误报生成失败。
    if let Err(cleanup_error) = cleanup_backup(&backup_path) {
        eprintln!(
            concat!(
                "warning: output directory {} was published successfully, but previous output ",
                "backup cleanup failed: {}; recoverable backup may remain at {}"
            ),
            path.display(),
            cleanup_error,
            backup_path.display()
        );
    }
    Ok(())
}

fn existing_output_directory(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_dir() => Ok(true),
        Ok(_) => Err(anyhow!(
            "existing output is not a directory: {}",
            path.display()
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => {
            Err(error).with_context(|| format!("inspecting existing output {}", path.display()))
        }
    }
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
    baseline_status_crosswalk: &Path,
    baseline_academic_records: &[AcademicRecord],
    baseline_hashes: &FileHashes,
    dataset_id: &str,
    seed: u64,
    timeline: &PublicationTimeline,
) -> Result<()> {
    let variant_dir = variants_dir.join(&variant.name);
    fs::create_dir_all(&variant_dir)?;
    let academic_path = variant_dir.join("academic_records.csv");
    let aid_path = variant_dir.join("financial_aid_records.csv");
    let late_aid_path = variant_dir.join("financial_aid_late_arrivals.csv");
    let publication_events_path = variant_dir.join("financial_aid_publication_events.csv");
    let identity_crosswalk_path = variant_dir.join("identity_crosswalk.csv");
    let status_crosswalk_path = variant_dir.join("aid_status_crosswalk.csv");
    reuse_unchanged_file(baseline_academic, &academic_path)?;
    reuse_unchanged_file(baseline_status_crosswalk, &status_crosswalk_path)?;
    write_financial_aid_csv(&aid_path, &variant.financial_aid_records)?;
    write_financial_aid_csv(&late_aid_path, &variant.late_financial_aid_records)?;
    write_financial_aid_publication_events(
        &publication_events_path,
        &variant.financial_aid_records,
        &variant.late_financial_aid_records,
        timeline,
    )?;
    write_identity_crosswalk(
        &identity_crosswalk_path,
        baseline_academic_records,
        &variant.selected_row_ids[&FragmentationOperator::IdentifierMismatch],
    )?;
    let variant_hashes = FileHashes {
        academic_records: baseline_hashes.academic_records.clone(),
        financial_aid_records: file_sha256(&aid_path)?,
        financial_aid_late_arrivals: file_sha256(&late_aid_path)?,
        identity_crosswalk: file_sha256(&identity_crosswalk_path)?,
        aid_status_crosswalk: baseline_hashes.aid_status_crosswalk.clone(),
        financial_aid_publication_events: file_sha256(&publication_events_path)?,
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
        temporal_manifest(
            timeline,
            variant.financial_aid_records.len(),
            variant.late_financial_aid_records.len(),
        ),
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
    temporal: TemporalManifest,
) -> Result<()> {
    let manifest = VariantManifest {
        manifest_version: 3,
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
            corruption_applies_only_to: "financial_aid_domain",
        },
        temporal,
    };
    let path = manifests_dir.join(format!("{variant}_manifest.json"));
    let file = File::create(&path).with_context(|| format!("creating {}", path.display()))?;
    serde_json::to_writer_pretty(file, &manifest)?;
    Ok(())
}

fn publication_timeline(config: &BenchmarkConfig) -> Result<PublicationTimeline> {
    let aid = &config.population.financial_aid;
    let event_time_watermark = config
        .population
        .academic
        .term_anchor_date
        .checked_add_signed(
            Duration::try_days(aid.disbursement_offset_days.max)
                .ok_or_else(|| anyhow!("financial-aid disbursement offset is out of range"))?,
        )
        .ok_or_else(|| anyhow!("financial-aid event-time watermark is out of range"))?;
    let current_published_date = event_time_watermark
        .checked_add_signed(Duration::days(1))
        .ok_or_else(|| anyhow!("financial-aid current publication date is out of range"))?;
    let replayed_published_date = current_published_date
        .checked_add_signed(
            Duration::try_days(aid.late_publication_delay_days)
                .ok_or_else(|| anyhow!("financial-aid replay delay is out of range"))?,
        )
        .ok_or_else(|| anyhow!("financial-aid replay publication date is out of range"))?;

    Ok(PublicationTimeline {
        event_time_watermark,
        current_published_at: midnight_utc(current_published_date),
        replayed_published_at: midnight_utc(replayed_published_date),
    })
}

fn temporal_manifest(
    timeline: &PublicationTimeline,
    current_record_count: usize,
    late_record_count: usize,
) -> TemporalManifest {
    let event_time_watermark = midnight_utc(timeline.event_time_watermark);
    TemporalManifest {
        contract_version: 1,
        timezone: "UTC",
        logical_time: true,
        snapshots: SnapshotManifests {
            current: SnapshotManifest {
                published_at: timeline.current_published_at.clone(),
                event_time_watermark: event_time_watermark.clone(),
            },
            replayed: SnapshotManifest {
                published_at: timeline.replayed_published_at.clone(),
                event_time_watermark,
            },
        },
        current_record_count,
        late_record_count,
    }
}

fn midnight_utc(date: NaiveDate) -> String {
    format!("{date}T00:00:00Z")
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

fn write_aid_status_crosswalk(path: &Path) -> Result<()> {
    let mut writer =
        csv::Writer::from_path(path).with_context(|| format!("creating {}", path.display()))?;
    writer.write_record(["financial_aid_status", "canonical_aid_status"])?;
    for status in [AidStatus::Active, AidStatus::Suspended, AidStatus::None] {
        let canonical = status.to_string();
        writer.write_record([canonical.as_str(), canonical.as_str()])?;
        writer.write_record([format!("financial-aid::{status}"), canonical])?;
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

fn write_financial_aid_publication_events(
    path: &Path,
    current_records: &[FinancialAidRecord],
    late_records: &[FinancialAidRecord],
    timeline: &PublicationTimeline,
) -> Result<()> {
    let mut writer = csv::WriterBuilder::new()
        .has_headers(false)
        .from_path(path)
        .with_context(|| format!("creating {}", path.display()))?;
    writer.write_record([
        "event_id",
        "financial_aid_student_id",
        "event_time",
        "observed_at",
        "published_at",
        "arrival_stream",
    ])?;

    // current 与 late 使用相同业务事件，只让产品可见时间和路由流不同。
    for (records, published_at, arrival_stream) in [
        (
            current_records,
            timeline.current_published_at.as_str(),
            "current",
        ),
        (
            late_records,
            timeline.replayed_published_at.as_str(),
            "late",
        ),
    ] {
        for record in records {
            let event_date = record.disbursement_date.ok_or_else(|| {
                anyhow!(
                    "financial-aid publication event {} is missing disbursement_date",
                    record.student_id
                )
            })?;
            let event_time = event_date.format("%Y-%m-%d").to_string();
            writer.serialize(FinancialAidPublicationEventCsvRow {
                event_id: format!("aid-disbursement::{}::{event_time}", record.student_id),
                financial_aid_student_id: &record.student_id,
                event_time,
                observed_at: midnight_utc(event_date),
                published_at,
                arrival_stream,
            })?;
        }
    }
    writer.flush()?;
    Ok(())
}

fn write_financial_aid_csv(path: &Path, records: &[FinancialAidRecord]) -> Result<()> {
    let mut writer =
        csv::Writer::from_path(path).with_context(|| format!("creating {}", path.display()))?;
    if records.is_empty() {
        // 空的迟到数据集也保留固定表头，避免下游推断出无列关系。
        writer.write_record([
            "student_id",
            "aid_amount",
            "aid_status",
            "disbursement_date",
        ])?;
    }
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

fn baseline_dataset_id(seed: u64, hashes: &FileHashes) -> String {
    // 数据集身份只由业务与治理文件决定；逻辑发布时间或重放策略变化不能生成新身份。
    // 因此这里明确不吸收 financial_aid_publication_events 的哈希。
    let mut hasher = Sha256::new();
    hasher.update(seed.to_string().as_bytes());
    hasher.update(hashes.academic_records.as_bytes());
    hasher.update(hashes.financial_aid_records.as_bytes());
    hasher.update(hashes.financial_aid_late_arrivals.as_bytes());
    hasher.update(hashes.identity_crosswalk.as_bytes());
    hasher.update(hashes.aid_status_crosswalk.as_bytes());
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
    use super::{publish_output, publish_output_with_backup_cleanup};
    use anyhow::{anyhow, Result};
    use std::fs;
    use std::io;
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

    #[test]
    fn successful_publish_is_not_reported_as_failed_when_backup_cleanup_fails() {
        let temp = tempdir().unwrap();
        let output = temp.path().join("run");
        fs::create_dir(&output).unwrap();
        fs::write(output.join("previous.txt"), "complete previous run").unwrap();
        let mut retained_backup = None;

        let result = publish_output_with_backup_cleanup(
            &output,
            true,
            |staging| {
                fs::write(staging.join("current.txt"), "complete current run")?;
                Ok(())
            },
            |backup| {
                retained_backup = Some(backup.to_path_buf());
                Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "forced cleanup failure",
                ))
            },
        );

        assert!(result.is_ok());
        assert_eq!(
            fs::read_to_string(output.join("current.txt")).unwrap(),
            "complete current run"
        );
        assert!(!output.join("previous.txt").exists());
        let retained_backup = retained_backup.expect("backup cleanup should be attempted");
        assert_eq!(
            fs::read_to_string(retained_backup.join("previous.txt")).unwrap(),
            "complete previous run"
        );
    }

    #[test]
    fn overwrite_rejects_existing_non_directory_before_building() {
        let temp = tempdir().unwrap();
        let output = temp.path().join("run");
        fs::write(&output, "not a directory").unwrap();
        let mut build_called = false;

        let error = publish_output(&output, true, |_| {
            build_called = true;
            Ok(())
        })
        .unwrap_err();

        assert!(!build_called);
        assert!(error.to_string().contains("not a directory"));
        assert_eq!(fs::read_to_string(&output).unwrap(), "not a directory");
    }
}
