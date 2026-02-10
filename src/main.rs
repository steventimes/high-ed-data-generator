use anyhow::{Context, Result};
use clap::Parser;
use chrono::{Datelike, NaiveDate};
use csv::WriterBuilder;
use rand::distributions::{Distribution, WeightedIndex};
use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;
use rand_distr::Normal;
use serde::Serialize;
use std::collections::HashMap;
use std::fs;
use std::io::BufWriter;
use std::path::{Path, PathBuf};
use uuid::Uuid;

#[derive(Parser, Debug)]
#[command(
    name = "higher-ed-synth",
    about = "Generate semester-fragmented synthetic higher-ed admin datasets"
)]
struct Args {
    /// Number of students (SIS population)
    #[arg(long, default_value_t = 200)]
    students: usize,

    /// Start term code like 2023FA, 2024SP, 2024SU
    #[arg(long, default_value = "2023FA")]
    start_term: String,

    /// Number of sequential terms to generate (FA->SP->SU->FA by default cycle rule)
    #[arg(long, default_value_t = 4)]
    terms: usize,

    /// RNG seed for deterministic output
    #[arg(long, default_value_t = 42)]
    seed: u64,

    /// Output directory
    #[arg(long, default_value = "./out")]
    out_dir: PathBuf,

    /// Probability a student changes major in a given term (if enrolled)
    #[arg(long, default_value_t = 0.04)]
    major_change_rate: f64,

    /// Probability a student stops out after a term (once stopped out, no future SIS rows)
    #[arg(long, default_value_t = 0.03)]
    stopout_rate: f64,

    /// Probability an enrolled student is missing from LMS extract (course not in LMS, sync issue)
    #[arg(long, default_value_t = 0.10)]
    lms_missing_rate: f64,

    /// Probability an enrolled student is missing from financial aid extract
    #[arg(long, default_value_t = 0.45)]
    fin_missing_rate: f64,

    /// Probability a student has an advising hold record in a term
    #[arg(long, default_value_t = 0.12)]
    hold_rate: f64,

    /// Probability that some IDs in the crosswalk are wrong/swapped (join pain)
    #[arg(long, default_value_t = 0.01)]
    crosswalk_mismatch_rate: f64,

    /// Pretty-print JSON outputs
    #[arg(long, default_value_t = false)]
    pretty_json: bool,
}

#[derive(Clone, Copy, Debug)]
enum TermSeason {
    SP,
    SU,
    FA,
}

#[derive(Clone, Copy, Debug)]
struct Term {
    year: i32,
    season: TermSeason,
}

impl Term {
    fn code(&self) -> String {
        let s = match self.season {
            TermSeason::SP => "SP",
            TermSeason::SU => "SU",
            TermSeason::FA => "FA",
        };
        format!("{}{}", self.year, s)
    }

    fn next(&self) -> Term {
        match self.season {
            TermSeason::FA => Term {
                year: self.year + 1,
                season: TermSeason::SP,
            },
            TermSeason::SP => Term {
                year: self.year,
                season: TermSeason::SU,
            },
            TermSeason::SU => Term {
                year: self.year,
                season: TermSeason::FA,
            },
        }
    }
}

fn parse_term_code(s: &str) -> Result<Term> {
    if s.len() != 6 {
        anyhow::bail!("term code must look like 2023FA / 2024SP / 2024SU");
    }
    let year: i32 = s[0..4].parse().context("failed to parse year")?;
    let season = match &s[4..6] {
        "FA" => TermSeason::FA,
        "SP" => TermSeason::SP,
        "SU" => TermSeason::SU,
        other => anyhow::bail!("unsupported season {other} (use FA/SP/SU)"),
    };
    Ok(Term { year, season })
}

#[derive(Clone, Copy, Debug)]
enum ClassLevel {
    Freshman,
    Sophomore,
    Junior,
    Senior,
}

impl ClassLevel {
    fn as_str(&self) -> &'static str {
        match self {
            ClassLevel::Freshman => "Freshman",
            ClassLevel::Sophomore => "Sophomore",
            ClassLevel::Junior => "Junior",
            ClassLevel::Senior => "Senior",
        }
    }
}

