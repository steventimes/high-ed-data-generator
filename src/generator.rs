use crate::args::{Args, MissingnessPattern, SchemaVersion, TermCodeStyle};
use crate::catalogs::{build_major_sampler, cip_for_subject, majors, SUBJECT_CATALOG};
use crate::io_utils::{ensure_dir, write_csv, write_json};
use crate::models::{
    class_level_from_credits, FacultyCourseRow, FacultyCourseWideRow, FinancialAidRow,
    FinancialAidWideRow, IdentityCrosswalkRow, IdentityCrosswalkWideRow, LmsActivityRawRow,
    LmsActivityRawWideRow, LmsActivityRow, LmsActivityWideRow, LocalPostsecondaryHold,
    LocalPostsecondaryHoldWide, RegistrarCourseEnrollmentRow, RegistrarCourseEnrollmentWideRow,
    SisEnrollmentRow, SisEnrollmentWideRow, StudentDemographicsRow, StudentDemographicsWideRow,
    StudentInternal, StudentLifecycle,
};
use crate::term::{Term, TermSeason};
use anyhow::Result;
use chrono::{Duration, NaiveDate};
use rand::distributions::{Distribution, WeightedIndex};
use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;
use rand_distr::Normal;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::path::Path;
use uuid::Uuid;

#[derive(Clone)]
struct InstructorProfile {
    instructor_id: String,
    faculty_rank: String,
    tenure_status: String,
    employment_status: String,
    home_department: String,
}

#[derive(Clone)]
struct SectionMeta {
    course_section_id: String,
    course_prefix: String,
    course_number: String,
    course_name: String,
    course_cip_code: String,
    course_level_type: String,
    delivery_method: String,
    instructor_id: String,
    meeting_pattern: Option<String>,
    section_capacity: u32,
    credits_attempted_course: u32,
    course_begin_date: String,
    course_end_date: String,
}

