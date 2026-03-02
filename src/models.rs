use chrono::NaiveDate;
use serde::Serialize;

#[derive(Clone, Copy, Debug)]
pub enum ClassLevel {
    Freshman,
    Sophomore,
    Junior,
    Senior,
}

impl ClassLevel {
    pub fn as_str(&self) -> &'static str {
        match self {
            ClassLevel::Freshman => "Freshman",
            ClassLevel::Sophomore => "Sophomore",
            ClassLevel::Junior => "Junior",
            ClassLevel::Senior => "Senior",
        }
    }
}

pub fn class_level_from_credits(credits: u32) -> ClassLevel {
    match credits {
        0..=29 => ClassLevel::Freshman,
        30..=59 => ClassLevel::Sophomore,
        60..=89 => ClassLevel::Junior,
        _ => ClassLevel::Senior,
    }
}

#[derive(Serialize)]
pub struct StudentMasterOut {
    pub student_id: String,
    pub full_name: String,
    pub email: String,
    pub birth_date: String,
    pub admit_term: String,
    pub initial_major: String,
    pub initial_year: Option<String>,
    pub primary_system: String,
}

#[derive(Clone)]
pub struct StudentInternal {
    pub student_id: String,
    pub full_name: String,
    pub email: String,
    pub moodle_user_key: String,
    pub workday_person_id: String,
    pub birth_date: NaiveDate,
    pub admit_term_idx: usize,
    pub major_current: String,
    pub base_gpa: f64,
    pub cumulative_credits: u32,
    pub cumulative_quality_points: f64,
    pub stopped_out: bool,
}

#[derive(Serialize)]
pub struct CrosswalkRow {
    pub student_id: String,
    pub moodle_user_key: String,
    pub workday_person_id: String,
}

#[derive(Serialize)]
pub struct SisEnrollmentRow {
    pub student_id: String,
    pub term_code: String,
    pub class_level: Option<String>,
    pub major: Option<String>,
    pub credits_attempted: Option<u32>,
    pub credits_earned: Option<u32>,
    pub term_gpa: Option<f64>,
    pub cumulative_gpa: Option<f64>,
    pub cumulative_credits: Option<u32>,
    pub enrollment_status: String,
    pub registration_system: String,
}

#[derive(Serialize)]
pub struct RegistrarCourseEnrollmentRow {
    pub student_id: String,
    pub term_code: String,
    pub workday_course_section_id: String,
    pub subject: String,
    pub catalog_number: String,
    pub credits: u32,
    pub grading_basis: String,
    pub letter_grade: String,
    pub grade_points: f64,
}

#[derive(Serialize)]
pub struct LmsActivityRow {
    pub moodle_user_key: String,
    pub term_code: String,
    pub course_shells: u32,
    pub login_count: u32,
    pub page_views: u32,
    pub assignments_submitted: u32,
    pub forum_posts: u32,
    pub quiz_attempts: u32,
    pub last_activity_date: String,
    pub lms_platform: String,
}

#[derive(Serialize)]
pub struct FinancialAidRow {
    pub workday_person_id: String,
    pub term_code: String,
    pub fafsa_received: bool,
    pub pell_amount: u32,
    pub institutional_grant: u32,
    pub loans: u32,
    pub balance_due: i32,
}

#[derive(Serialize)]
pub struct AdvisingHold {
    pub hold_id: String,
    pub term_code: String,
    pub student_id: Option<String>,
    pub hold_type: String,
    pub active: bool,
    pub placed_date: String,
    pub cleared_date: Option<String>,
}