fn class_level_from_credits(credits: u32) -> ClassLevel {
    match credits {
        0..=29 => ClassLevel::Freshman,
        30..=59 => ClassLevel::Sophomore,
        60..=89 => ClassLevel::Junior,
        _ => ClassLevel::Senior,
    }
}

// Ported from your JS major list + weights.
static MAJORS: &[(&str, u32)] = &[
    ("Agriculture and natural resources", 40675),
    ("Architecture and related services", 9462),
    ("Area, ethnic, cultural, gender, and group studies", 6658),
    ("Biological and biomedical sciences", 131462),
    ("Business", 375418),
    ("Communication, journalism, and related programs", 86043),
    ("Communications technologies", 4851),
    ("Computer and information sciences and support services", 108503),
    ("Education", 89410),
    ("Engineering", 123017),
    ("Engineering technologies", 18405),
    ("English language and literature/letters", 33429),
    ("Family and consumer sciences/human sciences", 20630),
    ("Foreign languages, literatures, and linguistics", 13912),
    ("Health professions and related programs", 263765),
    ("Homeland security, law enforcement, and firefighting", 56901),
    ("Legal professions and studies", 4444),
    ("Liberal arts and sciences, general studies, and humanities", 37887),
    ("Library science", 135),
    ("Mathematics and statistics", 26212),
    ("Military technologies and applied sciences", 1602),
    ("Multi/interdisciplinary studies", 52573),
    ("Parks, recreation, leisure, fitness, and kinesiology", 52776),
    ("Philosophy and religious studies", 11230),
    ("Physical sciences and science technologies", 28301),
    ("Precision production", 12),
    ("Psychology", 129609),
    ("Public administration and social services", 33429),
    ("Social sciences and history", 151109),
    ("Theology and religious vocations", 6394),
    ("Transportation and materials moving", 6540),
    ("Visual and performing arts", 90241),
];

fn build_major_sampler() -> (Vec<&'static str>, WeightedIndex<u32>) {
    let majors: Vec<&'static str> = MAJORS.iter().map(|(m, _)| *m).collect();
    let weights: Vec<u32> = MAJORS.iter().map(|(_, w)| *w).collect();
    let dist = WeightedIndex::new(weights).expect("major weights must be valid");
    (majors, dist)
}

// Ported from your JS getYear: percentages [40, 28.4, 12.4, 18.2, 1.1] with Unclassified -> null.
fn sample_initial_year<R: Rng>(rng: &mut R) -> Option<ClassLevel> {
    // Scale by 10 to preserve one decimal place.
    let years: [Option<ClassLevel>; 5] = [
        Some(ClassLevel::Freshman),
        Some(ClassLevel::Sophomore),
        Some(ClassLevel::Junior),
        Some(ClassLevel::Senior),
        None, // Unclassified
    ];
    let weights: [u32; 5] = [400, 284, 124, 182, 11];
    let dist = WeightedIndex::new(weights).expect("year weights");
    years[dist.sample(rng)]
}

// Ported from your JS getGPA piecewise interpolation.
// p=[0,0.25,0.5,0.75,1], x=[0,3.02,3.31,3.57,4]
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

// Ported from your JS getCreditsEarned(year): ranges by class level, 30-credit buckets.
fn sample_credits_bucket_like_js<R: Rng>(rng: &mut R, lvl: ClassLevel) -> u32 {
    let n: f64 = rng.gen();
    match lvl {
        ClassLevel::Freshman => (n * 30.0).floor() as u32,
        ClassLevel::Sophomore => (n * 30.0 + 30.0).floor() as u32,
        ClassLevel::Junior => (n * 30.0 + 60.0).floor() as u32,
        ClassLevel::Senior => (n * 30.0 + 90.0).floor() as u32,
    }
}

// Simple ASCII-ish slugify for email usernames.
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

fn uuid_from_rng<R: Rng>(rng: &mut R) -> Uuid {
    let mut bytes = [0u8; 16];
    rng.fill(&mut bytes);
    // Force RFC4122 version 4 semantics.
    bytes[6] = (bytes[6] & 0x0F) | 0x40;
    bytes[8] = (bytes[8] & 0x3F) | 0x80;
    Uuid::from_bytes(bytes)
}