pub fn generate(args: &Args, terms: &[Term]) -> Result<()> {
    let mut rng = ChaCha8Rng::seed_from_u64(args.seed);
    ensure_dir(&args.out_dir)?;
    ensure_dir(&args.out_dir.join("terms"))?;

    let emit_slim = matches!(args.schema_version, SchemaVersion::Slim | SchemaVersion::Both);
    let emit_wide = matches!(args.schema_version, SchemaVersion::Wide | SchemaVersion::Both);

    let major_catalog = majors();
    let major_dist = build_major_sampler();
    let mut used_emails = HashMap::<String, u32>::new();
    let mut used_erp_ids = HashSet::<String>::new();
    let mut students = Vec::<StudentInternal>::with_capacity(args.students);
    let first_names = [
        "Alex", "Jordan", "Taylor", "Riley", "Casey", "Morgan", "Avery", "Jamie", "Quinn",
        "Cameron", "Devin", "Parker", "Reese", "Skyler", "Rowan", "Sydney", "Drew", "Hayden",
    ];
    let last_names = [
        "Kim", "Patel", "Garcia", "Nguyen", "Johnson", "Smith", "Brown", "Davis", "Miller",
        "Wilson", "Martinez", "Anderson", "Thomas", "Jackson", "White", "Harris", "Clark",
    ];

    for i in 0..args.students {
        let student_id = format!("S{:0>6}", i + 1);
        let full_name = format!(
            "{} {}",
            first_names[rng.gen_range(0..first_names.len())],
            last_names[rng.gen_range(0..last_names.len())]
        );
        let email_local = unique_username(&slugify_username(&full_name), &mut used_emails);
        let institutional_email = format!("{email_local}@university.edu");
        let lms_user_id = format!("lms_{}", student_id.to_ascii_lowercase());
        let sis_user_id = format!("sis_{}", student_id.to_ascii_lowercase());
        let integration_id = format!("int_{}", student_id.to_ascii_lowercase());
        let erp_person_id = unique_erp_id(&mut rng, &mut used_erp_ids);

        let initial_enrollment_term_idx = if rng.gen::<f64>() < 0.74 {
            0
        } else {
            rng.gen_range(0..args.terms)
        };
        let entry_term = terms[initial_enrollment_term_idx];
        let cohort_year = entry_term.academic_year();
        let admit_type = sample_admit_type(&mut rng).to_string();
        let student_level = sample_student_level(&mut rng).to_string();
        let academic_career = academic_career_for_level(&student_level).to_string();
        let major = major_catalog[major_dist.sample(&mut rng)];
        let second_major = sample_second_major(&mut rng, major.label, &major_dist, major_catalog);
        let transfer_credits_accepted = if admit_type == "transfer" {
            rng.gen_range(8..=72)
        } else {
            0
        };

        let age_at_entry = sample_age_at_entry(&mut rng);
        let birth_year = entry_term.year - age_at_entry as i32;
        let hs_unweighted_gpa = if admit_type == "first_time" {
            Some(round2(rng.gen_range(2.0..=4.0)))
        } else {
            None
        };
        let hs_weighted_gpa = hs_unweighted_gpa.map(|g| round2((g + rng.gen_range(0.1..=0.9)).min(5.0)));
        let base_gpa = sample_base_gpa(&mut rng);
        let first_gen_flag = ternary_flag(&mut rng, 0.33, 0.05);
        let veteran_status = ternary_flag(&mut rng, 0.07, 0.03);
        let disability_status = ternary_flag(&mut rng, 0.13, 0.04);
        let race_ethnicity = sample_race_ethnicity(&mut rng).to_string();
        let gender = sample_gender(&mut rng).to_string();
        let country_of_origin = sample_country_of_origin(&mut rng).to_string();
        let citizenship_status = sample_citizenship_status(&mut rng, &country_of_origin).to_string();
        let state_of_residence_at_entry = sample_state_of_residence(&mut rng);
        let residency_status = sample_residency_status(&mut rng, &country_of_origin).to_string();
        let initial_cumulative_credits = if admit_type == "transfer" {
            transfer_credits_accepted
        } else {
            0
        };

        students.push(StudentInternal {
            student_id,
            institutional_email,
            lms_user_id,
            sis_user_id,
            integration_id,
            erp_person_id,
            birth_year,
            gender,
            race_ethnicity,
            first_gen_flag,
            veteran_status,
            disability_status,
            age_at_entry,
            state_of_residence_at_entry,
            country_of_origin,
            citizenship_status,
            initial_enrollment_term_idx,
            cohort_year,
            admit_type,
            hs_unweighted_gpa,
            hs_weighted_gpa,
            student_level,
            academic_career,
            residency_status,
            major_label: major.label.to_string(),
            major_cip_code: major.cip_code.to_string(),
            major_college: major.college.to_string(),
            likely_subject: major.likely_subject.to_string(),
            second_major_cip_code: second_major.map(|m| m.cip_code.to_string()),
            transfer_credits_accepted,
            base_gpa,
            cumulative_credits_earned: initial_cumulative_credits,
            cumulative_gpa_credits: initial_cumulative_credits,
            cumulative_quality_points: base_gpa * initial_cumulative_credits as f64,
            financial_need_index: round2((rng.gen::<f64>() + rng.gen::<f64>()) / 2.0),
            engagement_index: round2((rng.gen::<f64>() + rng.gen::<f64>()) / 2.0),
            residential_flag: rng.gen::<f64>() < 0.62,
            lifecycle: StudentLifecycle::Active,
            stopout_terms_remaining: 0,
        });
    }

    let demographics_wide: Vec<StudentDemographicsWideRow> = students
        .iter()
        .map(|s| StudentDemographicsWideRow {
            student_id: s.student_id.clone(),
            birth_year: s.birth_year,
            gender: s.gender.clone(),
            race_ethnicity: s.race_ethnicity.clone(),
            first_gen_flag: s.first_gen_flag,
            veteran_status: s.veteran_status,
            disability_status: s.disability_status,
            age_at_entry: s.age_at_entry,
            country_of_origin: s.country_of_origin.clone(),
            citizenship_status: s.citizenship_status.clone(),
            initial_enrollment_term: terms[s.initial_enrollment_term_idx].code(),
            cohort_year: s.cohort_year.clone(),
            admit_type: s.admit_type.clone(),
            hs_unweighted_gpa: s.hs_unweighted_gpa,
            state_of_residence_at_entry: s.state_of_residence_at_entry.clone(),
            hs_weighted_gpa: s.hs_weighted_gpa,
        })
        .collect();
    let demographics_slim: Vec<StudentDemographicsRow> =
        demographics_wide.iter().map(to_slim_demographics).collect();
    write_versioned_csv(
        &args.out_dir,
        "student_demographics",
        &demographics_slim,
        &demographics_wide,
        emit_slim,
        emit_wide,
    )?;

    let crosswalk_wide = build_crosswalk_rows(&students, terms, args, &mut rng);
    let crosswalk_slim: Vec<IdentityCrosswalkRow> =
        crosswalk_wide.iter().map(to_slim_crosswalk).collect();
    write_versioned_csv(
        &args.out_dir,
        "identity_crosswalk_integration",
        &crosswalk_slim,
        &crosswalk_wide,
        emit_slim,
        emit_wide,
    )?;

    let instructors = build_instructor_pool(&mut rng, 220);
    let instructor_lookup: HashMap<String, InstructorProfile> = instructors
        .iter()
        .map(|i| (i.instructor_id.clone(), i.clone()))
        .collect();
    let gpa_noise = Normal::new(0.0, 0.40).expect("gpa noise");
    let mut used_hold_ids = HashSet::<String>::new();

    for (term_idx, term) in terms.iter().enumerate() {
        let term_code = term.code();
        let term_dir = args.out_dir.join("terms").join(&term_code);
        ensure_dir(&term_dir)?;
        let academic_year = term.academic_year();
        let term_label = term.label().to_string();
        let (term_start, term_end) = term_window(term);
        let term_code_value = term_code_for_wide(args.term_code_style, &term_code);

        let mut sis_wide = Vec::<SisEnrollmentWideRow>::new();
        let mut reg_wide = Vec::<RegistrarCourseEnrollmentWideRow>::new();
        let mut faculty_wide = Vec::<FacultyCourseWideRow>::new();
        let mut lms_raw_wide = Vec::<LmsActivityRawWideRow>::new();
        let mut lms_derived_wide = Vec::<LmsActivityWideRow>::new();
        let mut fin_wide = Vec::<FinancialAidWideRow>::new();
        let mut hold_wide = Vec::<LocalPostsecondaryHoldWide>::new();
        let mut section_meta = HashMap::<String, SectionMeta>::new();
        let mut instructor_load_credits = HashMap::<String, u32>::new();

        for student in students.iter_mut() {
            if term_idx < student.initial_enrollment_term_idx {
                continue;
            }
            let enrollment_status = determine_enrollment_status(&mut rng, student, args, term_idx);
            let first_time_flag =
                term_idx == student.initial_enrollment_term_idx && student.admit_type == "first_time";
            let enrollment_type = if term_idx == student.initial_enrollment_term_idx {
                student.admit_type.clone()
            } else {
                "continuing".to_string()
            };

            if enrollment_status == "enrolled"
                && term_idx > student.initial_enrollment_term_idx
                && rng.gen::<f64>() < args.major_change_rate
            {
                let new_major = majors()[build_major_sampler().sample(&mut rng)];
                student.major_label = new_major.label.to_string();
                student.major_cip_code = new_major.cip_code.to_string();
                student.major_college = new_major.college.to_string();
                student.likely_subject = new_major.likely_subject.to_string();
            }

            let mut credits_attempted = None;
            let mut credits_earned = None;
            let mut term_gpa = None;
            let mut full_time_flag = false;
            let mut student_registrar_rows = Vec::<RegistrarCourseEnrollmentWideRow>::new();

            if enrollment_status == "enrolled" {
                let attempted = sample_term_credits(&mut rng, &student.student_level);
                let sampled = (student.base_gpa + gpa_noise.sample(&mut rng)).clamp(0.0, 4.0);
                let gpa = round2(sampled);
                let earned = sample_credits_earned(&mut rng, attempted, gpa);
                credits_attempted = Some(attempted);
                credits_earned = Some(earned);
                term_gpa = Some(gpa);
                full_time_flag = attempted >= full_time_threshold(&student.student_level);
                student.cumulative_credits_earned += earned;
                student.cumulative_gpa_credits += attempted;
                student.cumulative_quality_points += gpa * attempted as f64;
                student.base_gpa = round2((student.base_gpa * 0.75 + gpa * 0.25).clamp(0.0, 4.0));

                student_registrar_rows = build_registrar_rows(
                    &mut rng,
                    student,
                    &academic_year,
                    &term_label,
                    &term_code,
                    term_start,
                    term_end,
                    attempted,
                    gpa,
                    &instructors,
                    &mut section_meta,
                    &mut instructor_load_credits,
                );

                let lms_missing_rate = effective_missing_rate(
                    args.lms_missing_rate,
                    args.missingness_pattern,
                    term_idx,
                    student,
                    true,
                );
                if rng.gen::<f64>() >= lms_missing_rate {
                    let (raw_rows, derived_row) = build_lms_rows(
                        &mut rng,
                        student,
                        &academic_year,
                        &term_label,
                        term_start,
                        term_end,
                        &student_registrar_rows,
                        args.identifier_missing_rate,
                    );
                    lms_raw_wide.extend(raw_rows);
                    lms_derived_wide.push(derived_row);
                }

                let fin_missing_rate = effective_missing_rate(
                    args.fin_missing_rate,
                    args.missingness_pattern,
                    term_idx,
                    student,
                    false,
                );
                if rng.gen::<f64>() >= fin_missing_rate {
                    fin_wide.push(build_financial_aid_row(
                        &mut rng,
                        student,
                        term,
                        &academic_year,
                        &term_label,
                        term_code_value.clone(),
                        attempted,
                        full_time_flag,
                        args.aid_application_rate,
                    ));
                }
            }

            reg_wide.extend(student_registrar_rows);
            let cumulative_gpa = gpa_or_none(student.cumulative_quality_points, student.cumulative_gpa_credits);
            sis_wide.push(SisEnrollmentWideRow {
                student_id: student.student_id.clone(),
                academic_year: academic_year.clone(),
                term: term_label.clone(),
                student_level: student.student_level.clone(),
                major_cip_code: student.major_cip_code.clone(),
                major_label: student.major_label.clone(),
                second_major_cip_code: student.second_major_cip_code.clone(),
                enrollment_type,
                first_time_flag,
                transfer_credits_accepted: student.transfer_credits_accepted,
                credits_attempted,
                credits_earned,
                term_gpa,
                cumulative_gpa,
                credits_earned_cumulative: Some(student.cumulative_credits_earned),
                enrollment_status: enrollment_status.clone(),
                full_time_flag,
                term_start_date: term_start.to_string(),
                term_end_date: term_end.to_string(),
                term_code: term_code_value.clone(),
                class_level: class_level_for_student(student),
                academic_career: student.academic_career.clone(),
                college: student.major_college.clone(),
                residency_status: student.residency_status.clone(),
                institutional_email: if enrollment_status == "enrolled" {
                    Some(student.institutional_email.clone())
                } else {
                    None
                },
            });

            let hold_probability = compute_hold_probability(
                args.hold_rate,
                student.financial_need_index,
                &enrollment_status,
                cumulative_gpa,
            );
            if rng.gen::<f64>() < hold_probability {
                let hold_count = if rng.gen::<f64>() < 0.15 { 2 } else { 1 };
                for _ in 0..hold_count {
                    hold_wide.push(build_hold_row(
                        &mut rng,
                        &student.student_id,
                        &term_label,
                        term_code_value.clone(),
                        term_start,
                        term_end,
                        args.hold_clearance_lag_days,
                        &mut used_hold_ids,
                    ));
                }
            }
        }

        let mut section_counts = HashMap::<String, u32>::new();
        for row in &reg_wide {
            *section_counts.entry(row.course_section_id.clone()).or_insert(0) += 1;
        }
        for row in &mut reg_wide {
            let count = section_counts.get(&row.course_section_id).copied().unwrap_or(0);
            row.section_enrollment = count;
            row.section_capacity = row.section_capacity.max(count + 2);
        }
        for meta in section_meta.values() {
            if let Some(instructor) = instructor_lookup.get(&meta.instructor_id) {
                faculty_wide.push(FacultyCourseWideRow {
                    instructor_id: meta.instructor_id.clone(),
                    academic_year: academic_year.clone(),
                    term: term_label.clone(),
                    course_section_id: meta.course_section_id.clone(),
                    faculty_rank: instructor.faculty_rank.clone(),
                    tenure_status: instructor.tenure_status.clone(),
                    employment_status: instructor.employment_status.clone(),
                    teaching_load_credits: instructor_load_credits
                        .get(&meta.instructor_id)
                        .copied()
                        .unwrap_or(meta.credits_attempted_course),
                    home_department: instructor.home_department.clone(),
                    primary_instruction_mode: meta.delivery_method.clone(),
                });
            }
        }

        let sis_slim: Vec<SisEnrollmentRow> = sis_wide.iter().map(to_slim_sis).collect();
        let reg_slim: Vec<RegistrarCourseEnrollmentRow> =
            reg_wide.iter().map(to_slim_registrar).collect();
        let faculty_slim: Vec<FacultyCourseRow> = faculty_wide.iter().map(to_slim_faculty).collect();
        let lms_raw_slim: Vec<LmsActivityRawRow> = lms_raw_wide.iter().map(to_slim_lms_raw).collect();
        let lms_slim: Vec<LmsActivityRow> = lms_derived_wide.iter().map(to_slim_lms).collect();
        let fin_slim: Vec<FinancialAidRow> = fin_wide.iter().map(to_slim_financial).collect();
        let hold_slim: Vec<LocalPostsecondaryHold> = hold_wide.iter().map(to_slim_hold).collect();

        write_versioned_csv(&term_dir, "sis_enrollments", &sis_slim, &sis_wide, emit_slim, emit_wide)?;
        write_versioned_csv(
            &term_dir,
            "registrar_course_enrollments",
            &reg_slim,
            &reg_wide,
            emit_slim,
            emit_wide,
        )?;
        write_versioned_csv(&term_dir, "faculty_courses", &faculty_slim, &faculty_wide, emit_slim, emit_wide)?;
        write_versioned_csv(&term_dir, "lms_activity_raw", &lms_raw_slim, &lms_raw_wide, emit_slim, emit_wide)?;
        write_versioned_csv(&term_dir, "lms_activity", &lms_slim, &lms_derived_wide, emit_slim, emit_wide)?;
        write_versioned_csv(&term_dir, "financial_aid", &fin_slim, &fin_wide, emit_slim, emit_wide)?;
        write_versioned_json(
            &term_dir,
            "local_postsecondary_holds",
            &hold_slim,
            &hold_wide,
            emit_slim,
            emit_wide,
            args.pretty_json,
        )?;
    }

    let metadata = serde_json::json!({
        "institution": "Synthetic University",
        "students": args.students,
        "start_term": args.start_term,
        "terms": terms.iter().map(|t| t.code()).collect::<Vec<_>>(),
        "seed": args.seed,
        "schema_version": schema_label(args.schema_version),
        "term_code_style": term_code_style_label(args.term_code_style),
        "missingness_pattern": missingness_label(args.missingness_pattern),
        "financial_aid_grain": "term",
        "financial_aid_termization_rule": "annual_cost_of_attendance * seasonal share (fall=0.4, spring=0.4, summer=0.2)",
        "systems": { "lms": "Canvas-compatible", "registration": "SIS", "aid": "ERP" },
        "knobs": {
            "major_change_rate": args.major_change_rate,
            "stopout_rate": args.stopout_rate,
            "reenroll_after_stopout_rate": args.reenroll_after_stopout_rate,
            "withdrawal_rate": args.withdrawal_rate,
            "transfer_out_rate": args.transfer_out_rate,
            "lms_missing_rate": args.lms_missing_rate,
            "fin_missing_rate": args.fin_missing_rate,
            "hold_rate": args.hold_rate,
            "crosswalk_mismatch_rate": args.crosswalk_mismatch_rate,
            "identifier_missing_rate": args.identifier_missing_rate,
            "hold_clearance_lag_days": args.hold_clearance_lag_days,
            "aid_application_rate": args.aid_application_rate
        }
    });
    write_json(&args.out_dir.join("metadata.json"), &metadata, true)?;
    eprintln!("Done. Wrote {} schema outputs to {}", schema_label(args.schema_version), args.out_dir.display());
    Ok(())
}

