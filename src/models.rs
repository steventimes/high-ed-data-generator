use serde::Serialize;

#[derive(Clone, Copy, Debug)]
pub enum ClassLevel {
    Freshman,
    Sophomore,
    Junior,
    Senior,
}

impl ClassLevel {
    pub fn as_str(self) -> &'static str {
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StudentLifecycle {
    Active,
    StoppedOut,
    Withdrawn,
    TransferredOut,
}

#[derive(Clone)]
pub struct StudentInternal {
    pub student_id: String,
    pub institutional_email: String,
    pub lms_user_id: String,
    pub sis_user_id: String,
    pub integration_id: String,
    pub erp_person_id: String,
    pub birth_year: i32,
    pub gender: String,
    pub race_ethnicity: String,
    pub first_gen_flag: Option<bool>,
    pub veteran_status: Option<bool>,
    pub disability_status: Option<bool>,
    pub age_at_entry: u8,
    pub state_of_residence_at_entry: Option<String>,
    pub country_of_origin: String,
    pub citizenship_status: String,
    pub initial_enrollment_term_idx: usize,
    pub cohort_year: String,
    pub admit_type: String,
    pub hs_unweighted_gpa: Option<f64>,
    pub hs_weighted_gpa: Option<f64>,
    pub student_level: String,
    pub academic_career: String,
    pub residency_status: String,
    pub major_label: String,
    pub major_cip_code: String,
    pub major_college: String,
    pub likely_subject: String,
    pub second_major_cip_code: Option<String>,
    pub transfer_credits_accepted: u32,
    pub base_gpa: f64,
    pub cumulative_credits_earned: u32,
    pub cumulative_gpa_credits: u32,
    pub cumulative_quality_points: f64,
    pub financial_need_index: f64,
    pub engagement_index: f64,
    pub residential_flag: bool,
    pub lifecycle: StudentLifecycle,
    pub stopout_terms_remaining: u8,
}

#[derive(Clone, Serialize)]
pub struct StudentDemographicsRow {
    pub student_id: String,
    pub birth_year: i32,
    pub gender: String,
    pub race_ethnicity: String,
    pub first_gen_flag: Option<bool>,
    pub veteran_status: Option<bool>,
    pub disability_status: Option<bool>,
    pub age_at_entry: u8,
    pub country_of_origin: String,
    pub citizenship_status: String,
    pub initial_enrollment_term: String,
    pub cohort_year: String,
    pub admit_type: String,
    pub hs_unweighted_gpa: Option<f64>,
}

#[derive(Clone, Serialize)]
pub struct StudentDemographicsWideRow {
    pub student_id: String,
    pub birth_year: i32,
    pub gender: String,
    pub race_ethnicity: String,
    pub first_gen_flag: Option<bool>,
    pub veteran_status: Option<bool>,
    pub disability_status: Option<bool>,
    pub age_at_entry: u8,
    pub country_of_origin: String,
    pub citizenship_status: String,
    pub initial_enrollment_term: String,
    pub cohort_year: String,
    pub admit_type: String,
    pub hs_unweighted_gpa: Option<f64>,
    pub state_of_residence_at_entry: Option<String>,
    pub hs_weighted_gpa: Option<f64>,
}

#[derive(Clone, Serialize)]
pub struct IdentityCrosswalkRow {
    pub student_id: String,
    pub lms_user_id: Option<String>,
    pub sis_user_id: Option<String>,
    pub integration_id: Option<String>,
    pub erp_person_id: Option<String>,
    pub effective_start_date: String,
    pub effective_end_date: Option<String>,
    pub active_flag: bool,
    pub source_system: String,
    pub match_rule: String,
    pub match_confidence: f64,
}

#[derive(Clone, Serialize)]
pub struct IdentityCrosswalkWideRow {
    pub student_id: String,
    pub lms_user_id: Option<String>,
    pub sis_user_id: Option<String>,
    pub integration_id: Option<String>,
    pub erp_person_id: Option<String>,
    pub institutional_email: Option<String>,
    pub effective_start_date: String,
    pub effective_end_date: Option<String>,
    pub active_flag: bool,
    pub source_system: String,
    pub match_rule: String,
    pub match_confidence: f64,
}

#[derive(Clone, Serialize)]
pub struct SisEnrollmentRow {
    pub student_id: String,
    pub academic_year: String,
    pub term: String,
    pub student_level: String,
    pub major_cip_code: String,
    pub major_label: String,
    pub second_major_cip_code: Option<String>,
    pub enrollment_type: String,
    pub first_time_flag: bool,
    pub transfer_credits_accepted: u32,
    pub credits_attempted: Option<u32>,
    pub credits_earned: Option<u32>,
    pub term_gpa: Option<f64>,
    pub cumulative_gpa: Option<f64>,
    pub credits_earned_cumulative: Option<u32>,
    pub enrollment_status: String,
    pub full_time_flag: bool,
    pub term_start_date: String,
    pub term_end_date: String,
}

#[derive(Clone, Serialize)]
pub struct SisEnrollmentWideRow {
    pub student_id: String,
    pub academic_year: String,
    pub term: String,
    pub student_level: String,
    pub major_cip_code: String,
    pub major_label: String,
    pub second_major_cip_code: Option<String>,
    pub enrollment_type: String,
    pub first_time_flag: bool,
    pub transfer_credits_accepted: u32,
    pub credits_attempted: Option<u32>,
    pub credits_earned: Option<u32>,
    pub term_gpa: Option<f64>,
    pub cumulative_gpa: Option<f64>,
    pub credits_earned_cumulative: Option<u32>,
    pub enrollment_status: String,
    pub full_time_flag: bool,
    pub term_start_date: String,
    pub term_end_date: String,
    pub term_code: Option<String>,
    pub class_level: Option<String>,
    pub academic_career: String,
    pub college: String,
    pub residency_status: String,
    pub institutional_email: Option<String>,
}

#[derive(Clone, Serialize)]
pub struct RegistrarCourseEnrollmentRow {
    pub student_id: String,
    pub academic_year: String,
    pub term: String,
    pub course_section_id: String,
    pub course_prefix: String,
    pub course_number: String,
    pub course_name: String,
    pub course_cip_code: String,
    pub course_level_type: String,
    pub delivery_method: String,
    pub credits_attempted_course: u32,
    pub credits_earned_course: u32,
    pub grade: String,
    pub course_begin_date: String,
    pub course_end_date: String,
}

#[derive(Clone, Serialize)]
pub struct RegistrarCourseEnrollmentWideRow {
    pub student_id: String,
    pub academic_year: String,
    pub term: String,
    pub course_section_id: String,
    pub course_prefix: String,
    pub course_number: String,
    pub course_name: String,
    pub course_cip_code: String,
    pub course_level_type: String,
    pub delivery_method: String,
    pub credits_attempted_course: u32,
    pub credits_earned_course: u32,
    pub grade: String,
    pub course_begin_date: String,
    pub course_end_date: String,
    pub grading_basis: String,
    pub letter_grade: Option<String>,
    pub grade_points: Option<f64>,
    pub repeat_flag: bool,
    pub withdrawal_flag: bool,
    pub enrollment_status: String,
    pub instructor_id: String,
    pub meeting_pattern: Option<String>,
    pub section_capacity: u32,
    pub section_enrollment: u32,
    pub grade_posted_date: Option<String>,
}

#[derive(Clone, Serialize)]
pub struct FacultyCourseRow {
    pub instructor_id: String,
    pub academic_year: String,
    pub term: String,
    pub course_section_id: String,
    pub faculty_rank: String,
    pub tenure_status: String,
    pub employment_status: String,
}

#[derive(Clone, Serialize)]
pub struct FacultyCourseWideRow {
    pub instructor_id: String,
    pub academic_year: String,
    pub term: String,
    pub course_section_id: String,
    pub faculty_rank: String,
    pub tenure_status: String,
    pub employment_status: String,
    pub teaching_load_credits: u32,
    pub home_department: String,
    pub primary_instruction_mode: String,
}

#[derive(Clone, Serialize)]
pub struct LmsActivityRawRow {
    pub lms_user_id: String,
    pub sis_user_id: Option<String>,
    pub integration_id: Option<String>,
    pub course_id: String,
    pub sis_course_id: Option<String>,
    pub section_id: String,
    pub sis_section_id: Option<String>,
    pub event_timestamp: String,
    pub event_type: String,
    pub enrollment_state: String,
    pub lms_enrollment_state_at_term_end: String,
    pub last_activity_at: Option<String>,
    pub total_activity_time_seconds: u32,
    pub submission_late: Option<bool>,
    pub submission_missing: Option<bool>,
    pub submitted_at: Option<String>,
    pub grade: Option<String>,
}

#[derive(Clone, Serialize)]
pub struct LmsActivityRawWideRow {
    pub lms_user_id: String,
    pub sis_user_id: Option<String>,
    pub integration_id: Option<String>,
    pub course_id: String,
    pub sis_course_id: Option<String>,
    pub section_id: String,
    pub sis_section_id: Option<String>,
    pub event_timestamp: String,
    pub event_type: String,
    pub enrollment_state: String,
    pub lms_enrollment_state_at_term_end: String,
    pub last_activity_at: Option<String>,
    pub total_activity_time_seconds: u32,
    pub submission_late: Option<bool>,
    pub submission_missing: Option<bool>,
    pub submitted_at: Option<String>,
    pub grade: Option<String>,
    pub student_id: Option<String>,
    pub academic_year: String,
    pub term: String,
}

#[derive(Clone, Serialize)]
pub struct LmsActivityRow {
    pub student_id: String,
    pub academic_year: String,
    pub term: String,
    pub distinct_course_count: u32,
    pub login_count: u32,
    pub active_days_count: u32,
    pub page_views: u32,
    pub submissions_count: u32,
    pub assignment_count_total: u32,
    pub discussion_posts_count: u32,
    pub quiz_attempts_count: u32,
    pub weekend_events_count: u32,
    pub late_night_events_count: u32,
    pub total_activity_time_seconds: u32,
    pub first_activity_date: String,
    pub last_activity_date: String,
    pub missing_submission_count: u32,
}

#[derive(Clone, Serialize)]
pub struct LmsActivityWideRow {
    pub student_id: String,
    pub sis_user_id: Option<String>,
    pub academic_year: String,
    pub term: String,
    pub distinct_course_count: u32,
    pub login_count: u32,
    pub active_days_count: u32,
    pub page_views: u32,
    pub submissions_count: u32,
    pub assignment_count_total: u32,
    pub discussion_posts_count: u32,
    pub quiz_attempts_count: u32,
    pub weekend_events_count: u32,
    pub late_night_events_count: u32,
    pub total_activity_time_seconds: u32,
    pub first_activity_date: String,
    pub last_activity_date: String,
    pub missing_submission_count: u32,
    pub content_interaction_count: u32,
    pub forum_post_length_avg: Option<f64>,
    pub assignment_review_count: u32,
    pub lms_activity_regularity_index: f64,
    pub current_grade_visible_flag: bool,
}

#[derive(Clone, Serialize)]
pub struct FinancialAidRow {
    pub student_id: String,
    pub academic_year: String,
    pub fafsa_filed_flag: bool,
    pub applied_aid_flag: bool,
    pub need_index_regime: String,
    pub need_index_value: Option<i32>,
    pub pell_amount: u32,
    pub federal_seog_amount: u32,
    pub state_grant_need_based: u32,
    pub state_grant_non_need_based: u32,
    pub institutional_grant_need_based: u32,
    pub institutional_grant_merit: u32,
    pub institutional_grant_other: u32,
    pub federal_loan_amount: u32,
    pub parent_plus_amount: u32,
    pub private_loan_amount: u32,
    pub federal_work_study_amount: u32,
    pub state_work_study_amount: u32,
    pub institutional_work_study_amount: u32,
    pub cost_of_attendance: u32,
    pub tuition_and_fees: u32,
    pub housing_charge: u32,
    pub meal_plan_charge: u32,
    pub total_grants: u32,
    pub total_loans: u32,
    pub total_work_study: u32,
    pub total_aid: u32,
    pub unmet_need: u32,
    pub need_based_applicant_type: String,
}

#[derive(Clone, Serialize)]
pub struct FinancialAidWideRow {
    pub student_id: String,
    pub erp_person_id: Option<String>,
    pub academic_year: String,
    pub term: Option<String>,
    pub term_code: Option<String>,
    pub fafsa_filed_flag: bool,
    pub applied_aid_flag: bool,
    pub need_index_regime: String,
    pub need_index_value: Option<i32>,
    pub pell_amount: u32,
    pub federal_seog_amount: u32,
    pub state_grant_need_based: u32,
    pub state_grant_non_need_based: u32,
    pub institutional_grant_need_based: u32,
    pub institutional_grant_merit: u32,
    pub institutional_grant_other: u32,
    pub federal_loan_amount: u32,
    pub parent_plus_amount: u32,
    pub private_loan_amount: u32,
    pub federal_work_study_amount: u32,
    pub state_work_study_amount: u32,
    pub institutional_work_study_amount: u32,
    pub cost_of_attendance: u32,
    pub tuition_and_fees: u32,
    pub housing_charge: u32,
    pub meal_plan_charge: u32,
    pub total_grants: u32,
    pub total_loans: u32,
    pub total_work_study: u32,
    pub total_aid: u32,
    pub unmet_need: u32,
    pub need_based_applicant_type: String,
    pub balance_due: i32,
    pub refund_amount: u32,
    pub aid_package_status: String,
}

#[derive(Clone, Serialize)]
pub struct LocalPostsecondaryHold {
    pub hold_id: String,
    pub student_id: String,
    pub term: String,
    pub hold_type: String,
    pub source_office: String,
    pub severity: String,
    pub active_flag: bool,
    pub blocks_registration: bool,
    pub blocks_transcript: bool,
    pub hold_reason_code: String,
    pub note_visibility: String,
    pub resolution_channel: String,
    pub placed_date: String,
    pub cleared_date: Option<String>,
}

#[derive(Clone, Serialize)]
pub struct LocalPostsecondaryHoldWide {
    pub hold_id: String,
    pub student_id: String,
    pub term: String,
    pub term_code: Option<String>,
    pub hold_type: String,
    pub source_office: String,
    pub severity: String,
    pub active_flag: bool,
    pub blocks_registration: bool,
    pub blocks_transcript: bool,
    pub hold_reason_code: String,
    pub note_visibility: String,
    pub resolution_channel: String,
    pub placed_date: String,
    pub cleared_date: Option<String>,
}
