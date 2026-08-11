use high_ed_data_generator::config::BenchmarkConfig;
use high_ed_data_generator::output::{execute_run, RunOptions};
use sha2::{Digest, Sha256};
use std::fs;
use tempfile::tempdir;

const CONFIG: &str = r#"
version: 1
population:
  size: 500
  seed: 20260410
  academic:
    gpa: {mean: 2.8, std: 0.6, min: 0.0, max: 4.0}
    full_time_probability: 0.62
    semester: Fall 2024
    term_anchor_date: 2024-09-01
  financial_aid:
    zero_probability: 0.28
    recipient_mean: 14100.0
    gamma_shape: 2.0
    active_probability: 0.95
    suspended_probability: 0.05
    disbursement_offset_days: {min: -10, max: 30}
variants:
  baseline: {}
  low_fragmentation: {drop_row: 0.10}
  medium_fragmentation: {drop_row: 0.30}
  high_fragmentation: {drop_row: 0.50}
"#;

#[test]
fn config_rejects_unknown_fields() {
    let invalid = CONFIG.replace("size: 500", "size: 500\n  mystery: true");
    let error = BenchmarkConfig::from_yaml(&invalid).unwrap_err();
    assert!(error.to_string().contains("unknown field `mystery`"));
}

#[test]
fn config_rejects_values_that_can_break_generation() {
    for invalid in [
        CONFIG.replace("mean: 2.8", "mean: .nan"),
        CONFIG.replace("recipient_mean: 14100.0", "recipient_mean: .inf"),
        CONFIG.replace("semester: Fall 2024", "semester: ''"),
        CONFIG.replace(
            "disbursement_offset_days: {min: -10, max: 30}",
            "disbursement_offset_days: {min: -9223372036854775808, max: 9223372036854775807}",
        ),
    ] {
        assert!(BenchmarkConfig::from_yaml(&invalid).is_err());
    }
}

#[test]
fn config_rejects_gpa_bounds_outside_the_four_point_scale() {
    for invalid in [
        CONFIG.replace("min: 0.0", "min: -0.1"),
        CONFIG.replace("max: 4.0", "max: 4.1"),
    ] {
        let error = BenchmarkConfig::from_yaml(&invalid).unwrap_err();
        assert!(error.to_string().contains("gpa"));
    }
}

#[test]
fn generation_preserves_golden_hashes_and_variant_invariants() {
    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    fs::write(&config_path, CONFIG).unwrap();

    let run_dir = execute_run(&RunOptions {
        config_path,
        output_dir: temp.path().join("run"),
        overwrite: false,
    })
    .unwrap();

    let baseline_dir = run_dir.join("variants/baseline");
    let academic = fs::read(baseline_dir.join("academic_records.csv")).unwrap();
    let aid = fs::read(baseline_dir.join("financial_aid_records.csv")).unwrap();
    assert_eq!(
        hex::encode(Sha256::digest(&academic)),
        "6f54d34ae6102aa256daecd3da15bbd58a44047378214382254d29043df0a8b4"
    );
    assert_eq!(
        hex::encode(Sha256::digest(&aid)),
        "2f7555aa65b4603663d8204b0585f5e0b8b06f50d0641d82fb950cf7ba071bc9"
    );

    for variant in [
        "low_fragmentation",
        "medium_fragmentation",
        "high_fragmentation",
    ] {
        assert_eq!(
            fs::read(run_dir.join(format!("variants/{variant}/academic_records.csv"))).unwrap(),
            academic
        );
        assert!(run_dir
            .join(format!("manifests/{variant}_manifest.json"))
            .is_file());
    }
    assert!(run_dir.join("config_snapshot/benchmark.yaml").is_file());
}

#[cfg(unix)]
#[test]
fn unchanged_academic_data_reuses_the_baseline_file() {
    use std::os::unix::fs::MetadataExt;

    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    fs::write(&config_path, CONFIG).unwrap();
    let run_dir = execute_run(&RunOptions {
        config_path,
        output_dir: temp.path().join("run"),
        overwrite: false,
    })
    .unwrap();

    let baseline = fs::metadata(run_dir.join("variants/baseline/academic_records.csv")).unwrap();
    let variant =
        fs::metadata(run_dir.join("variants/high_fragmentation/academic_records.csv")).unwrap();
    assert_eq!(baseline.dev(), variant.dev());
    assert_eq!(baseline.ino(), variant.ino());
}