fn random_birthdate<R: Rng>(rng: &mut R) -> NaiveDate {
    // Roughly 18–30 years old
    let year = rng.gen_range(1996..=2008);
    let month = rng.gen_range(1..=12);
    let day = rng.gen_range(1..=28);
    NaiveDate::from_ymd_opt(year, month, day).unwrap()
}

fn ensure_dir(p: &Path) -> Result<()> {
    fs::create_dir_all(p).with_context(|| format!("failed to create dir {}", p.display()))
}

#[derive(Serialize)]
struct StudentMasterOut {
    student_id: String,
    full_name: String,
    email: String,
    birth_date: String,
    admit_term: String,
    initial_major: String,
    initial_year: Option<String>,
}

#[derive(Clone)]
struct StudentInternal {
    student_id: String,
    full_name: String,
    email: String,

    lms_user_id: String,
    fin_person_id: String,

    birth_date: NaiveDate,
    admit_term_idx: usize, // index into terms array

    major_current: String,
    base_gpa: f64,

    cumulative_credits: u32,
    cumulative_quality_points: f64,

    stopped_out: bool,
}

#[derive(Serialize)]
struct CrosswalkRow {
    student_id: String,
    lms_user_id: String,
    fin_person_id: String,
}

#[derive(Serialize)]
struct SisEnrollmentRow {
    student_id: String,
    term_code: String,
    class_level: Option<String>,
    major: Option<String>,
    credits_attempted: Option<u32>,
    credits_earned: Option<u32>,
    term_gpa: Option<f64>,
    cumulative_gpa: Option<f64>,
    cumulative_credits: Option<u32>,
    enrollment_status: String,
}

#[derive(Serialize)]
struct RegistrarCourseEnrollmentRow {
    student_id: String,
    term_code: String,
    crn: String,
    subject: String,
    catalog_number: String,
    credits: u32,
    letter_grade: String,
    grade_points: f64,
}

#[derive(Serialize)]
struct LmsActivityRow {
    lms_user_id: String,
    term_code: String,
    course_count: u32,
    login_count: u32,
    page_views: u32,
    assignments_submitted: u32,
    last_activity_date: String,
}

#[derive(Serialize)]
struct FinancialAidRow {
    fin_person_id: String,
    term_code: String,
    fafsa_received: bool,
    pell_amount: u32,
    institutional_grant: u32,
    loans: u32,
    balance_due: i32,
}

#[derive(Serialize)]
struct AdvisingHold {
    hold_id: String,
    term_code: String,
    // Sometimes holds exports have SIS IDs; sometimes not. Keep it nullable.
    student_id: Option<String>,
    hold_type: String,
    active: bool,
    placed_date: String,
    cleared_date: Option<String>,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let mut rng = ChaCha8Rng::seed_from_u64(args.seed);

    // Build terms
    let mut terms = Vec::with_capacity(args.terms);
    let mut t = parse_term_code(&args.start_term)?;
    for _ in 0..args.terms {
        terms.push(t);
        t = t.next();
    }

    // Output dirs
    ensure_dir(&args.out_dir)?;
    ensure_dir(&args.out_dir.join("terms"))?;

    // Major sampler
    let (major_values, major_dist) = build_major_sampler();

    // Email uniqueness
    let mut email_counts: HashMap<String, u32> = HashMap::new();

    // Create students
    let mut students: Vec<StudentInternal> = Vec::with_capacity(args.students);

    let first_names = [
        "Alex", "Jordan", "Taylor", "Riley", "Casey", "Morgan", "Avery", "Jamie", "Quinn", "Cameron",
        "Devin", "Parker", "Reese", "Skyler", "Rowan", "Sydney", "Drew", "Hayden", "Emerson", "Kendall",
    ];
    let last_names = [
        "Kim", "Patel", "Garcia", "Nguyen", "Johnson", "Smith", "Brown", "Davis", "Miller", "Wilson",
        "Martinez", "Anderson", "Thomas", "Jackson", "White", "Harris", "Clark", "Lewis", "Walker", "Young",
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
        let email = format!("{}@example.edu", username);

        let major = major_values[major_dist.sample(&mut rng)].to_string();
        let init_year = sample_initial_year(&mut rng);
        let credits_start = init_year.map(|y| sample_credits_bucket_like_js(&mut rng, y)).unwrap_or(0);

        let base_gpa = sample_gpa_like_js(&mut rng);
        let cumulative_quality_points = base_gpa * credits_start as f64;

        // 70% are present starting in the first term; 30% admit later terms.
        let admit_term_idx = if rng.gen::<f64>() < 0.70 {
            0
        } else {
            rng.gen_range(0..args.terms)
        };

        students.push(StudentInternal {
            student_id,
            full_name,
            email,
            lms_user_id: uuid_from_rng(&mut rng).to_string(),
            fin_person_id: format!("{:08}", rng.gen_range(0..100_000_000u32)),
            birth_date: random_birthdate(&mut rng),
            admit_term_idx,
            major_current: major,
            base_gpa,
            cumulative_credits: credits_start,
            cumulative_quality_points,
            stopped_out: false,
        });
    }