fn write_versioned_csv<TSlim: Serialize, TWide: Serialize>(
    out_dir: &Path,
    base_name: &str,
    slim_rows: &[TSlim],
    wide_rows: &[TWide],
    emit_slim: bool,
    emit_wide: bool,
) -> Result<()> {
    let base_path = out_dir.join(format!("{base_name}.csv"));
    if emit_slim {
        write_csv(&base_path, slim_rows)?;
    }
    if emit_wide {
        let wide_path = if emit_slim { out_dir.join(format!("{base_name}_wide.csv")) } else { base_path };
        write_csv(&wide_path, wide_rows)?;
    }
    Ok(())
}

fn write_versioned_json<TSlim: Serialize, TWide: Serialize>(
    out_dir: &Path,
    base_name: &str,
    slim_rows: &[TSlim],
    wide_rows: &[TWide],
    emit_slim: bool,
    emit_wide: bool,
    pretty_json: bool,
) -> Result<()> {
    let base_path = out_dir.join(format!("{base_name}.json"));
    if emit_slim {
        write_json(&base_path, slim_rows, pretty_json)?;
    }
    if emit_wide {
        let wide_path = if emit_slim { out_dir.join(format!("{base_name}_wide.json")) } else { base_path };
        write_json(&wide_path, wide_rows, pretty_json)?;
    }
    Ok(())
}

fn build_crosswalk_rows<R: Rng>(
    students: &[StudentInternal],
    terms: &[Term],
    args: &Args,
    rng: &mut R,
) -> Vec<IdentityCrosswalkWideRow> {
    let mut rows = Vec::<IdentityCrosswalkWideRow>::new();
    let n = students.len();
    let mismatch_count = ((n as f64) * args.crosswalk_mismatch_rate).round() as usize;
    let mut indices: Vec<usize> = (0..n).collect();
    indices.shuffle(rng);
    let mismatched = &indices[..mismatch_count.min(n)];
    let mut mismatch_map = HashMap::<usize, usize>::new();
    if mismatched.len() > 1 {
        for (i, current) in mismatched.iter().enumerate() {
            let next = mismatched[(i + 1) % mismatched.len()];
            mismatch_map.insert(*current, next);
        }
    }

    for (idx, student) in students.iter().enumerate() {
        let mapped_idx = mismatch_map.get(&idx).copied().unwrap_or(idx);
        let mapped_student = &students[mapped_idx];
        let mismatched_row = mapped_idx != idx;
        let (start_date, _) = term_window(&terms[student.initial_enrollment_term_idx]);

        let mut lms_user_id = Some(mapped_student.lms_user_id.clone());
        let mut sis_user_id = Some(mapped_student.sis_user_id.clone());
        let mut integration_id = Some(mapped_student.integration_id.clone());
        let mut erp_person_id = Some(mapped_student.erp_person_id.clone());
        let mut institutional_email = Some(mapped_student.institutional_email.clone());
        if rng.gen::<f64>() < args.identifier_missing_rate {
            lms_user_id = None;
        }
        if rng.gen::<f64>() < args.identifier_missing_rate {
            sis_user_id = None;
        }
        if rng.gen::<f64>() < args.identifier_missing_rate {
            integration_id = None;
        }
        if rng.gen::<f64>() < args.identifier_missing_rate {
            erp_person_id = None;
        }
        if rng.gen::<f64>() < args.identifier_missing_rate {
            institutional_email = None;
        }

        rows.push(IdentityCrosswalkWideRow {
            student_id: student.student_id.clone(),
            lms_user_id,
            sis_user_id,
            integration_id,
            erp_person_id,
            institutional_email,
            effective_start_date: start_date.to_string(),
            effective_end_date: None,
            active_flag: true,
            source_system: "IDM".to_string(),
            match_rule: if mismatched_row {
                "manual_merge".to_string()
            } else {
                "exact_id".to_string()
            },
            match_confidence: if mismatched_row {
                round2(rng.gen_range(0.55..0.87))
            } else {
                round2(rng.gen_range(0.95..1.0))
            },
        });
    }
    rows
}

fn build_instructor_pool<R: Rng>(rng: &mut R, count: usize) -> Vec<InstructorProfile> {
    let departments = [
        "Computer Science",
        "Biology",
        "Economics",
        "Psychology",
        "Neuroscience",
        "Chemistry",
        "Mathematics",
        "Politics",
        "History",
        "Philosophy",
        "Business",
        "Sociology",
        "Environmental Studies",
    ];
    let mut list = Vec::with_capacity(count);
    for i in 0..count {
        let faculty_rank = sample_faculty_rank(rng).to_string();
        list.push(InstructorProfile {
            instructor_id: format!("I{:0>5}", i + 1),
            tenure_status: sample_tenure_status(rng, &faculty_rank).to_string(),
            employment_status: if rng.gen::<f64>() < 0.76 {
                "Full-time".to_string()
            } else {
                "Part-time".to_string()
            },
            home_department: departments[rng.gen_range(0..departments.len())].to_string(),
            faculty_rank,
        });
    }
    list
}

