use crate::args::Args;
use crate::catalogs::{build_major_sampler, BRANDEIS_SUBJECTS};
use crate::io_utils::{ensure_dir, write_csv, write_json};
use crate::models::{
    class_level_from_credits, AdvisingHold, ClassLevel, CrosswalkRow, FinancialAidRow,
    LmsActivityRow, RegistrarCourseEnrollmentRow, SisEnrollmentRow, StudentInternal,
    StudentMasterOut,
};
use crate::term::{Term, TermSeason};
use anyhow::Result;
use chrono::{Datelike, NaiveDate};
use rand::distributions::{Distribution, WeightedIndex};
use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;
use rand_distr::Normal;
use std::collections::HashMap;
use uuid::Uuid;

pub fn generate(args: &Args, terms: &[Term]) -> Result<()> {
    let mut rng = ChaCha8Rng::seed_from_u64(args.seed);
    ensure_dir(&args.out_dir)?;
    ensure_dir(&args.out_dir.join("terms"))?;

    let (major_values, major_dist) = build_major_sampler();
    let mut email_counts: HashMap<String, u32> = HashMap::new();
    let mut students: Vec<StudentInternal> = Vec::with_capacity(args.students);

    let first_names = [
        "Alex", "Jordan", "Taylor", "Riley", "Casey", "Morgan", "Avery", "Jamie", "Quinn",
        "Cameron", "Devin", "Parker", "Reese", "Skyler", "Rowan", "Sydney", "Drew", "Hayden",
        "Emerson", "Kendall",
    ];
    let last_names = [
        "Kim", "Patel", "Garcia", "Nguyen", "Johnson", "Smith", "Brown", "Davis", "Miller",
        "Wilson", "Martinez", "Anderson", "Thomas", "Jackson", "White", "Harris", "Clark", "Lewis",
        "Walker", "Young",
    ];

    for i in 0..args.students {
        let student_id = format!("S{:0>6}", i + 1);
        let full_name = format!(
            "{} {}",
            first_names[rng.gen_range(0..first_names.len())],
            last_names[rng.gen_range(0..last_names.len())]
        );
        let base = slugify_username(&full_name);
        let username = unique_username(&base, &mut email_counts);
        let email = format!("{}@brandeis.edu", username);

        let major = major_values[major_dist.sample(&mut rng)].to_string();
        let init_year = sample_initial_year(&mut rng);
        let credits_start = init_year
            .map(|y| sample_credits_bucket_like_js(&mut rng, y))
            .unwrap_or(0);
        let base_gpa = sample_gpa_like_js(&mut rng);

        let admit_term_idx = if rng.gen::<f64>() < 0.72 {
            0
        } else {
            rng.gen_range(0..args.terms)
        };

        students.push(StudentInternal {
            student_id: student_id.clone(),
            full_name,
            email,
            moodle_user_key: format!("mdl_{}", student_id.to_ascii_lowercase()),
            workday_person_id: format!("WD{:08}", rng.gen_range(0..100_000_000u32)),
            birth_date: random_birthdate(&mut rng),
            admit_term_idx,
            major_current: major,
            base_gpa,
            cumulative_credits: credits_start,
            cumulative_quality_points: base_gpa * credits_start as f64,
            stopped_out: false,
        });
    }

    if args.crosswalk_mismatch_rate > 0.0 && students.len() >= 2 {
        let swaps = ((students.len() as f64) * args.crosswalk_mismatch_rate).round() as usize;
        for _ in 0..swaps {
            let a = rng.gen_range(0..students.len());
            let mut b = rng.gen_range(0..students.len());
            while b == a {
                b = rng.gen_range(0..students.len());
            }
            let tmp = students[a].moodle_user_key.clone();
            students[a].moodle_user_key = students[b].moodle_user_key.clone();
            students[b].moodle_user_key = tmp;
        }
    }

    let students_master: Vec<StudentMasterOut> = students
        .iter()
        .map(|s| StudentMasterOut {
            student_id: s.student_id.clone(),
            full_name: s.full_name.clone(),
            email: s.email.clone(),
            birth_date: s.birth_date.to_string(),
            admit_term: terms[s.admit_term_idx].code(),
            initial_major: s.major_current.clone(),
            initial_year: Some(
                class_level_from_credits(s.cumulative_credits)
                    .as_str()
                    .to_string(),
            ),
            primary_system: "Workday Student".to_string(),
        })
        .collect();
    write_json(
        &args.out_dir.join("students_master.json"),
        &students_master,
        args.pretty_json,
    )?;

    let crosswalk: Vec<CrosswalkRow> = students
        .iter()
        .map(|s| CrosswalkRow {
            student_id: s.student_id.clone(),
            moodle_user_key: s.moodle_user_key.clone(),
            workday_person_id: s.workday_person_id.clone(),
        })
        .collect();
    write_csv(&args.out_dir.join("identity_crosswalk.csv"), &crosswalk)?;

    let gpa_noise = Normal::new(0.0, 0.35).unwrap();

    for (term_idx, term) in terms.iter().enumerate() {
        let term_code = term.code();
        let term_dir = args.out_dir.join("terms").join(&term_code);
        ensure_dir(&term_dir)?;

        let mut sis_rows = Vec::new();
        let mut reg_rows = Vec::new();
        let mut lms_rows = Vec::new();
        let mut fin_rows = Vec::new();
        let mut hold_rows = Vec::new();

        for s in students.iter_mut() {
            if s.stopped_out || term_idx < s.admit_term_idx {
                continue;
            }
            if term_idx > s.admit_term_idx && rng.gen::<f64>() < args.stopout_rate {
                s.stopped_out = true;
                continue;
            }
            if term_idx > s.admit_term_idx && rng.gen::<f64>() < args.major_change_rate {
                s.major_current = major_values[major_dist.sample(&mut rng)].to_string();
            }

            let enrolled = rng.gen::<f64>() < 0.93;
            if !enrolled {
                sis_rows.push(SisEnrollmentRow {
                    student_id: s.student_id.clone(),
                    term_code: term_code.clone(),
                    class_level: Some(
                        class_level_from_credits(s.cumulative_credits)
                            .as_str()
                            .to_string(),
                    ),
                    major: Some(s.major_current.clone()),
                    credits_attempted: None,
                    credits_earned: None,
                    term_gpa: None,
                    cumulative_gpa: gpa_or_none(s.cumulative_quality_points, s.cumulative_credits),
                    cumulative_credits: Some(s.cumulative_credits),
                    enrollment_status: "not_enrolled".to_string(),
                    registration_system: "Workday".to_string(),
                });
                continue;
            }

            let full_time = rng.gen::<f64>() < 0.8;
            let credits_attempted = if full_time {
                rng.gen_range(12..=18)
            } else {
                rng.gen_range(3..=11)
            };

            let mut term_gpa = (s.base_gpa + gpa_noise.sample(&mut rng)).clamp(0.0, 4.0);
            term_gpa = (term_gpa * 100.0).round() / 100.0;

            let completion_factor = (0.70 + 0.08 * term_gpa).clamp(0.0, 0.98);
            let credits_earned = ((credits_attempted as f64) * completion_factor).round() as u32;
            s.cumulative_credits += credits_earned;
            s.cumulative_quality_points += term_gpa * credits_earned as f64;
            let cumulative_gpa =
                (s.cumulative_quality_points / s.cumulative_credits as f64 * 100.0).round() / 100.0;

            sis_rows.push(SisEnrollmentRow {
                student_id: s.student_id.clone(),
                term_code: term_code.clone(),
                class_level: Some(
                    class_level_from_credits(s.cumulative_credits)
                        .as_str()
                        .to_string(),
                ),
                major: Some(s.major_current.clone()),
                credits_attempted: Some(credits_attempted),
                credits_earned: Some(credits_earned),
                term_gpa: Some(term_gpa),
                cumulative_gpa: Some(cumulative_gpa),
                cumulative_credits: Some(s.cumulative_credits),
                enrollment_status: "enrolled".to_string(),
                registration_system: "Workday".to_string(),
            });

            build_workday_enrollments(
                &mut rng,
                &term_code,
                &s.student_id,
                credits_attempted,
                term_gpa,
                &gpa_noise,
                &mut reg_rows,
            );

            if rng.gen::<f64>() >= args.lms_missing_rate {
                let last_activity = NaiveDate::from_ymd_opt(
                    term.year,
                    season_month(term.season),
                    rng.gen_range(1..=28),
                )
                .unwrap();
                lms_rows.push(LmsActivityRow {
                    moodle_user_key: s.moodle_user_key.clone(),
                    term_code: term_code.clone(),
                    course_shells: rng.gen_range(3..=6),
                    login_count: rng.gen_range(8..=140),
                    page_views: rng.gen_range(80..=4200),
                    assignments_submitted: rng.gen_range(0..=65),
                    forum_posts: rng.gen_range(0..=35),
                    quiz_attempts: rng.gen_range(0..=25),
                    last_activity_date: last_activity.to_string(),
                    lms_platform: "Moodle".to_string(),
                });
            }

            if rng.gen::<f64>() >= args.fin_missing_rate {
                let fafsa = rng.gen::<f64>() < 0.66;
                let pell = if fafsa && rng.gen::<f64>() < 0.32 {
                    rng.gen_range(0..=3600)
                } else {
                    0
                };
                let inst = if fafsa {
                    rng.gen_range(0..=7000)
                } else {
                    rng.gen_range(0..=2500)
                };
                let loans = if fafsa && rng.gen::<f64>() < 0.57 {
                    rng.gen_range(0..=8500)
                } else {
                    0
                };
                fin_rows.push(FinancialAidRow {
                    workday_person_id: s.workday_person_id.clone(),
                    term_code: term_code.clone(),
                    fafsa_received: fafsa,
                    pell_amount: pell,
                    institutional_grant: inst,
                    loans,
                    balance_due: rng.gen_range(-2500..=9000),
                });
            }

            if rng.gen::<f64>() < args.hold_rate {
                let placed = NaiveDate::from_ymd_opt(
                    term.year,
                    hold_month(term.season),
                    rng.gen_range(1..=28),
                )
                .unwrap();
                let cleared = if rng.gen::<f64>() < 0.58 {
                    Some(
                        NaiveDate::from_ymd_opt(
                            term.year,
                            placed.month(),
                            rng.gen_range(placed.day()..=28),
                        )
                        .unwrap(),
                    )
                } else {
                    None
                };
                let hold_types = [
                    "advising",
                    "financial",
                    "immunization",
                    "conduct",
                    "housing",
                    "workday_verification",
                ];
                hold_rows.push(AdvisingHold {
                    hold_id: uuid_from_rng(&mut rng).to_string(),
                    term_code: term_code.clone(),
                    student_id: if rng.gen::<f64>() < 0.87 {
                        Some(s.student_id.clone())
                    } else {
                        None
                    },
                    hold_type: hold_types[rng.gen_range(0..hold_types.len())].to_string(),
                    active: cleared.is_none(),
                    placed_date: placed.to_string(),
                    cleared_date: cleared.map(|d| d.to_string()),
                });
            }
        }

        write_csv(&term_dir.join("sis_enrollments.csv"), &sis_rows)?;
        write_csv(
            &term_dir.join("registrar_course_enrollments.csv"),
            &reg_rows,
        )?;
        write_csv(&term_dir.join("lms_activity.csv"), &lms_rows)?;
        write_csv(&term_dir.join("financial_aid.csv"), &fin_rows)?;
        write_json(
            &term_dir.join("advising_holds.json"),
            &hold_rows,
            args.pretty_json,
        )?;
    }

    let metadata = serde_json::json!({
        "institution": "Brandeis University",
        "students": args.students,
        "start_term": args.start_term,
        "terms": terms.iter().map(|t| t.code()).collect::<Vec<_>>(),
        "seed": args.seed,
        "systems": {
            "lms": "Moodle",
            "registration": "Workday"
        }
    });
    write_json(&args.out_dir.join("metadata.json"), &metadata, true)?;

    eprintln!("Done. Wrote outputs to {}", args.out_dir.display());
    Ok(())
}