#[test]
fn departmental_id_fragmentation_publishes_a_recoverable_crosswalk() {
    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    let config = CONFIG.replace(
        "high_fragmentation: {drop_row: 0.50}",
        "high_fragmentation: {identifier_mismatch: 0.20}",
    );
    fs::write(&config_path, config).unwrap();

    let run_dir = execute_run(&RunOptions {
        config_path,
        output_dir: temp.path().join("run"),
        overwrite: false,
    })
    .unwrap();
    let variant_dir = run_dir.join("variants/high_fragmentation");

    let aid_ids = csv::Reader::from_path(variant_dir.join("financial_aid_records.csv"))
        .unwrap()
        .records()
        .map(|row| row.unwrap().get(0).unwrap().to_string())
        .collect::<Vec<_>>();
    let crosswalk = csv::Reader::from_path(variant_dir.join("identity_crosswalk.csv"))
        .unwrap()
        .records()
        .map(|row| {
            let row = row.unwrap();
            (
                row.get(0).unwrap().to_string(),
                row.get(1).unwrap().to_string(),
            )
        })
        .collect::<std::collections::BTreeMap<_, _>>();

    let remapped = aid_ids
        .iter()
        .filter(|student_id| student_id.starts_with("financial-aid::"))
        .collect::<Vec<_>>();
    assert_eq!(remapped.len(), 100);
    assert_eq!(crosswalk.len(), 500);
    for financial_aid_id in remapped {
        let canonical_id = financial_aid_id.trim_start_matches("financial-aid::");
        assert_eq!(
            crosswalk.get(canonical_id).map(String::as_str),
            Some(financial_aid_id.as_str())
        );
    }

    let manifest: serde_json::Value = serde_json::from_slice(
        &fs::read(run_dir.join("manifests/high_fragmentation_manifest.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        manifest["selected_row_ids"]["identifier_mismatch"]
            .as_array()
            .unwrap()
            .len(),
        100
    );
}

#[test]
fn generation_refuses_to_replace_an_existing_run_without_permission() {
    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    let output_dir = temp.path().join("run");
    fs::write(&config_path, CONFIG).unwrap();
    fs::create_dir(&output_dir).unwrap();

    let error = execute_run(&RunOptions {
        config_path,
        output_dir,
        overwrite: false,
    })
    .unwrap_err();

    assert!(error.to_string().contains("already exists"));
}

#[test]
fn overwrite_works_when_the_config_is_inside_the_previous_run() {
    let temp = tempdir().unwrap();
    let output_dir = temp.path().join("run");
    fs::create_dir(&output_dir).unwrap();
    let config_path = output_dir.join("benchmark.yaml");
    fs::write(&config_path, CONFIG).unwrap();
    fs::write(output_dir.join("obsolete.txt"), "old run").unwrap();

    execute_run(&RunOptions {
        config_path,
        output_dir: output_dir.clone(),
        overwrite: true,
    })
    .unwrap();

    assert!(!output_dir.join("obsolete.txt").exists());
    assert!(output_dir.join("config_snapshot/benchmark.yaml").is_file());
    assert!(output_dir
        .join("manifests/high_fragmentation_manifest.json")
        .is_file());
}

#[test]
fn overwrite_rejects_parent_directory_components() {
    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    fs::write(&config_path, CONFIG).unwrap();

    let parent = temp.path().join("existing");
    let nested = parent.join("nested");
    fs::create_dir_all(&nested).unwrap();
    let marker = parent.join("keep.txt");
    fs::write(&marker, "must survive").unwrap();

    let error = execute_run(&RunOptions {
        config_path,
        output_dir: nested.join(".."),
        overwrite: true,
    })
    .unwrap_err();

    assert!(error.to_string().contains("parent"));
    assert!(marker.is_file());
}