#[allow(clippy::too_many_arguments)]
fn build_registrar_rows<R: Rng>(
    rng: &mut R,
    student: &StudentInternal,
    academic_year: &str,
    term_label: &str,
    term_code: &str,
    term_start: NaiveDate,
    term_end: NaiveDate,
    credits_attempted: u32,
    term_gpa: f64,
    instructors: &[InstructorProfile],
    section_meta: &mut HashMap<String, SectionMeta>,
    instructor_load_credits: &mut HashMap<String, u32>,
) -> Vec<RegistrarCourseEnrollmentWideRow> {
    let mut rows = Vec::<RegistrarCourseEnrollmentWideRow>::new();
    let mut remaining = credits_attempted;
    while remaining > 0 {
        let credits = sample_course_credits(rng, remaining);
        remaining -= credits;
        let subject = if rng.gen::<f64>() < 0.45 {
            student.likely_subject.clone()
        } else {
            SUBJECT_CATALOG[rng.gen_range(0..SUBJECT_CATALOG.len())].to_string()
        };
        let course_number = sample_course_number(rng, &student.student_level);
        let section_number = rng.gen_range(1..=4);
        let course_section_id =
            format!("{term_code}-{subject}{course_number}-{}", section_letter(section_number));
        let course_cip_code = cip_for_subject(&subject).to_string();
        let course_level_type = course_level_type(course_number).to_string();
        let delivery_method = sample_delivery_method(rng).to_string();
        let course_begin_date = term_start.to_string();
        let course_end_date = term_end.to_string();

        if !section_meta.contains_key(&course_section_id) {
            let instructor = pick_instructor(rng, instructors, &subject);
            section_meta.insert(
                course_section_id.clone(),
                SectionMeta {
                    course_section_id: course_section_id.clone(),
                    course_prefix: subject.clone(),
                    course_number: course_number.to_string(),
                    course_name: course_name(&subject, course_number),
                    course_cip_code: course_cip_code.clone(),
                    course_level_type: course_level_type.clone(),
                    delivery_method: delivery_method.clone(),
                    instructor_id: instructor.instructor_id.clone(),
                    meeting_pattern: sample_meeting_pattern(rng),
                    section_capacity: rng.gen_range(18..=45),
                    credits_attempted_course: credits,
                    course_begin_date: course_begin_date.clone(),
                    course_end_date: course_end_date.clone(),
                },
            );
            *instructor_load_credits
                .entry(instructor.instructor_id.clone())
                .or_insert(0) += credits;
        }
        let meta = section_meta.get(&course_section_id).expect("section metadata");
        let mut grade = grade_from_points(
            (term_gpa + subject_grade_shift(&subject) + rng.gen_range(-0.35..=0.35)).clamp(0.0, 4.0),
        )
        .to_string();
        let mut withdrawal_flag = false;
        if rng.gen::<f64>() < 0.04 {
            grade = "W".to_string();
            withdrawal_flag = true;
        }
        let credits_earned_course = if withdrawal_flag { 0 } else { credits };
        rows.push(RegistrarCourseEnrollmentWideRow {
            student_id: student.student_id.clone(),
            academic_year: academic_year.to_string(),
            term: term_label.to_string(),
            course_section_id: course_section_id.clone(),
            course_prefix: meta.course_prefix.clone(),
            course_number: meta.course_number.clone(),
            course_name: meta.course_name.clone(),
            course_cip_code: meta.course_cip_code.clone(),
            course_level_type: meta.course_level_type.clone(),
            delivery_method: meta.delivery_method.clone(),
            credits_attempted_course: credits,
            credits_earned_course,
            grade: grade.clone(),
            course_begin_date: meta.course_begin_date.clone(),
            course_end_date: meta.course_end_date.clone(),
            grading_basis: "graded".to_string(),
            letter_grade: if withdrawal_flag { None } else { Some(grade) },
            grade_points: if withdrawal_flag { None } else { Some(term_gpa) },
            repeat_flag: rng.gen::<f64>() < 0.04,
            withdrawal_flag,
            enrollment_status: if withdrawal_flag {
                "withdrawn".to_string()
            } else {
                "completed".to_string()
            },
            instructor_id: meta.instructor_id.clone(),
            meeting_pattern: meta.meeting_pattern.clone(),
            section_capacity: meta.section_capacity,
            section_enrollment: 0,
            grade_posted_date: Some((term_end + Duration::days(rng.gen_range(2..=10))).to_string()),
        });
    }
    rows
}

fn build_lms_rows<R: Rng>(
    rng: &mut R,
    student: &StudentInternal,
    academic_year: &str,
    term_label: &str,
    term_start: NaiveDate,
    term_end: NaiveDate,
    registrar_rows: &[RegistrarCourseEnrollmentWideRow],
    identifier_missing_rate: f64,
) -> (Vec<LmsActivityRawWideRow>, LmsActivityWideRow) {
    let term_days = (term_end - term_start).num_days().max(1) as u32;
    let distinct_course_count = registrar_rows.len() as u32;
    let login_count = (student.engagement_index * 60.0 + rng.gen_range(8.0..=30.0)) as u32;
    let active_days_count = ((term_days as f64) * (0.2 + 0.55 * student.engagement_index))
        .round()
        .clamp(1.0, term_days as f64) as u32;
    let page_views = ((login_count as f64) * rng.gen_range(5.0..=12.0)).round() as u32;
    let assignment_count_total = (distinct_course_count * rng.gen_range(4..=7)).max(1);
    let submissions_count = ((assignment_count_total as f64) * (0.58 + 0.35 * student.engagement_index))
        .round() as u32;
    let missing_submission_count = assignment_count_total.saturating_sub(submissions_count);
    let discussion_posts_count = ((student.engagement_index * 12.0) + rng.gen_range(0.0..=7.0)) as u32;
    let quiz_attempts_count = distinct_course_count * rng.gen_range(1..=4);
    let weekend_events_count = ((page_views as f64) * rng.gen_range(0.12..=0.24)).round() as u32;
    let late_night_events_count = ((page_views as f64) * rng.gen_range(0.08..=0.18)).round() as u32;
    let total_activity_time_seconds = page_views * rng.gen_range(28..=45) + login_count * 90;
    let first_activity_date = term_start + Duration::days(rng.gen_range(0..=8));
    let last_activity_date = term_end - Duration::days(rng.gen_range(0..=5));
    let content_interaction_count = ((page_views as f64) * rng.gen_range(0.35..=0.75)) as u32;
    let forum_post_length_avg = if discussion_posts_count == 0 {
        None
    } else {
        Some(round2(rng.gen_range(75.0..=260.0)))
    };
    let assignment_review_count = ((submissions_count as f64) * rng.gen_range(0.25..=0.65)) as u32;
    let regularity = round2((active_days_count as f64 / term_days as f64).clamp(0.0, 1.0).powf(0.70));

    let mut raw_rows = Vec::<LmsActivityRawWideRow>::new();
    for reg in registrar_rows {
        let section_id = reg.course_section_id.clone();
        let course_id = format!("course_{}", section_id.to_ascii_lowercase());
        let sis_course_id = Some(format!("{}{}", reg.course_prefix, reg.course_number));
        let event_date_1 = term_start + Duration::days(rng.gen_range(0..=term_days as i64));
        raw_rows.push(LmsActivityRawWideRow {
            lms_user_id: student.lms_user_id.clone(),
            sis_user_id: maybe_missing(Some(student.sis_user_id.clone()), identifier_missing_rate, rng),
            integration_id: maybe_missing(Some(student.integration_id.clone()), identifier_missing_rate, rng),
            course_id: course_id.clone(),
            sis_course_id: sis_course_id.clone(),
            section_id: section_id.clone(),
            sis_section_id: Some(section_id.clone()),
            event_timestamp: event_date_1
                .and_hms_opt(rng.gen_range(8..=23), rng.gen_range(0..=59), 0)
                .expect("valid time")
                .to_string(),
            event_type: "page_view".to_string(),
            enrollment_state: "active".to_string(),
            lms_enrollment_state_at_term_end: if reg.withdrawal_flag {
                "inactive".to_string()
            } else {
                "completed".to_string()
            },
            last_activity_at: Some(last_activity_date.to_string()),
            total_activity_time_seconds: (total_activity_time_seconds / distinct_course_count.max(1)).max(1),
            submission_late: None,
            submission_missing: None,
            submitted_at: None,
            grade: None,
            student_id: Some(student.student_id.clone()),
            academic_year: academic_year.to_string(),
            term: term_label.to_string(),
        });

        let event_date_2 = term_start + Duration::days(rng.gen_range(7..=term_days as i64));
        raw_rows.push(LmsActivityRawWideRow {
            lms_user_id: student.lms_user_id.clone(),
            sis_user_id: maybe_missing(Some(student.sis_user_id.clone()), identifier_missing_rate, rng),
            integration_id: maybe_missing(Some(student.integration_id.clone()), identifier_missing_rate, rng),
            course_id,
            sis_course_id,
            section_id: section_id.clone(),
            sis_section_id: Some(section_id),
            event_timestamp: event_date_2
                .and_hms_opt(rng.gen_range(9..=23), rng.gen_range(0..=59), 0)
                .expect("valid time")
                .to_string(),
            event_type: "submission".to_string(),
            enrollment_state: "active".to_string(),
            lms_enrollment_state_at_term_end: "completed".to_string(),
            last_activity_at: Some(last_activity_date.to_string()),
            total_activity_time_seconds: (total_activity_time_seconds / distinct_course_count.max(1)).max(1),
            submission_late: Some(rng.gen::<f64>() < 0.16),
            submission_missing: Some(rng.gen::<f64>() < 0.08),
            submitted_at: Some(event_date_2.to_string()),
            grade: reg.letter_grade.clone(),
            student_id: Some(student.student_id.clone()),
            academic_year: academic_year.to_string(),
            term: term_label.to_string(),
        });
    }

    (
        raw_rows,
        LmsActivityWideRow {
            student_id: student.student_id.clone(),
            sis_user_id: Some(student.sis_user_id.clone()),
            academic_year: academic_year.to_string(),
            term: term_label.to_string(),
            distinct_course_count,
            login_count,
            active_days_count,
            page_views,
            submissions_count,
            assignment_count_total,
            discussion_posts_count,
            quiz_attempts_count,
            weekend_events_count,
            late_night_events_count,
            total_activity_time_seconds,
            first_activity_date: first_activity_date.to_string(),
            last_activity_date: last_activity_date.to_string(),
            missing_submission_count,
            content_interaction_count,
            forum_post_length_avg,
            assignment_review_count,
            lms_activity_regularity_index: regularity,
            current_grade_visible_flag: rng.gen::<f64>() < 0.88,
        },
    )
}