    // Inject crosswalk mismatches by swapping some LMS IDs (join pain)
    if args.crosswalk_mismatch_rate > 0.0 && students.len() >= 2 {
        let swaps = ((students.len() as f64) * args.crosswalk_mismatch_rate).round() as usize;
        for _ in 0..swaps {
            let a = rng.gen_range(0..students.len());
            let mut b = rng.gen_range(0..students.len());
            while b == a {
                b = rng.gen_range(0..students.len());
            }
            let tmp = students[a].lms_user_id.clone();
            students[a].lms_user_id = students[b].lms_user_id.clone();
            students[b].lms_user_id = tmp;
        }
    }

    // Write students_master.json
    let students_master: Vec<StudentMasterOut> = students
        .iter()
        .map(|s| StudentMasterOut {
            student_id: s.student_id.clone(),
            full_name: s.full_name.clone(),
            email: s.email.clone(),
            birth_date: s.birth_date.to_string(),
            admit_term: terms[s.admit_term_idx].code(),
            initial_major: s.major_current.clone(),
            initial_year: Some(class_level_from_credits(s.cumulative_credits).as_str().to_string()),
        })
        .collect();

    let master_path = args.out_dir.join("students_master.json");
    let master_file = fs::File::create(&master_path)?;
    let master_writer = BufWriter::new(master_file);
    if args.pretty_json {
        serde_json::to_writer_pretty(master_writer, &students_master)?;
    } else {
        serde_json::to_writer(master_writer, &students_master)?;
    }

    // Write identity_crosswalk.csv
    let crosswalk_path = args.out_dir.join("identity_crosswalk.csv");
    let mut cw = WriterBuilder::new().has_headers(true).from_path(&crosswalk_path)?;
    for s in &students {
        cw.serialize(CrosswalkRow {
            student_id: s.student_id.clone(),
            lms_user_id: s.lms_user_id.clone(),
            fin_person_id: s.fin_person_id.clone(),
        })?;
    }
    cw.flush()?;

    // GPA noise per term
    let gpa_noise = Normal::new(0.0, 0.35).unwrap();