fn gpa_or_none(qp: f64, credits: u32) -> Option<f64> {
    if credits == 0 {
        None
    } else {
        Some((qp / credits as f64 * 100.0).round() / 100.0)
    }
}

fn build_workday_enrollments<R: Rng>(
    rng: &mut R,
    term_code: &str,
    student_id: &str,
    credits_attempted: u32,
    term_gpa: f64,
    gpa_noise: &Normal<f64>,
    rows: &mut Vec<RegistrarCourseEnrollmentRow>,
) {
    let mut remaining = credits_attempted;
    let mut idx = 1u32;

    while remaining > 0 {
        let course_credits = if remaining >= 4 && rng.gen::<f64>() < 0.2 {
            4
        } else {
            3
        };
        if remaining < course_credits {
            break;
        }
        remaining -= course_credits;

        let subject = BRANDEIS_SUBJECTS[rng.gen_range(0..BRANDEIS_SUBJECTS.len())];
        let catalog = rng.gen_range(100..=199);
        let section = rng.gen_range(1..=4);
        let section_id = format!(
            "WD-{}-{}-{:03}{}",
            term_code,
            subject,
            catalog,
            section_letter(section)
        );

        let mut gp = (term_gpa + gpa_noise.sample(rng) / 3.0).clamp(0.0, 4.0);
        gp = (gp * 100.0).round() / 100.0;

        rows.push(RegistrarCourseEnrollmentRow {
            student_id: student_id.to_string(),
            term_code: term_code.to_string(),
            workday_course_section_id: section_id,
            subject: subject.to_string(),
            catalog_number: catalog.to_string(),
            credits: course_credits,
            grading_basis: "Graded".to_string(),
            letter_grade: to_letter(gp).to_string(),
            grade_points: gp,
        });

        idx += 1;
        if idx > 8 {
            break;
        }
    }
}