#[allow(clippy::too_many_arguments)]
fn build_financial_aid_row<R: Rng>(
    rng: &mut R,
    student: &StudentInternal,
    term: &Term,
    academic_year: &str,
    term_label: &str,
    term_code: Option<String>,
    credits_attempted: u32,
    full_time: bool,
    aid_application_rate: f64,
) -> FinancialAidWideRow {
    let ay_start = parse_academic_year_start(academic_year);
    let need_index_regime = if ay_start >= 2024 { "SAI" } else { "EFC" }.to_string();
    let applied_aid_flag = rng.gen::<f64>() < aid_application_rate;
    let fafsa_filed_flag = applied_aid_flag && rng.gen::<f64>() < 0.68;

    let need_index_value = if !applied_aid_flag {
        None
    } else if need_index_regime == "SAI" {
        Some(((rng.gen::<f64>().powf(2.2) * 18_500.0) as i32) - 1500)
    } else {
        Some((rng.gen::<f64>().powf(2.1) * 18_000.0) as i32)
    };
    let has_need = student.financial_need_index > 0.58;
    let pell_amount = if fafsa_filed_flag
        && has_need
        && student.student_level == "Undergraduate"
        && rng.gen::<f64>() < 0.62
    {
        rng.gen_range(740..=7_395)
    } else {
        0
    };
    let federal_seog_amount = if fafsa_filed_flag
        && has_need
        && student.student_level == "Undergraduate"
        && rng.gen::<f64>() < 0.26
    {
        rng.gen_range(100..=1_200)
    } else {
        0
    };
    let state_grant_need_based = if fafsa_filed_flag && has_need && rng.gen::<f64>() < 0.46 {
        rng.gen_range(200..=2_800)
    } else {
        0
    };
    let state_grant_non_need_based = if applied_aid_flag && rng.gen::<f64>() < 0.12 {
        rng.gen_range(200..=1_500)
    } else {
        0
    };
    let institutional_grant_need_based = if has_need && rng.gen::<f64>() < 0.40 {
        rng.gen_range(500..=6_200)
    } else {
        0
    };
    let institutional_grant_merit = if applied_aid_flag && rng.gen::<f64>() < 0.27 {
        rng.gen_range(500..=8_500)
    } else {
        0
    };
    let institutional_grant_other = if applied_aid_flag && rng.gen::<f64>() < 0.14 {
        rng.gen_range(100..=2_400)
    } else {
        0
    };
    let federal_loan_amount = if applied_aid_flag && rng.gen::<f64>() < 0.43 {
        rng.gen_range(500..=8_500)
    } else {
        0
    };
    let parent_plus_amount = if student.student_level == "Undergraduate" && rng.gen::<f64>() < 0.09 {
        rng.gen_range(0..=6_500)
    } else {
        0
    };
    let private_loan_amount = if applied_aid_flag && rng.gen::<f64>() < 0.08 {
        rng.gen_range(0..=7_200)
    } else {
        0
    };
    let federal_work_study_amount =
        if applied_aid_flag && has_need && full_time && rng.gen::<f64>() < 0.30 {
            rng.gen_range(150..=2_000)
        } else {
            0
        };
    let state_work_study_amount = if applied_aid_flag && has_need && rng.gen::<f64>() < 0.08 {
        rng.gen_range(100..=1_200)
    } else {
        0
    };
    let institutional_work_study_amount = if applied_aid_flag && rng.gen::<f64>() < 0.09 {
        rng.gen_range(100..=1_500)
    } else {
        0
    };

    let intensity_factor = if full_time {
        1.0
    } else {
        (credits_attempted as f64 / full_time_threshold(&student.student_level) as f64).clamp(0.35, 0.95)
    };
    let term_share = match term.season {
        TermSeason::FA | TermSeason::SP => 0.4,
        TermSeason::SU => 0.2,
    };
    let annual_base = match student.residency_status.as_str() {
        "international" => 61_000.0,
        "out_of_state" => 58_000.0,
        _ => 56_000.0,
    };
    let cost_of_attendance = (annual_base * term_share * intensity_factor).round() as u32;
    let tuition_and_fees = (cost_of_attendance as f64 * 0.62).round() as u32;
    let housing_charge = if student.residential_flag && full_time {
        (cost_of_attendance as f64 * 0.26).round() as u32
    } else {
        0
    };
    let meal_plan_charge = if housing_charge > 0 {
        (cost_of_attendance as f64 * 0.08).round() as u32
    } else if rng.gen::<f64>() < 0.22 {
        (cost_of_attendance as f64 * 0.03).round() as u32
    } else {
        0
    };

    let total_grants = pell_amount
        + federal_seog_amount
        + state_grant_need_based
        + state_grant_non_need_based
        + institutional_grant_need_based
        + institutional_grant_merit
        + institutional_grant_other;
    let total_loans = federal_loan_amount + parent_plus_amount + private_loan_amount;
    let total_work_study =
        federal_work_study_amount + state_work_study_amount + institutional_work_study_amount;
    let total_aid = total_grants + total_loans + total_work_study;
    let gap = cost_of_attendance as i32 - total_aid as i32;
    let unmet_need = gap.max(0) as u32;
    let need_based_applicant_type = if total_aid == 0 {
        "no_aid"
    } else if pell_amount > 0 {
        "pell_recipient"
    } else if institutional_grant_need_based > 0 || state_grant_need_based > 0 {
        "need_based_non_pell"
    } else {
        "non_need_based_only"
    }
    .to_string();

    FinancialAidWideRow {
        student_id: student.student_id.clone(),
        erp_person_id: Some(student.erp_person_id.clone()),
        academic_year: academic_year.to_string(),
        term: Some(term_label.to_string()),
        term_code,
        fafsa_filed_flag,
        applied_aid_flag,
        need_index_regime,
        need_index_value,
        pell_amount,
        federal_seog_amount,
        state_grant_need_based,
        state_grant_non_need_based,
        institutional_grant_need_based,
        institutional_grant_merit,
        institutional_grant_other,
        federal_loan_amount,
        parent_plus_amount,
        private_loan_amount,
        federal_work_study_amount,
        state_work_study_amount,
        institutional_work_study_amount,
        cost_of_attendance,
        tuition_and_fees,
        housing_charge,
        meal_plan_charge,
        total_grants,
        total_loans,
        total_work_study,
        total_aid,
        unmet_need,
        need_based_applicant_type,
        balance_due: gap,
        refund_amount: if gap < 0 { (-gap) as u32 } else { 0 },
        aid_package_status: if !applied_aid_flag {
            "not_applied"
        } else if total_aid == 0 {
            "offered"
        } else if gap < 0 {
            "disbursed"
        } else {
            ["offered", "accepted", "revised", "disbursed"][rng.gen_range(0..4)]
        }
        .to_string(),
    }
}

fn build_hold_row<R: Rng>(
    rng: &mut R,
    student_id: &str,
    term_label: &str,
    term_code: Option<String>,
    term_start: NaiveDate,
    term_end: NaiveDate,
    hold_clearance_lag_days: u32,
    used_hold_ids: &mut HashSet<String>,
) -> LocalPostsecondaryHoldWide {
    let hold_type = sample_hold_type(rng).to_string();
    let profile = hold_profile(&hold_type);
    let term_days = (term_end - term_start).num_days().max(1) as f64;
    let placed_date = term_start + Duration::days((rng.gen::<f64>().powf(1.7) * term_days).round() as i64);
    let clear_probability = match profile.severity {
        "info" => 0.90,
        "soft_block" => 0.65,
        _ => 0.45,
    };
    let cleared_date = if rng.gen::<f64>() < clear_probability {
        let max_days = (hold_clearance_lag_days.saturating_mul(2)).max(3);
        Some((placed_date + Duration::days(rng.gen_range(1..=max_days) as i64)).to_string())
    } else {
        None
    };
    LocalPostsecondaryHoldWide {
        hold_id: unique_hold_id(rng, used_hold_ids),
        student_id: student_id.to_string(),
        term: term_label.to_string(),
        term_code,
        hold_type,
        source_office: profile.source_office.to_string(),
        severity: profile.severity.to_string(),
        active_flag: cleared_date.is_none(),
        blocks_registration: profile.blocks_registration,
        blocks_transcript: profile.blocks_transcript,
        hold_reason_code: profile.reason_code.to_string(),
        note_visibility: sample_note_visibility(rng).to_string(),
        resolution_channel: profile.resolution_channel.to_string(),
        placed_date: placed_date.to_string(),
        cleared_date,
    }
}

