use high_ed_data_generator::config::BenchmarkConfig;
use high_ed_data_generator::output::{execute_run, RunOptions};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
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
fn config_rejects_duplicate_keys_at_top_level_and_nested_level() {
    let cases = [
        format!("version: 2\n{CONFIG}"),
        CONFIG.replace(
            "gpa: {mean: 2.8, std: 0.6, min: 0.0, max: 4.0}",
            "gpa: {mean: 2.8, mean: 3.1, std: 0.6, min: 0.0, max: 4.0}",
        ),
    ];

    for invalid in cases {
        let error = BenchmarkConfig::from_yaml(&invalid).unwrap_err();
        assert!(
            error.to_string().contains("duplicate"),
            "unexpected duplicate-key error: {error}"
        );
    }
}

#[test]
fn yaml_parser_rejects_recursive_alias_without_panicking() {
    #[derive(Debug, serde::Deserialize)]
    struct RecursiveNode {
        #[allow(dead_code)]
        child: Option<Box<RecursiveNode>>,
    }

    // 小型自引用 alias 足以验证递归保护, 不构造会消耗大量内存的 alias bomb.
    let parsed = std::panic::catch_unwind(|| {
        serde_yaml_bw::from_str::<RecursiveNode>("&root {child: *root}")
    });
    let error = parsed
        .expect("recursive YAML must return an error instead of panicking")
        .unwrap_err();
    assert!(
        error.to_string().contains("recursion limit exceeded"),
        "unexpected recursive-alias error: {error}"
    );
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
fn config_accepts_versions_one_and_two() {
    BenchmarkConfig::from_yaml(CONFIG).unwrap();
    BenchmarkConfig::from_yaml(&CONFIG.replace("version: 1", "version: 2")).unwrap();

    let error =
        BenchmarkConfig::from_yaml(&CONFIG.replace("version: 1", "version: 3")).unwrap_err();
    assert!(error.to_string().contains("version must be 1 or 2"));
}

#[test]
fn version_one_rejects_version_two_fragmentation_features() {
    for (feature, invalid) in [
        (
            "publication_delay",
            CONFIG.replace(
                "high_fragmentation: {drop_row: 0.50}",
                "high_fragmentation: {publication_delay: 0.20}",
            ),
        ),
        (
            "aid_status_code_drift",
            CONFIG.replace(
                "high_fragmentation: {drop_row: 0.50}",
                "high_fragmentation: {aid_status_code_drift: 0.20}",
            ),
        ),
        (
            "late_publication_delay_days",
            CONFIG.replace(
                "disbursement_offset_days: {min: -10, max: 30}",
                "disbursement_offset_days: {min: -10, max: 30}\n    late_publication_delay_days: 14",
            ),
        ),
    ] {
        let error = BenchmarkConfig::from_yaml(&invalid).unwrap_err();
        let message = error.to_string();
        assert!(
            message.contains("version 2"),
            "version 1 should reject {feature} with a clear migration hint: {error}"
        );
        assert!(
            message.contains(feature),
            "validation error should identify {feature}: {error}"
        );
    }
}

#[test]
fn config_rejects_invalid_late_publication_delay() {
    for delay in ["0", "-1", "9223372036854775807"] {
        let invalid = CONFIG.replace(
            "disbursement_offset_days: {min: -10, max: 30}",
            &format!(
                "disbursement_offset_days: {{min: -10, max: 30}}\n    late_publication_delay_days: {delay}"
            ),
        );
        let error = BenchmarkConfig::from_yaml(&invalid).unwrap_err();
        assert!(
            error.to_string().contains("late publication delay"),
            "unexpected validation error for {delay}: {error}"
        );
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
fn temporal_and_semantic_fragmentation_publish_recoverable_governance_inputs() {
    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    let config = CONFIG
        .replace("version: 1", "version: 2")
        .replace(
            "high_fragmentation: {drop_row: 0.50}",
            "high_fragmentation: {identifier_mismatch: 0.20, publication_delay: 0.20, aid_status_code_drift: 0.20}",
        );
    fs::write(&config_path, config).unwrap();

    let run_dir = execute_run(&RunOptions {
        config_path,
        output_dir: temp.path().join("run"),
        overwrite: false,
    })
    .unwrap();
    let variant_dir = run_dir.join("variants/high_fragmentation");

    let current = read_aid_rows(&variant_dir.join("financial_aid_records.csv"));
    let late = read_aid_rows(&variant_dir.join("financial_aid_late_arrivals.csv"));
    assert_eq!(current.len(), 400);
    assert_eq!(late.len(), 100);

    let all_ids = current
        .iter()
        .chain(&late)
        .map(|row| row.0.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(all_ids.len(), 500);

    let drifted_statuses = current
        .iter()
        .chain(&late)
        .filter(|row| row.1.starts_with("financial-aid::"))
        .map(|row| row.1.as_str())
        .collect::<Vec<_>>();
    assert_eq!(drifted_statuses.len(), 100);

    let status_crosswalk = csv::Reader::from_path(variant_dir.join("aid_status_crosswalk.csv"))
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
    for local_status in drifted_statuses {
        assert!(status_crosswalk.contains_key(local_status));
    }

    let manifest: serde_json::Value = serde_json::from_slice(
        &fs::read(run_dir.join("manifests/high_fragmentation_manifest.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        manifest["selected_row_ids"]["publication_delay"]
            .as_array()
            .unwrap()
            .len(),
        100
    );
    assert_eq!(
        manifest["selected_row_ids"]["aid_status_code_drift"]
            .as_array()
            .unwrap()
            .len(),
        100
    );

    let (headers, events) =
        read_csv_by_header(&variant_dir.join("financial_aid_publication_events.csv"));
    assert_eq!(
        headers,
        [
            "event_id",
            "financial_aid_student_id",
            "event_time",
            "observed_at",
            "published_at",
            "arrival_stream",
        ]
    );
    assert_eq!(events.len(), 500);
    assert_eq!(
        events
            .iter()
            .filter(|row| row["arrival_stream"] == "current")
            .count(),
        400
    );
    assert_eq!(
        events
            .iter()
            .filter(|row| row["arrival_stream"] == "late")
            .count(),
        100
    );
    assert_eq!(
        events
            .iter()
            .filter(|row| row["financial_aid_student_id"].starts_with("financial-aid::"))
            .count(),
        100
    );
    for event in &events {
        assert_eq!(
            event["event_id"],
            format!(
                "aid-disbursement::{}::{}",
                event["financial_aid_student_id"], event["event_time"]
            )
        );
        assert_eq!(
            event["observed_at"],
            format!("{}T00:00:00Z", event["event_time"])
        );
        match event["arrival_stream"].as_str() {
            "current" => assert_eq!(event["published_at"], "2024-10-02T00:00:00Z"),
            "late" => assert_eq!(event["published_at"], "2024-10-09T00:00:00Z"),
            stream => panic!("unexpected arrival stream: {stream}"),
        }
    }

    assert_eq!(manifest["manifest_version"], 3);
    assert_eq!(
        manifest["temporal"],
        serde_json::json!({
            "contract_version": 1,
            "timezone": "UTC",
            "logical_time": true,
            "snapshots": {
                "current": {
                    "published_at": "2024-10-02T00:00:00Z",
                    "event_time_watermark": "2024-10-01T00:00:00Z"
                },
                "replayed": {
                    "published_at": "2024-10-09T00:00:00Z",
                    "event_time_watermark": "2024-10-01T00:00:00Z"
                }
            },
            "current_record_count": 400,
            "late_record_count": 100
        })
    );
    assert_eq!(
        manifest["variant_file_hashes"]["financial_aid_publication_events"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
}

#[test]
fn temporal_publication_is_byte_deterministic() {
    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    fs::write(&config_path, CONFIG).unwrap();

    for run_name in ["run-one", "run-two"] {
        execute_run(&RunOptions {
            config_path: config_path.clone(),
            output_dir: temp.path().join(run_name),
            overwrite: false,
        })
        .unwrap();
    }

    for relative_path in [
        "variants/high_fragmentation/financial_aid_publication_events.csv",
        "manifests/high_fragmentation_manifest.json",
    ] {
        assert_eq!(
            fs::read(temp.path().join("run-one").join(relative_path)).unwrap(),
            fs::read(temp.path().join("run-two").join(relative_path)).unwrap(),
            "non-deterministic output at {relative_path}"
        );
    }
}

#[test]
fn dropped_rows_do_not_create_phantom_publication_events() {
    let temp = tempdir().unwrap();
    let config_path = temp.path().join("benchmark.yaml");
    let config = CONFIG.replace("version: 1", "version: 2").replace(
        "high_fragmentation: {drop_row: 0.50}",
        "high_fragmentation: {drop_row: 1.0, publication_delay: 1.0}",
    );
    fs::write(&config_path, config).unwrap();

    let run_dir = execute_run(&RunOptions {
        config_path,
        output_dir: temp.path().join("run"),
        overwrite: false,
    })
    .unwrap();
    let variant_dir = run_dir.join("variants/high_fragmentation");
    assert!(read_aid_rows(&variant_dir.join("financial_aid_records.csv")).is_empty());
    assert!(read_aid_rows(&variant_dir.join("financial_aid_late_arrivals.csv")).is_empty());

    let (headers, events) =
        read_csv_by_header(&variant_dir.join("financial_aid_publication_events.csv"));
    assert_eq!(
        headers,
        [
            "event_id",
            "financial_aid_student_id",
            "event_time",
            "observed_at",
            "published_at",
            "arrival_stream",
        ]
    );
    assert!(events.is_empty());

    let manifest = read_manifest(&run_dir);
    assert_eq!(
        manifest["selected_row_ids"]["publication_delay"]
            .as_array()
            .unwrap()
            .len(),
        500
    );
    assert_eq!(manifest["temporal"]["current_record_count"], 0);
    assert_eq!(manifest["temporal"]["late_record_count"], 0);
    assert_eq!(
        manifest["temporal"]["snapshots"]["current"]["event_time_watermark"],
        "2024-10-01T00:00:00Z"
    );
}

#[test]
fn late_replay_delay_does_not_change_business_dataset_identity() {
    let temp = tempdir().unwrap();
    let seven_day_config = CONFIG
        .replace("version: 1", "version: 2")
        .replace(
            "disbursement_offset_days: {min: -10, max: 30}",
            "disbursement_offset_days: {min: -10, max: 30}\n    late_publication_delay_days: 7",
        )
        .replace(
            "high_fragmentation: {drop_row: 0.50}",
            "high_fragmentation: {publication_delay: 0.20}",
        );
    let fourteen_day_config = seven_day_config.replace(
        "late_publication_delay_days: 7",
        "late_publication_delay_days: 14",
    );

    for (run_name, config) in [
        ("seven-days", seven_day_config),
        ("fourteen-days", fourteen_day_config),
    ] {
        let config_path = temp.path().join(format!("{run_name}.yaml"));
        fs::write(&config_path, config).unwrap();
        execute_run(&RunOptions {
            config_path,
            output_dir: temp.path().join(run_name),
            overwrite: false,
        })
        .unwrap();
    }

    let seven_manifest = read_manifest(temp.path().join("seven-days"));
    let fourteen_manifest = read_manifest(temp.path().join("fourteen-days"));
    assert_eq!(
        seven_manifest["baseline_dataset_id"],
        fourteen_manifest["baseline_dataset_id"]
    );
    assert_eq!(
        seven_manifest["temporal"]["snapshots"]["replayed"]["published_at"],
        "2024-10-09T00:00:00Z"
    );
    assert_eq!(
        fourteen_manifest["temporal"]["snapshots"]["replayed"]["published_at"],
        "2024-10-16T00:00:00Z"
    );

    let seven_dir = temp.path().join("seven-days/variants/high_fragmentation");
    let fourteen_dir = temp
        .path()
        .join("fourteen-days/variants/high_fragmentation");
    for file_name in [
        "academic_records.csv",
        "financial_aid_records.csv",
        "financial_aid_late_arrivals.csv",
    ] {
        assert_eq!(
            fs::read(seven_dir.join(file_name)).unwrap(),
            fs::read(fourteen_dir.join(file_name)).unwrap()
        );
    }
    assert_ne!(
        fs::read(seven_dir.join("financial_aid_publication_events.csv")).unwrap(),
        fs::read(fourteen_dir.join("financial_aid_publication_events.csv")).unwrap()
    );
}

fn read_csv_by_header(path: &Path) -> (Vec<String>, Vec<BTreeMap<String, String>>) {
    let mut reader = csv::Reader::from_path(path).unwrap();
    let headers = reader
        .headers()
        .unwrap()
        .iter()
        .map(str::to_string)
        .collect::<Vec<_>>();
    let rows = reader
        .records()
        .map(|row| {
            headers
                .iter()
                .cloned()
                .zip(row.unwrap().iter().map(str::to_string))
                .collect()
        })
        .collect();
    (headers, rows)
}

fn read_manifest(run_dir: impl AsRef<Path>) -> serde_json::Value {
    serde_json::from_slice(
        &fs::read(
            run_dir
                .as_ref()
                .join("manifests/high_fragmentation_manifest.json"),
        )
        .unwrap(),
    )
    .unwrap()
}

fn read_aid_rows(path: &std::path::Path) -> Vec<(String, String)> {
    csv::Reader::from_path(path)
        .unwrap()
        .records()
        .map(|row| {
            let row = row.unwrap();
            (
                row.get(0).unwrap().to_string(),
                row.get(2).unwrap().to_string(),
            )
        })
        .collect()
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