    // Per-term generation
    for (term_idx, term) in terms.iter().enumerate() {
        let term_code = term.code();
        let term_dir = args.out_dir.join("terms").join(&term_code);
        ensure_dir(&term_dir)?;

        let mut sis_rows: Vec<SisEnrollmentRow> = Vec::new();
        let mut reg_rows: Vec<RegistrarCourseEnrollmentRow> = Vec::new();
        let mut lms_rows: Vec<LmsActivityRow> = Vec::new();
        let mut fin_rows: Vec<FinancialAidRow> = Vec::new();
        let mut hold_rows: Vec<AdvisingHold> = Vec::new();

        // Some shared course subjects
        let subjects = ["CSCI", "MATH", "BIOL", "CHEM", "PSYC", "ECON", "SOCI", "ENGL", "HIST", "BUS"];

        for s in students.iter_mut() {
            if s.stopped_out {
                continue;
            }
            if term_idx < s.admit_term_idx {
                continue;
            }

            // stop-out after prior term
            if term_idx > s.admit_term_idx && rng.gen::<f64>() < args.stopout_rate {
                s.stopped_out = true;
                continue;
            }

            // Major change
            if term_idx > s.admit_term_idx && rng.gen::<f64>() < args.major_change_rate {
                s.major_current = major_values[major_dist.sample(&mut rng)].to_string();
            }

            // Enrollment status (some become "not enrolled" but still exist in master)
            let enrolled = rng.gen::<f64>() < 0.92;
            if !enrolled {
                sis_rows.push(SisEnrollmentRow {
                    student_id: s.student_id.clone(),
                    term_code: term_code.clone(),
                    class_level: Some(class_level_from_credits(s.cumulative_credits).as_str().to_string()),
                    major: Some(s.major_current.clone()),
                    credits_attempted: None,
                    credits_earned: None,
                    term_gpa: None,
                    cumulative_gpa: if s.cumulative_credits > 0 {
                        Some((s.cumulative_quality_points / (s.cumulative_credits as f64) * 100.0).round() / 100.0)
                    } else {
                        None
                    },
                    cumulative_credits: Some(s.cumulative_credits),
                    enrollment_status: "not_enrolled".to_string(),
                });
                continue;
            }

            // Credit load
            let full_time = rng.gen::<f64>() < 0.75;
            let credits_attempted: u32 = if full_time {
                rng.gen_range(12..=18)
            } else {
                rng.gen_range(3..=11)
            };

            // Term GPA derived from base + noise, clipped to [0,4]
            let mut term_gpa = s.base_gpa + gpa_noise.sample(&mut rng);
            if term_gpa < 0.0 {
                term_gpa = 0.0;
            } else if term_gpa > 4.0 {
                term_gpa = 4.0;
            }
            term_gpa = (term_gpa * 100.0).round() / 100.0;

            // Completion: fewer completions when GPA is low
            let completion_factor = (0.70 + 0.08 * term_gpa).clamp(0.0, 0.98);
            let credits_earned = ((credits_attempted as f64) * completion_factor).round() as u32;

            // Update cumulative metrics
            s.cumulative_credits += credits_earned;
            s.cumulative_quality_points += term_gpa * (credits_earned as f64);
            let cumulative_gpa = if s.cumulative_credits > 0 {
                (s.cumulative_quality_points / (s.cumulative_credits as f64) * 100.0).round() / 100.0
            } else {
                0.0
            };

            let lvl = class_level_from_credits(s.cumulative_credits);

            sis_rows.push(SisEnrollmentRow {
                student_id: s.student_id.clone(),
                term_code: term_code.clone(),
                class_level: Some(lvl.as_str().to_string()),
                major: Some(s.major_current.clone()),
                credits_attempted: Some(credits_attempted),
                credits_earned: Some(credits_earned),
                term_gpa: Some(term_gpa),
                cumulative_gpa: Some(cumulative_gpa),
                cumulative_credits: Some(s.cumulative_credits),
                enrollment_status: "enrolled".to_string(),
            });

            // Registrar course enrollments: approximate course count (mostly 3-credit courses)
            let mut remaining = credits_attempted;
            let mut course_idx = 0u32;
            while remaining > 0 {
                let course_credits = if remaining >= 4 && rng.gen::<f64>() < 0.20 { 4 } else { 3 };
                if remaining < course_credits {
                    break;
                }
                remaining -= course_credits;

                let subj = subjects[rng.gen_range(0..subjects.len())];
                let num = rng.gen_range(100..=499);
                let section = rng.gen_range(1..=5);
                let crn = format!("{}{:03}{:02}", term_code, num, section);

                // Grade points around term_gpa
                let mut gp = term_gpa + (gpa_noise.sample(&mut rng) / 3.0);
                gp = gp.clamp(0.0, 4.0);
                gp = (gp * 100.0).round() / 100.0;

                let letter = if gp >= 3.85 {
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
                };

                reg_rows.push(RegistrarCourseEnrollmentRow {
                    student_id: s.student_id.clone(),
                    term_code: term_code.clone(),
                    crn,
                    subject: subj.to_string(),
                    catalog_number: num.to_string(),
                    credits: course_credits,
                    letter_grade: letter.to_string(),
                    grade_points: gp,
                });

                course_idx += 1;
                if course_idx > 8 {
                    break;
                }
            }

            // LMS activity (missing for some)
            if rng.gen::<f64>() >= args.lms_missing_rate {
                let last_activity = NaiveDate::from_ymd_opt(term.year, match term.season {
                    TermSeason::SP => 4,
                    TermSeason::SU => 7,
                    TermSeason::FA => 11,
                }, rng.gen_range(1..=28)).unwrap();

                lms_rows.push(LmsActivityRow {
                    lms_user_id: s.lms_user_id.clone(),
                    term_code: term_code.clone(),
                    course_count: rng.gen_range(3..=6),
                    login_count: rng.gen_range(5..=120),
                    page_views: rng.gen_range(50..=4000),
                    assignments_submitted: rng.gen_range(0..=60),
                    last_activity_date: last_activity.to_string(),
                });
            }

            // Financial aid (missing for many)
            if rng.gen::<f64>() >= args.fin_missing_rate {
                let fafsa = rng.gen::<f64>() < 0.65;
                let pell = if fafsa && rng.gen::<f64>() < 0.35 { rng.gen_range(0..=3500) } else { 0 };
                let inst = if fafsa { rng.gen_range(0..=6000) } else { rng.gen_range(0..=2000) };
                let loans = if fafsa && rng.gen::<f64>() < 0.55 { rng.gen_range(0..=7500) } else { 0 };
                let balance_due = rng.gen_range(-2000..=8000);

                fin_rows.push(FinancialAidRow {
                    fin_person_id: s.fin_person_id.clone(),
                    term_code: term_code.clone(),
                    fafsa_received: fafsa,
                    pell_amount: pell,
                    institutional_grant: inst,
                    loans,
                    balance_due,
                });
            }

            // Advising holds (semi-structured, sometimes missing student_id)
            if rng.gen::<f64>() < args.hold_rate {
                let placed = NaiveDate::from_ymd_opt(term.year, match term.season {
                    TermSeason::SP => 1,
                    TermSeason::SU => 5,
                    TermSeason::FA => 8,
                }, rng.gen_range(1..=28)).unwrap();

                let cleared = if rng.gen::<f64>() < 0.55 {
                    Some(NaiveDate::from_ymd_opt(term.year, placed.month(), rng.gen_range(placed.day()..=28)).unwrap())
                } else {
                    None
                };

                let hold_types = ["advising", "financial", "immunization", "conduct", "housing"];
                let ht = hold_types[rng.gen_range(0..hold_types.len())].to_string();

                hold_rows.push(AdvisingHold {
                    hold_id: uuid_from_rng(&mut rng).to_string(),
                    term_code: term_code.clone(),
                    student_id: if rng.gen::<f64>() < 0.85 { Some(s.student_id.clone()) } else { None },
                    hold_type: ht,
                    active: cleared.is_none(),
                    placed_date: placed.to_string(),
                    cleared_date: cleared.map(|d| d.to_string()),
                });
            }
        }

        // Write term outputs
        write_csv(&term_dir.join("sis_enrollments.csv"), &sis_rows)?;
        write_csv(&term_dir.join("registrar_course_enrollments.csv"), &reg_rows)?;
        write_csv(&term_dir.join("lms_activity.csv"), &lms_rows)?;
        write_csv(&term_dir.join("financial_aid.csv"), &fin_rows)?;
        write_json(&term_dir.join("advising_holds.json"), &hold_rows, args.pretty_json)?;
    }

    // Lightweight metadata
    let meta = serde_json::json!({
        "students": args.students,
        "start_term": args.start_term,
        "terms": terms.iter().map(|t| t.code()).collect::<Vec<_>>(),
        "seed": args.seed
    });

    write_json(&args.out_dir.join("metadata.json"), &meta, true)?;

    eprintln!("Done. Wrote outputs to {}", args.out_dir.display());
    Ok(())
}

fn write_csv<T: Serialize>(path: &Path, rows: &Vec<T>) -> Result<()> {
    let mut w = WriterBuilder::new()
        .has_headers(true)
        .from_path(path)
        .with_context(|| format!("failed to create csv {}", path.display()))?;
    for r in rows {
        w.serialize(r)?;
    }
    w.flush()?;
    Ok(())
}

fn write_json<T: Serialize>(path: &Path, v: &T, pretty: bool) -> Result<()> {
    let f = fs::File::create(path).with_context(|| format!("failed to create {}", path.display()))?;
    let bw = BufWriter::new(f);
    if pretty {
        serde_json::to_writer_pretty(bw, v)?;
    } else {
        serde_json::to_writer(bw, v)?;
    }
    Ok(())
}