fn determine_enrollment_status<R: Rng>(
    rng: &mut R,
    student: &mut StudentInternal,
    args: &Args,
    term_idx: usize,
) -> String {
    if term_idx == student.initial_enrollment_term_idx {
        return if rng.gen::<f64>() < 0.96 {
            "enrolled".to_string()
        } else {
            "withdrawn".to_string()
        };
    }
    match student.lifecycle {
        StudentLifecycle::TransferredOut => "transferred_out".to_string(),
        StudentLifecycle::Withdrawn => {
            if rng.gen::<f64>() < 0.10 {
                student.lifecycle = StudentLifecycle::Active;
                "enrolled".to_string()
            } else {
                "withdrawn".to_string()
            }
        }
        StudentLifecycle::StoppedOut => {
            if rng.gen::<f64>() < args.reenroll_after_stopout_rate {
                student.lifecycle = StudentLifecycle::Active;
                student.stopout_terms_remaining = 0;
                "enrolled".to_string()
            } else {
                if student.stopout_terms_remaining > 0 {
                    student.stopout_terms_remaining -= 1;
                }
                "stopout".to_string()
            }
        }
        StudentLifecycle::Active => {
            let draw = rng.gen::<f64>();
            if draw < args.transfer_out_rate {
                student.lifecycle = StudentLifecycle::TransferredOut;
                "transferred_out".to_string()
            } else if draw < args.transfer_out_rate + args.stopout_rate {
                student.lifecycle = StudentLifecycle::StoppedOut;
                student.stopout_terms_remaining = rng.gen_range(1..=3);
                "stopout".to_string()
            } else if draw < args.transfer_out_rate + args.stopout_rate + args.withdrawal_rate {
                if rng.gen::<f64>() < 0.38 {
                    student.lifecycle = StudentLifecycle::Withdrawn;
                }
                "withdrawn".to_string()
            } else {
                "enrolled".to_string()
            }
        }
    }
}

fn class_level_for_student(student: &StudentInternal) -> Option<String> {
    if student.student_level == "Undergraduate" {
        Some(class_level_from_credits(student.cumulative_credits_earned).as_str().to_string())
    } else if student.student_level == "Nondegree" {
        Some("NonDegree".to_string())
    } else if student.student_level == "Graduate" {
        Some("Graduate".to_string())
    } else {
        Some("Professional".to_string())
    }
}

fn effective_missing_rate(
    base: f64,
    pattern: MissingnessPattern,
    term_idx: usize,
    student: &StudentInternal,
    lms_stream: bool,
) -> f64 {
    let adjusted = match pattern {
        MissingnessPattern::Mcar => base,
        MissingnessPattern::MarByTerm => base + term_idx as f64 * 0.015,
        MissingnessPattern::MarByStudentGroup => {
            let mut p = base;
            if student.first_gen_flag == Some(true) {
                p += 0.05;
            }
            if student.financial_need_index > 0.75 {
                p += 0.05;
            }
            if !lms_stream && student.student_level != "Undergraduate" {
                p += 0.02;
            }
            p
        }
        MissingnessPattern::SystemOutageBurst => {
            if term_idx % 5 == 2 {
                base + 0.25
            } else if term_idx % 5 == 3 {
                base + 0.12
            } else {
                base
            }
        }
    };
    adjusted.clamp(0.0, 0.95)
}