fn section_letter(n: u32) -> char {
    ["A", "B", "C", "D", "E"][n as usize - 1]
        .chars()
        .next()
        .unwrap()
}

fn to_letter(gp: f64) -> &'static str {
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

fn season_month(season: TermSeason) -> u32 {
    match season {
        TermSeason::SP => 4,
        TermSeason::SU => 7,
        TermSeason::FA => 11,
    }
}

fn hold_month(season: TermSeason) -> u32 {
    match season {
        TermSeason::SP => 1,
        TermSeason::SU => 5,
        TermSeason::FA => 8,
    }
}

fn sample_initial_year<R: Rng>(rng: &mut R) -> Option<ClassLevel> {
    let years = [
        Some(ClassLevel::Freshman),
        Some(ClassLevel::Sophomore),
        Some(ClassLevel::Junior),
        Some(ClassLevel::Senior),
        None,
    ];
    let weights = [400, 284, 124, 182, 11];
    let dist = WeightedIndex::new(weights).expect("year weights");
    years[dist.sample(rng)]
}

fn sample_gpa_like_js<R: Rng>(rng: &mut R) -> f64 {
    let p = [0.0, 0.25, 0.5, 0.75, 1.0];
    let x = [0.0, 3.02, 3.31, 3.57, 4.0];
    let num: f64 = rng.gen();
    for i in 0..(p.len() - 1) {
        if num >= p[i] && num < p[i + 1] {
            let t = (num - p[i]) / (p[i + 1] - p[i]);
            let v = t * (x[i + 1] - x[i]) + x[i];
            return (v * 100.0).round() / 100.0;
        }
    }
    4.0
}