fn sample_age_at_entry<R: Rng>(rng: &mut R) -> u8 {
    if rng.gen::<f64>() < 0.12 {
        rng.gen_range(25..=45)
    } else {
        rng.gen_range(17..=24)
    }
}
fn sample_gender<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.57 {
        "Female"
    } else if x < 0.99 {
        "Male"
    } else if x < 0.995 {
        "Another_gender"
    } else {
        "Unknown"
    }
}
fn sample_race_ethnicity<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.54 {
        "White"
    } else if x < 0.74 {
        "Hispanic_or_Latino"
    } else if x < 0.87 {
        "Black_or_African_American"
    } else if x < 0.94 {
        "Asian"
    } else if x < 0.98 {
        "Two_or_more_races"
    } else if x < 0.99 {
        "Nonresident_alien"
    } else {
        "Race_ethnicity_unknown"
    }
}
fn sample_country_of_origin<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.88 {
        "US"
    } else {
        ["CN", "IN", "KR", "CA", "MX", "BR", "NG", "GB", "JP", "AE"][rng.gen_range(0..10)]
    }
}
fn sample_citizenship_status<R: Rng>(rng: &mut R, country: &str) -> &'static str {
    if country == "US" {
        let x = rng.gen::<f64>();
        if x < 0.84 {
            "us_citizen"
        } else if x < 0.97 {
            "permanent_resident"
        } else {
            "other_or_unknown"
        }
    } else if rng.gen::<f64>() < 0.86 {
        "nonresident_visa"
    } else {
        "other_or_unknown"
    }
}
fn sample_state_of_residence<R: Rng>(rng: &mut R) -> Option<String> {
    if rng.gen::<f64>() < 0.03 {
        return None;
    }
    Some(
        ["MA", "NY", "CA", "NJ", "CT", "RI", "NH", "VT", "ME", "PA", "TX", "FL", "IL", "WA", "VA"]
            [rng.gen_range(0..15)]
            .to_string(),
    )
}
fn sample_residency_status<R: Rng>(rng: &mut R, country: &str) -> &'static str {
    if country != "US" {
        "international"
    } else if rng.gen::<f64>() < 0.58 {
        "in_state"
    } else {
        "out_of_state"
    }
}
fn sample_student_level<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.75 {
        "Undergraduate"
    } else if x < 0.95 {
        "Graduate"
    } else if x < 0.98 {
        "Professional"
    } else {
        "Nondegree"
    }
}
fn academic_career_for_level(level: &str) -> &'static str {
    match level {
        "Graduate" => "GRAD",
        "Professional" => "PROF",
        "Nondegree" => "NOND",
        _ => "UGRD",
    }
}
fn sample_admit_type<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.72 {
        "first_time"
    } else if x < 0.90 {
        "transfer"
    } else if x < 0.97 {
        "readmit"
    } else {
        "dual_enrollment"
    }
}
fn sample_second_major<R: Rng>(
    rng: &mut R,
    primary_label: &str,
    major_dist: &WeightedIndex<u32>,
    major_catalog: &[crate::catalogs::MajorCatalog],
) -> Option<crate::catalogs::MajorCatalog> {
    if rng.gen::<f64>() >= 0.14 {
        return None;
    }
    for _ in 0..12 {
        let cand = major_catalog[major_dist.sample(rng)];
        if cand.label != primary_label {
            return Some(cand);
        }
    }
    None
}
fn sample_base_gpa<R: Rng>(rng: &mut R) -> f64 {
    round2(if rng.gen::<f64>() < 0.2 {
        rng.gen_range(2.0..=2.7)
    } else if rng.gen::<f64>() < 0.7 {
        rng.gen_range(2.7..=3.4)
    } else {
        rng.gen_range(3.4..=4.0)
    })
}
fn ternary_flag<R: Rng>(rng: &mut R, true_prob: f64, unknown_prob: f64) -> Option<bool> {
    if rng.gen::<f64>() < unknown_prob {
        None
    } else {
        Some(rng.gen::<f64>() < true_prob)
    }
}
fn sample_term_credits<R: Rng>(rng: &mut R, student_level: &str) -> u32 {
    match student_level {
        "Graduate" => {
            if rng.gen::<f64>() < 0.65 {
                rng.gen_range(9..=12)
            } else {
                rng.gen_range(3..=8)
            }
        }
        "Professional" => {
            if rng.gen::<f64>() < 0.78 {
                rng.gen_range(12..=16)
            } else {
                rng.gen_range(6..=11)
            }
        }
        "Nondegree" => rng.gen_range(1..=8),
        _ => {
            if rng.gen::<f64>() < 0.72 {
                rng.gen_range(12..=17)
            } else {
                rng.gen_range(6..=11)
            }
        }
    }
}
fn sample_credits_earned<R: Rng>(rng: &mut R, attempted: u32, gpa: f64) -> u32 {
    if attempted == 0 {
        return 0;
    }
    let loss = if gpa < 1.9 {
        rng.gen_range(2..=attempted.min(6))
    } else if gpa < 2.4 {
        rng.gen_range(1..=attempted.min(4))
    } else if rng.gen::<f64>() < 0.08 {
        1
    } else {
        0
    };
    attempted.saturating_sub(loss)
}
fn full_time_threshold(student_level: &str) -> u32 {
    match student_level {
        "Graduate" => 9,
        "Nondegree" => 6,
        _ => 12,
    }
}
fn sample_course_credits<R: Rng>(rng: &mut R, remaining: u32) -> u32 {
    if remaining <= 2 {
        remaining
    } else if remaining >= 4 && rng.gen::<f64>() < 0.28 {
        4
    } else {
        3.min(remaining)
    }
}
fn sample_course_number<R: Rng>(rng: &mut R, student_level: &str) -> u32 {
    match student_level {
        "Graduate" | "Professional" => rng.gen_range(500..=799),
        "Nondegree" => rng.gen_range(50..=299),
        _ => {
            if rng.gen::<f64>() < 0.65 {
                rng.gen_range(100..=299)
            } else {
                rng.gen_range(300..=499)
            }
        }
    }
}
fn course_level_type(catalog_number: u32) -> &'static str {
    if catalog_number < 200 {
        "lower_division"
    } else if catalog_number < 500 {
        "upper_division"
    } else {
        "graduate"
    }
}
fn sample_delivery_method<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.55 {
        "face_to_face"
    } else if x < 0.90 {
        "online"
    } else {
        "hybrid"
    }
}
fn sample_meeting_pattern<R: Rng>(rng: &mut R) -> Option<String> {
    let patterns = ["MWF 09:00-09:50", "MWF 10:00-10:50", "TR 09:30-10:50", "TR 11:00-12:20", "TR 14:00-15:20", "R 18:00-20:30"];
    if rng.gen::<f64>() < 0.14 {
        None
    } else {
        Some(patterns[rng.gen_range(0..patterns.len())].to_string())
    }
}
fn sample_faculty_rank<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.18 {
        "Professor"
    } else if x < 0.36 {
        "Associate_Professor"
    } else if x < 0.54 {
        "Assistant_Professor"
    } else if x < 0.66 {
        "Instructor"
    } else if x < 0.84 {
        "Lecturer"
    } else {
        "Adjunct"
    }
}
fn sample_tenure_status<R: Rng>(rng: &mut R, rank: &str) -> &'static str {
    match rank {
        "Professor" => "Tenured",
        "Associate_Professor" => if rng.gen::<f64>() < 0.82 { "Tenured" } else { "On_Tenure_Track" },
        "Assistant_Professor" => if rng.gen::<f64>() < 0.70 { "On_Tenure_Track" } else { "Not_on_Tenure_Track" },
        _ => "Not_on_Tenure_Track",
    }
}
fn pick_instructor<'a, R: Rng>(rng: &mut R, instructors: &'a [InstructorProfile], subject: &str) -> &'a InstructorProfile {
    let preferred = department_for_subject(subject);
    if rng.gen::<f64>() < 0.65 {
        let candidates: Vec<&InstructorProfile> = instructors.iter().filter(|i| i.home_department == preferred).collect();
        if !candidates.is_empty() {
            return candidates[rng.gen_range(0..candidates.len())];
        }
    }
    &instructors[rng.gen_range(0..instructors.len())]
}
fn department_for_subject(subject: &str) -> &'static str {
    match subject {
        "COSI" => "Computer Science",
        "BIOL" => "Biology",
        "ECON" => "Economics",
        "PSYC" => "Psychology",
        "NEUR" => "Neuroscience",
        "CHEM" => "Chemistry",
        "MATH" => "Mathematics",
        "POL" => "Politics",
        "HIST" => "History",
        "PHIL" => "Philosophy",
        "BUS" => "Business",
        "SOC" => "Sociology",
        "ENVS" => "Environmental Studies",
        _ => "Computer Science",
    }
}
fn course_name(subject: &str, number: u32) -> String {
    let stem = match subject {
        "COSI" => "Computer Science",
        "BIOL" => "Biology",
        "ECON" => "Economics",
        "PSYC" => "Psychology",
        "NEUR" => "Neuroscience",
        "CHEM" => "Chemistry",
        "MATH" => "Mathematics",
        "HSSP" => "Health Policy",
        "AMST" => "American Studies",
        "ENVS" => "Environmental Studies",
        "POL" => "Politics",
        "ANTH" => "Anthropology",
        "ENG" => "English",
        "HIST" => "History",
        "PHIL" => "Philosophy",
        "BUS" => "Business",
        "PHYS" => "Physics",
        "SOC" => "Sociology",
        "IGS" => "Global Studies",
        "ARTS" => "Studio Art",
        "MUS" => "Music",
        "EAS" => "East Asian Studies",
        _ => "General Studies",
    };
    format!("{stem} {number}")
}
fn subject_grade_shift(subject: &str) -> f64 {
    match subject {
        "COSI" | "BIOL" | "CHEM" | "MATH" | "NEUR" | "PHYS" => -0.14,
        "ECON" | "BUS" => -0.08,
        "ENG" | "HIST" | "PHIL" | "AMST" | "ARTS" => 0.08,
        _ => 0.0,
    }
}
fn grade_from_points(gp: f64) -> &'static str {
    if gp >= 3.85 {
        "A"
    } else if gp >= 3.50 {
        "A-"
    } else if gp >= 3.20 {
        "B+"
    } else if gp >= 2.85 {
        "B"
    } else if gp >= 2.50 {
        "B-"
    } else if gp >= 2.15 {
        "C+"
    } else if gp >= 1.85 {
        "C"
    } else if gp >= 1.50 {
        "C-"
    } else if gp >= 1.00 {
        "D"
    } else {
        "F"
    }
}
fn sample_hold_type<R: Rng>(rng: &mut R) -> &'static str {
    let values = ["advising", "financial", "immunization", "conduct", "housing", "registrar", "title_ix", "documentation"];
    let weights = [26, 23, 11, 9, 10, 11, 5, 5];
    let dist = WeightedIndex::new(weights).expect("hold type weights");
    values[dist.sample(rng)]
}
struct HoldProfile {
    source_office: &'static str,
    severity: &'static str,
    blocks_registration: bool,
    blocks_transcript: bool,
    reason_code: &'static str,
    resolution_channel: &'static str,
}
fn hold_profile(hold_type: &str) -> HoldProfile {
    match hold_type {
        "advising" => HoldProfile { source_office: "advising", severity: "soft_block", blocks_registration: true, blocks_transcript: false, reason_code: "ADV01", resolution_channel: "advisor" },
        "financial" => HoldProfile { source_office: "bursar", severity: "hard_block", blocks_registration: true, blocks_transcript: true, reason_code: "FIN10", resolution_channel: "bursar_payment" },
        "immunization" => HoldProfile { source_office: "health", severity: "soft_block", blocks_registration: true, blocks_transcript: false, reason_code: "IMM07", resolution_channel: "online_form" },
        "conduct" => HoldProfile { source_office: "student_conduct", severity: "hard_block", blocks_registration: true, blocks_transcript: false, reason_code: "CON02", resolution_channel: "manual_review" },
        "housing" => HoldProfile { source_office: "housing", severity: "soft_block", blocks_registration: false, blocks_transcript: false, reason_code: "HOU04", resolution_channel: "online_form" },
        "registrar" => HoldProfile { source_office: "registrar", severity: "hard_block", blocks_registration: true, blocks_transcript: true, reason_code: "REG03", resolution_channel: "manual_review" },
        "title_ix" => HoldProfile { source_office: "title_ix", severity: "hard_block", blocks_registration: true, blocks_transcript: false, reason_code: "TIX01", resolution_channel: "manual_review" },
        _ => HoldProfile { source_office: "registrar", severity: "info", blocks_registration: false, blocks_transcript: false, reason_code: "DOC01", resolution_channel: "online_form" },
    }
}
fn sample_note_visibility<R: Rng>(rng: &mut R) -> &'static str {
    let x = rng.gen::<f64>();
    if x < 0.64 {
        "internal"
    } else if x < 0.90 {
        "student_visible"
    } else {
        "restricted"
    }
}
fn compute_hold_probability(base: f64, financial_need_index: f64, enrollment_status: &str, cumulative_gpa: Option<f64>) -> f64 {
    let mut p = base;
    if enrollment_status != "enrolled" {
        p += 0.05;
    }
    if financial_need_index > 0.80 {
        p += 0.04;
    }
    if cumulative_gpa.unwrap_or(3.0) < 2.1 {
        p += 0.03;
    }
    p.clamp(0.01, 0.95)
}
fn term_window(term: &Term) -> (NaiveDate, NaiveDate) {
    match term.season {
        TermSeason::SP => (NaiveDate::from_ymd_opt(term.year, 1, 15).expect("spring start"), NaiveDate::from_ymd_opt(term.year, 5, 15).expect("spring end")),
        TermSeason::SU => (NaiveDate::from_ymd_opt(term.year, 5, 20).expect("summer start"), NaiveDate::from_ymd_opt(term.year, 8, 10).expect("summer end")),
        TermSeason::FA => (NaiveDate::from_ymd_opt(term.year, 8, 25).expect("fall start"), NaiveDate::from_ymd_opt(term.year, 12, 20).expect("fall end")),
    }
}
fn term_code_for_wide(style: TermCodeStyle, term_code: &str) -> Option<String> {
    match style {
        TermCodeStyle::Split => None,
        TermCodeStyle::Packed | TermCodeStyle::Both => Some(term_code.to_string()),
    }
}
fn parse_academic_year_start(academic_year: &str) -> i32 {
    academic_year.split('-').next().and_then(|s| s.parse::<i32>().ok()).unwrap_or(2024)
}
fn schema_label(v: SchemaVersion) -> &'static str {
    match v {
        SchemaVersion::Slim => "slim",
        SchemaVersion::Wide => "wide",
        SchemaVersion::Both => "both",
    }
}
fn term_code_style_label(v: TermCodeStyle) -> &'static str {
    match v {
        TermCodeStyle::Packed => "packed",
        TermCodeStyle::Split => "split",
        TermCodeStyle::Both => "both",
    }
}
fn missingness_label(v: MissingnessPattern) -> &'static str {
    match v {
        MissingnessPattern::Mcar => "mcar",
        MissingnessPattern::MarByTerm => "mar_by_term",
        MissingnessPattern::MarByStudentGroup => "mar_by_student_group",
        MissingnessPattern::SystemOutageBurst => "system_outage_burst",
    }
}
fn unique_erp_id<R: Rng>(rng: &mut R, used: &mut HashSet<String>) -> String {
    loop {
        let id = format!("ERP{:08}", rng.gen_range(0..100_000_000u32));
        if used.insert(id.clone()) {
            return id;
        }
    }
}
fn unique_hold_id<R: Rng>(rng: &mut R, used: &mut HashSet<String>) -> String {
    loop {
        let id = uuid_from_rng(rng).to_string();
        if used.insert(id.clone()) {
            return id;
        }
    }
}
fn uuid_from_rng<R: Rng>(rng: &mut R) -> Uuid {
    let mut bytes = [0u8; 16];
    rng.fill(&mut bytes);
    bytes[6] = (bytes[6] & 0x0F) | 0x40;
    bytes[8] = (bytes[8] & 0x3F) | 0x80;
    Uuid::from_bytes(bytes)
}
fn maybe_missing<T, R: Rng>(value: Option<T>, p_missing: f64, rng: &mut R) -> Option<T> {
    if rng.gen::<f64>() < p_missing { None } else { value }
}
fn section_letter(n: u32) -> char {
    const LETTERS: &[char] = &['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
    LETTERS[(n as usize - 1) % LETTERS.len()]
}
fn to_slim_demographics(r: &StudentDemographicsWideRow) -> StudentDemographicsRow {
    StudentDemographicsRow { student_id: r.student_id.clone(), birth_year: r.birth_year, gender: r.gender.clone(), race_ethnicity: r.race_ethnicity.clone(), first_gen_flag: r.first_gen_flag, veteran_status: r.veteran_status, disability_status: r.disability_status, age_at_entry: r.age_at_entry, country_of_origin: r.country_of_origin.clone(), citizenship_status: r.citizenship_status.clone(), initial_enrollment_term: r.initial_enrollment_term.clone(), cohort_year: r.cohort_year.clone(), admit_type: r.admit_type.clone(), hs_unweighted_gpa: r.hs_unweighted_gpa }
}
fn to_slim_crosswalk(r: &IdentityCrosswalkWideRow) -> IdentityCrosswalkRow {
    IdentityCrosswalkRow { student_id: r.student_id.clone(), lms_user_id: r.lms_user_id.clone(), sis_user_id: r.sis_user_id.clone(), integration_id: r.integration_id.clone(), erp_person_id: r.erp_person_id.clone(), effective_start_date: r.effective_start_date.clone(), effective_end_date: r.effective_end_date.clone(), active_flag: r.active_flag, source_system: r.source_system.clone(), match_rule: r.match_rule.clone(), match_confidence: r.match_confidence }
}
fn to_slim_sis(r: &SisEnrollmentWideRow) -> SisEnrollmentRow {
    SisEnrollmentRow { student_id: r.student_id.clone(), academic_year: r.academic_year.clone(), term: r.term.clone(), student_level: r.student_level.clone(), major_cip_code: r.major_cip_code.clone(), major_label: r.major_label.clone(), second_major_cip_code: r.second_major_cip_code.clone(), enrollment_type: r.enrollment_type.clone(), first_time_flag: r.first_time_flag, transfer_credits_accepted: r.transfer_credits_accepted, credits_attempted: r.credits_attempted, credits_earned: r.credits_earned, term_gpa: r.term_gpa, cumulative_gpa: r.cumulative_gpa, credits_earned_cumulative: r.credits_earned_cumulative, enrollment_status: r.enrollment_status.clone(), full_time_flag: r.full_time_flag, term_start_date: r.term_start_date.clone(), term_end_date: r.term_end_date.clone() }
}
fn to_slim_registrar(r: &RegistrarCourseEnrollmentWideRow) -> RegistrarCourseEnrollmentRow {
    RegistrarCourseEnrollmentRow { student_id: r.student_id.clone(), academic_year: r.academic_year.clone(), term: r.term.clone(), course_section_id: r.course_section_id.clone(), course_prefix: r.course_prefix.clone(), course_number: r.course_number.clone(), course_name: r.course_name.clone(), course_cip_code: r.course_cip_code.clone(), course_level_type: r.course_level_type.clone(), delivery_method: r.delivery_method.clone(), credits_attempted_course: r.credits_attempted_course, credits_earned_course: r.credits_earned_course, grade: r.grade.clone(), course_begin_date: r.course_begin_date.clone(), course_end_date: r.course_end_date.clone() }
}
fn to_slim_faculty(r: &FacultyCourseWideRow) -> FacultyCourseRow {
    FacultyCourseRow { instructor_id: r.instructor_id.clone(), academic_year: r.academic_year.clone(), term: r.term.clone(), course_section_id: r.course_section_id.clone(), faculty_rank: r.faculty_rank.clone(), tenure_status: r.tenure_status.clone(), employment_status: r.employment_status.clone() }
}
fn to_slim_lms_raw(r: &LmsActivityRawWideRow) -> LmsActivityRawRow {
    LmsActivityRawRow { lms_user_id: r.lms_user_id.clone(), sis_user_id: r.sis_user_id.clone(), integration_id: r.integration_id.clone(), course_id: r.course_id.clone(), sis_course_id: r.sis_course_id.clone(), section_id: r.section_id.clone(), sis_section_id: r.sis_section_id.clone(), event_timestamp: r.event_timestamp.clone(), event_type: r.event_type.clone(), enrollment_state: r.enrollment_state.clone(), lms_enrollment_state_at_term_end: r.lms_enrollment_state_at_term_end.clone(), last_activity_at: r.last_activity_at.clone(), total_activity_time_seconds: r.total_activity_time_seconds, submission_late: r.submission_late, submission_missing: r.submission_missing, submitted_at: r.submitted_at.clone(), grade: r.grade.clone() }
}
fn to_slim_lms(r: &LmsActivityWideRow) -> LmsActivityRow {
    LmsActivityRow { student_id: r.student_id.clone(), academic_year: r.academic_year.clone(), term: r.term.clone(), distinct_course_count: r.distinct_course_count, login_count: r.login_count, active_days_count: r.active_days_count, page_views: r.page_views, submissions_count: r.submissions_count, assignment_count_total: r.assignment_count_total, discussion_posts_count: r.discussion_posts_count, quiz_attempts_count: r.quiz_attempts_count, weekend_events_count: r.weekend_events_count, late_night_events_count: r.late_night_events_count, total_activity_time_seconds: r.total_activity_time_seconds, first_activity_date: r.first_activity_date.clone(), last_activity_date: r.last_activity_date.clone(), missing_submission_count: r.missing_submission_count }
}
fn to_slim_financial(r: &FinancialAidWideRow) -> FinancialAidRow {
    FinancialAidRow { student_id: r.student_id.clone(), academic_year: r.academic_year.clone(), fafsa_filed_flag: r.fafsa_filed_flag, applied_aid_flag: r.applied_aid_flag, need_index_regime: r.need_index_regime.clone(), need_index_value: r.need_index_value, pell_amount: r.pell_amount, federal_seog_amount: r.federal_seog_amount, state_grant_need_based: r.state_grant_need_based, state_grant_non_need_based: r.state_grant_non_need_based, institutional_grant_need_based: r.institutional_grant_need_based, institutional_grant_merit: r.institutional_grant_merit, institutional_grant_other: r.institutional_grant_other, federal_loan_amount: r.federal_loan_amount, parent_plus_amount: r.parent_plus_amount, private_loan_amount: r.private_loan_amount, federal_work_study_amount: r.federal_work_study_amount, state_work_study_amount: r.state_work_study_amount, institutional_work_study_amount: r.institutional_work_study_amount, cost_of_attendance: r.cost_of_attendance, tuition_and_fees: r.tuition_and_fees, housing_charge: r.housing_charge, meal_plan_charge: r.meal_plan_charge, total_grants: r.total_grants, total_loans: r.total_loans, total_work_study: r.total_work_study, total_aid: r.total_aid, unmet_need: r.unmet_need, need_based_applicant_type: r.need_based_applicant_type.clone() }
}
fn to_slim_hold(r: &LocalPostsecondaryHoldWide) -> LocalPostsecondaryHold {
    LocalPostsecondaryHold { hold_id: r.hold_id.clone(), student_id: r.student_id.clone(), term: r.term.clone(), hold_type: r.hold_type.clone(), source_office: r.source_office.clone(), severity: r.severity.clone(), active_flag: r.active_flag, blocks_registration: r.blocks_registration, blocks_transcript: r.blocks_transcript, hold_reason_code: r.hold_reason_code.clone(), note_visibility: r.note_visibility.clone(), resolution_channel: r.resolution_channel.clone(), placed_date: r.placed_date.clone(), cleared_date: r.cleared_date.clone() }
}
fn round2(v: f64) -> f64 { (v * 100.0).round() / 100.0 }
fn gpa_or_none(qp: f64, credits: u32) -> Option<f64> { if credits == 0 { None } else { Some(round2(qp / credits as f64)) } }
fn slugify_username(full_name: &str) -> String { full_name.to_ascii_lowercase().chars().filter(|c| c.is_ascii_alphanumeric()).collect() }
fn unique_username(base: &str, counts: &mut HashMap<String, u32>) -> String {
    let n = counts.entry(base.to_string()).or_insert(0);
    *n += 1;
    if *n == 1 { base.to_string() } else { format!("{base}{n}") }
}