fn sample_credits_bucket_like_js<R: Rng>(rng: &mut R, level: ClassLevel) -> u32 {
    let n: f64 = rng.gen();
    match level {
        ClassLevel::Freshman => (n * 30.0).floor() as u32,
        ClassLevel::Sophomore => (n * 30.0 + 30.0).floor() as u32,
        ClassLevel::Junior => (n * 30.0 + 60.0).floor() as u32,
        ClassLevel::Senior => (n * 30.0 + 90.0).floor() as u32,
    }
}

fn slugify_username(full_name: &str) -> String {
    full_name
        .to_ascii_lowercase()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect()
}

fn unique_username(base: &str, counts: &mut HashMap<String, u32>) -> String {
    let n = counts.entry(base.to_string()).or_insert(0);
    *n += 1;
    if *n == 1 {
        base.to_string()
    } else {
        format!("{}{}", base, n)
    }
}

fn random_birthdate<R: Rng>(rng: &mut R) -> NaiveDate {
    let year = rng.gen_range(1996..=2008);
    let month = rng.gen_range(1..=12);
    let day = rng.gen_range(1..=28);
    NaiveDate::from_ymd_opt(year, month, day).unwrap()
}

fn uuid_from_rng<R: Rng>(rng: &mut R) -> Uuid {
    let mut bytes = [0u8; 16];
    rng.fill(&mut bytes);
    bytes[6] = (bytes[6] & 0x0F) | 0x40;
    bytes[8] = (bytes[8] & 0x3F) | 0x80;
    Uuid::from_bytes(bytes)
}