# Higher-Ed Administrative Dataset Schema

> **Revision notes (this version)**
>
> - Added new **Section 1: Student Demographics** (`student_demographics`) as the base student-grain table. Demographics that were scattered across term-level rows are now anchored here.
> - **Section 1 (Student Demographics)**: Added `citizenship_status` and `cohort_year` so the base student table better matches IPEDS/PDP demographic and cohort-tracking practice.
> - **Section 2 (SIS Enrollments)**: Kept the term-grain focus on genuinely time-varying fields. Removed `registration_system` from the row table (demoted to dataset-level metadata) and clarified that transfer-credit source detail should usually live in a separate articulation table if modeled.
> - **Section 3 (LMS Activity)**: Added optional deeper derived engagement fields (`content_interaction_count`, `forum_post_length_avg`, `assignment_review_count`, `lms_activity_regularity_index`). Clarified the `total_activity_time_seconds` duplication between raw (3A) and derived (3B) layers. Removed `lms_platform` from the row table (demoted to dataset-level metadata).
> - **Section 4 (Financial Aid)**: Added `fafsa_filed_flag`, `cost_of_attendance`, `unmet_need`, and `need_based_applicant_type`. Updated `need_index_regime` notes to reflect that SAI replaced EFC from AY2024-25 onward. Added realistic bounds to `pell_amount` notes and an FSEOG institutional-match note.
> - Added new **Section 5A: Faculty Course Assignments** (`faculty_courses`) to support instructor workload, section ownership, and student-faculty interaction analyses using IPEDS HR-aligned faculty attributes.
> - **Section 8 (Generator Knobs)**: Removed `PRETTY_JSON` (runtime formatting flag with no schema relevance). Updated `STOPOUT_RATE` and `MAJOR_CHANGE_RATE` notes with NCES-backed base rates. Added a **Recommended distributions** table with sourced parameters for all major fields and knobs.
> - Added a new **Governance and deployment notes** section to capture FERPA/privacy, synthetic-data usage, explainability, bias, and human-in-the-loop guardrails suggested by the supplemental project guidance.
> - **Sources**: Flagged the HSLS:09 citation as a K-12 study that is out of scope for a postsecondary schema unless the pipeline explicitly models the high-school-to-college transition. Added IPEDS Human Resources, FERPA, and supplemental learning-analytics references where relevant.

---

## Plain-language glossary for the labels used in this document

### What each classification means

- **Canonical field**: A field that is strongly supportable from a public standard, official reporting specification, or official platform documentation. These are the safest fields to treat as the "core" of the schema.
- **Derived analytics field**: A field that is usually **computed from other data**, rather than stored as a raw operational field in the source system. Example: a count of weekend logins derived from raw event timestamps.
- **Institution-specific extension**: A field that is realistic and often useful, but whose naming, coding, or business rule is usually **local to a university or vendor system** rather than supported as a public cross-institution standard.
- **Metadata / provenance**: A field that explains **where the data came from** or **which system produced it**. These are often useful, but they usually describe the dataset rather than the student or course itself.

### A simple evidence ladder

This is the practical evidence ladder used in the document:

1. **Strongest support**: the field or concept is directly documented in an authoritative source.
2. **Good support, but naming may be local**: the concept is documented, but the exact column name or code format is local.
3. **Useful analytic feature**: the field is valid, but it is computed from raw source data.
4. **Operational/local field**: the field may be common in real systems, but the exact definition is campus-specific.

## How to read each table

- **Field**: the proposed column name in the dataset.
- **Classification**: whether the field is a core standard-like field, a derived analytic field, a local extension, or metadata.
- **Suggested type**: the basic kind of data stored in the field, such as text, number, date, or boolean (`true/false`).
- **Recommended semantics / allowed values**: what the field is supposed to mean and what kinds of values should go into it.
- **Most specific supporting source(s)**: the most direct source used to justify the field. When possible, this points to a specific specification or official documentation page rather than a broad website homepage.
- **Notes**: extra interpretation, cautions, naming advice, or implementation guidance.

## How sources are used

### Highest-weight sources for field validation

1. **CEDS / NCES** for cross-institution postsecondary vocabulary and core concepts
   - CEDS data model guide: <https://ceds.ed.gov/pdf/ceds-data-model-guide.pdf>
   - CEDS connection report with postsecondary academic record and demographic elements: <https://ceds.ed.gov/connectReport.aspx?uid=30925>
   - NCES Administrative Data Collections: <https://nces.ed.gov/admindata/>

2. **National Student Clearinghouse Postsecondary Data Partnership (PDP)** for concrete file layouts and required student/course/aid fields
   - Cohort file detail records: <https://help.studentclearinghouse.org/pdp/knowledge-base/formatting-the-cohort-file-detail-records/>
   - Course file detail records: <https://help.studentclearinghouse.org/pdp/knowledge-base/formatting-the-course-file-detail-records/>
   - Financial aid file detail records: <https://help.studentclearinghouse.org/pdp/knowledge-base/formatting-the-financial-aid-file-detail-records/>
   - PDP data submission guide: <https://help.studentclearinghouse.org/pdp/wp-content/uploads/PDPDataSubmissionGuide.pdf>

3. **IPEDS / Federal Student Aid** for aid, charges, and cost-of-attendance concepts
   - IPEDS Student Financial Aid (SFA): <https://nces.ed.gov/ipeds/survey-components/12>
   - IPEDS Cost (CST): <https://nces.ed.gov/ipeds/survey-components/13>
   - Federal Student Aid, SAI explained: <https://studentaid.gov/sites/default/files/sai-explained.pdf>

4. **Canvas official documentation** for LMS-linked identifiers and submission/activity semantics
   - Logins: <https://developerdocs.instructure.com/services/canvas/resources/logins>
   - Enrollments: <https://developerdocs.instructure.com/services/canvas/resources/enrollments>
   - Submissions: <https://developerdocs.instructure.com/services/canvas/resources/submissions>
   - Object IDs and SIS IDs: <https://developerdocs.instructure.com/services/canvas/basics/file.object_ids>

5. **IPEDS Human Resources / staff reporting** for faculty-rank, tenure-status, and employment-status fields used in optional instructor tables
   - IPEDS Human Resources component: <https://nces.ed.gov/ipeds/survey-components/3>
   - IPEDS HR survey instructions: <https://surveys.nces.ed.gov/ipeds/public/survey-materials/instructions?instructionid=30043>

### Design-principle sources (useful, but not field dictionaries)

These support the idea of building linked, research-ready, and synthetic administrative datasets, but they should **not** be treated as canonical field dictionaries:

- What makes administrative data "research-ready"?: <https://pmc.ncbi.nlm.nih.gov/articles/PMC9052961/>
- An overview of synthetic administrative data for research: <https://pmc.ncbi.nlm.nih.gov/articles/PMC10464868/>
- ~~RTI / HSLS:09 follow-up announcement~~ (**removed**): The HSLS:09 is the High School Longitudinal Study of 2009, a K-12 study. It is out of scope for a postsecondary schema. Retain only if the schema explicitly models the K-12-to-college transition pipeline, and if so, document that scope explicitly.

### Governance and deployment sources

These are relevant when the synthetic dataset is used for modeling, demos, or decision support, but they are **not** field dictionaries:

- FERPA overview: <https://studentprivacy.ed.gov/ferpa>
- FERPA for colleges and universities: <https://studentprivacy.ed.gov/training/ferpa-101-colleges-universities>

### Supplemental analytical sources

These are useful for optional derived engagement features, but they are weaker than official platform field documentation:

- OLC, *Behind the Clicks: What LMS Data Is Really Telling Us About Online Learning*: <https://live-olc-v23.pantheonsite.io/olc-insights/2025/06/behind-the-clicks/>
- Systematic review of learning analytics and feedback practices in higher education: <https://www.sciencedirect.com/science/article/pii/S1747938X22000586>

### Supplemental example source

- DatabaseSample university database page: <https://www.databasesample.com/database/university-database>
  This is acceptable as a **toy/example schema source**, but it should **not** be treated as a standards authority for field naming or field semantics.

---

## 1) Student Demographics (`student_demographics`)

Recommended grain: **one row per student** (student-level, not term-level).

This table is the base identity and demographic record for each student. It deliberately separates static student attributes — those that do not change term-to-term — from the term-level SIS enrollment rows in Section 2. All other tables join to this one via `student_id`.

> **Why a separate demographics table?** In real university systems, demographics live in a person record, not repeated in every enrollment row. Keeping them separate avoids update anomalies (e.g., if a student updates their gender identity, only one row changes), reduces row width in the term-level table, and matches the structure of IPEDS, PDP, and SIS vendor systems such as Banner, Workday, and PeopleSoft.

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| student_id | Canonical field | string | Stable institutional student identifier | PDP Cohort file; CEDS | Primary key; links to all term-level tables. Must be stable across all terms. |
| birth_year | Canonical field | integer/null | Four-digit birth year | CEDS student birth date concepts | Prefer birth year over full date of birth for privacy; compute age at enrollment by subtracting from term start year. |
| gender | Canonical field | enum/string | IPEDS-aligned categories: `Male`, `Female`, `Another_gender`, `Unknown` | CEDS Gender concept; IPEDS Enrollment component | Do not collapse to a binary flag; IPEDS requires the four-category structure. |
| race_ethnicity | Canonical field | enum/string | IPEDS race/ethnicity categories: `White`, `Black_or_African_American`, `Hispanic_or_Latino`, `Asian`, `American_Indian_or_Alaska_Native`, `Native_Hawaiian_or_Pacific_Islander`, `Two_or_more_races`, `Nonresident_alien`, `Race_ethnicity_unknown` | CEDS race/ethnicity concepts; IPEDS Enrollment component | Essential for equity research and federally required demographic disaggregation. Store as IPEDS-aligned codes, not free text. IPEDS uses a two-part collection process (ethnicity first, then one-or-more race selections). |
| first_gen_flag | Canonical field | boolean/enum | `true`, `false`, or `unknown` | CEDS First Generation College Student; PDP Cohort file | Strongly supported; belongs here as a static student attribute, not a term-level field. |
| veteran_status | Canonical field | boolean/enum | `true`, `false`, or `unknown` | CEDS Veteran Status; IPEDS Student Financial Aid component | Federally tracked; affects aid eligibility and retention patterns. |
| disability_status | Canonical field | boolean/enum | `true`, `false`, or `unknown` | CEDS disability/special services indicator concepts | Important equity covariate; students with disabilities show meaningfully different stop-out and completion patterns. |
| age_at_entry | Canonical field | integer | Student age in whole years at initial postsecondary enrollment at this institution | CEDS postsecondary student age concepts; IPEDS Enrollment component | Critical covariate for trajectory modeling; nontraditional students (age 25 or older) behave very differently from traditional-age students. Derive from birth_year and initial_enrollment_term. |
| state_of_residence_at_entry | Institution-specific extension | string/null | Two-letter U.S. state code (e.g., `MA`) at time of first enrollment | Local admissions/residency records | Useful for modeling in-state vs. out-of-state tuition eligibility and regional mobility. |
| country_of_origin | Canonical field | string/null | ISO 3166-1 alpha-2 country code | CEDS; IPEDS international student and nonresident alien concepts | Needed for nonresident-alien cohort modeling. Set to `US` for domestic students. |
| citizenship_status | Canonical field | enum/string | `us_citizen`, `permanent_resident`, `nonresident_visa`, `other_or_unknown` | IPEDS Fall Enrollment component; IPEDS race/ethnicity reporting guidance | Useful because citizenship/nonresident status and race/ethnicity should not be collapsed into one muddy field. Map to local residency/admissions codes as needed. |
| initial_enrollment_term | Canonical field | string | First term of postsecondary enrollment at this institution, e.g. `2023FA` | PDP Cohort file; CEDS | Anchor for all longitudinal trajectory modeling. |
| cohort_year | Canonical field | string | Entry cohort year such as `2023-2024` or local cohort-year encoding | PDP Data Submission Guide; PDP cohort-year/term concepts | Useful for longitudinal retention/completion tracking. If `initial_enrollment_term` is already stored, this can be derived; keeping it explicitly is still reasonable. |
| admit_type | Canonical field | enum/string | `first_time`, `transfer`, `readmit`, `continuing`, `dual_enrollment` | PDP enrollment type concepts; CEDS postsecondary enrollment type | More stable as a student-level attribute than a term-level field. Matches PDP cohort enrollment type codes. |
| hs_unweighted_gpa | Canonical field | decimal/null | Unweighted high school GPA on a 4.0 scale | NCES Beginning Postsecondary Students study; PDP Cohort file admissions concepts | Static admission attribute; does not vary by term. |
| hs_weighted_gpa | Institution-specific extension | decimal/null | Weighted high school GPA; may exceed 4.0 due to AP/IB course weighting | Local admissions/transcript system | Document the weighting scale used. Not standardized across institutions. |

### Recommended strong-core student demographics fields

`student_id, birth_year, gender, race_ethnicity, first_gen_flag, veteran_status, disability_status, age_at_entry, country_of_origin, citizenship_status, initial_enrollment_term, cohort_year, admit_type, hs_unweighted_gpa`

---

## 2) SIS Enrollments (`sis_enrollments`)

Recommended grain: **one row per student per academic term**.

> **Note**: Static demographic and admission attributes (`race_ethnicity`, `gender`, `first_gen_flag`, `veteran_status`, `disability_status`, `hs_unweighted_gpa`, `hs_weighted_gpa`) have been moved to the `student_demographics` table (Section 1). The fields below are genuinely term-varying or are key linkage fields that belong at the enrollment grain.

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| student_id | Canonical field | string | Stable institutional student identifier | PDP Cohort file; PDP Course file | Use a stable internal identifier, not email. |
| academic_year | Canonical field | string | Academic year designator such as `2025-2026` or local equivalent | CEDS connection report; PDP Cohort/Course files | Prefer explicit academic year over embedding year in one opaque code. |
| term | Canonical field | enum/string | `Fall`, `Winter`, `Spring`, `Summer`, or institution-documented equivalent | CEDS Academic Term Designator; PDP term fields | Prefer explicit term over only `2025FA`-style packing. Suggested distribution: Fall ≈ 40%, Spring ≈ 35%, Summer ≈ 15%, Winter ≈ 10% (if offered). |
| term_code | Institution-specific extension | string | Local packed term code such as `2025FA` | Local encoding of academic_year + term | Fine as a local encoding, but not the canonical public concept. |
| student_level | Canonical field | enum/string | Undergraduate / graduate / professional / nondegree or mapped local equivalent | CEDS postsecondary student level concepts | Prefer this over vague `class_level`. Suggested distribution: Undergraduate ≈ 75%, Graduate ≈ 20%, Professional ≈ 3%, Nondegree ≈ 2%. |
| class_level | Institution-specific extension | enum/string | `Freshman`, `Sophomore`, `Junior`, `Senior`, etc. | Local standing derived from credits | Realistic, but often derived and not the most canonical cross-institution field. Distribution is monotone-decreasing due to attrition (Freshman > Sophomore > Junior > Senior). |
| academic_career | Institution-specific extension | enum/string | `UGRD`, `GRAD`, `PROF`, `NOND`, etc. | Local SIS/vendor code set | Common in Workday/PeopleSoft/Banner-style exports, but not a universal public code set. |
| college | Institution-specific extension | string | School/college label such as Engineering or Arts & Sciences | Local org hierarchy | Useful, but not supported as a canonical cross-institution student-term field. |
| major_cip_code | Canonical field | string | CIP code for primary major/program | CEDS major/program concepts; PDP/CIP-aligned reporting | Prefer code plus optional display label. Distribution is Zipfian: Business, Health Sciences, and Social Sciences dominate; many rare CIP codes appear only a few times. |
| major_label | Institution-specific extension | string | Local major/program name | Local display name for CIP-backed field | Good for readability, but CIP is stronger as canonical storage. |
| second_major_cip_code | Canonical field | string/null | CIP code for second major when applicable | IPEDS second-major reporting logic; program/CIP structure | Supported conceptually; will be null for most students. |
| residency_status | Institution-specific extension | enum/string | `in_state`, `out_of_state`, `international`, etc. | Local registrar/bursar rule set | Real and useful, but not strongly validated as a canonical public microdata field. |
| enrollment_type | Canonical field | enum/string | First-time / continuing / transfer / readmit, with institution-documented coding | PDP cohort/enrollment fields; CEDS postsecondary enrollment type | Better than ad-hoc `admit_type`. |
| first_time_flag | Canonical field | boolean | Whether the student is first-time in postsecondary entry/cohort sense | PDP Cohort file | Strongly supported. |
| transfer_credits_accepted | Canonical field | numeric | Accepted transfer credits | PDP Data Submission Guide / cohort concepts | Use accepted transfer credit, not a vague transfer-credit total. If you need source-institution or articulation detail, model that in a separate optional transfer-credit table rather than overloading the student-term row. |
| credits_attempted | Canonical field | numeric | Total attempted credits in term | CEDS academic record; PDP course/progress fields | Strongly supported. Distribution is bimodal: full-time peak near 15 credits, part-time peak near 6–9 credits. |
| credits_earned | Canonical field | numeric | Total earned/completed credits in term | CEDS academic record; PDP course/progress fields | Strongly supported. |
| term_gpa | Canonical field | decimal | GPA for the academic term/session | PDP cohort/course-related fields | Strongly supported. Distribution: left-skewed normal bounded [0, 4], μ ≈ 3.0–3.2, σ ≈ 0.5. Truncate at 0 and 4.0. |
| cumulative_gpa | Canonical field | decimal | Overall/cumulative GPA | PDP overall GPA concepts; CEDS cumulative academic record concepts | Strongly supported. Slightly higher mean and tighter spread than term_gpa due to averaging across terms. |
| credits_earned_cumulative | Canonical field | numeric | Cumulative earned credits | CEDS Credits Earned Cumulative | Better than vague `cumulative_credits`. |
| enrollment_status | Canonical field | enum/string | Institution-documented enrollment status for the term | CEDS Postsecondary Enrollment Status | Strongly supported. |
| full_time_flag | Canonical field | boolean | Derived from documented full-time threshold | CEDS/IPEDS/PDP intensity concepts | Document whether you use institutional or federal threshold logic. Suggested Bernoulli p ≈ 0.60 full-time nationally; varies significantly by institution type. |
| term_start_date | Canonical field | date | Academic term begin date | PDP cohort term begin date; academic calendar logic | Strongly supportable. |
| term_end_date | Canonical field | date | Academic term end date | PDP cohort term end date; academic calendar logic | Strongly supportable. |
| institutional_email | Institution-specific extension | string/null | Institutional email address | Local identity management | Useful for linkage and operations, not as the canonical student key. |

> **Removed field — `registration_system`**: Previously classified as Metadata / provenance. This field had a single repeated value per row (e.g., `Banner`). It belongs in dataset-level metadata or a data dictionary header, not in individual student-term rows. Document the source system once at the dataset level.

### Recommended strong-core SIS fields

`student_id, academic_year, term, student_level, major_cip_code, enrollment_type, first_time_flag, transfer_credits_accepted, credits_attempted, credits_earned, term_gpa, cumulative_gpa, credits_earned_cumulative, enrollment_status, full_time_flag, term_start_date, term_end_date`

---

## 3) LMS Activity (`lms_activity`)

Important modeling note: most LMS engagement variables are better represented as **derived analytics** rather than pretending they are raw canonical columns.

Recommended split:

- **Raw LMS facts**: one row per event, enrollment, or submission.
- **Derived LMS term analytics**: one row per student-term aggregation.

### 3A) Raw / linkage-friendly LMS fields

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| lms_user_id | Canonical field | string | LMS-local user identifier | Canvas Logins; Canvas Users | Prefer general `lms_user_id` instead of platform-specific `moodle_user_key`. |
| sis_user_id | Canonical field | string/null | SIS-linked user identifier in LMS | Canvas Logins; Canvas Enrollments; Canvas SIS IDs | Strong support for SIS/LMS linkage. |
| integration_id | Canonical field | string/null | Integration identifier used by trusted account or SIS integration | Canvas Logins; Canvas Enrollments; Canvas SIS IDs | Strong support as integration-layer identifier. |
| course_id | Canonical field | string | LMS course identifier | Canvas API object model | Raw canonical LMS concept. |
| sis_course_id | Canonical field | string/null | SIS-linked course identifier | Canvas SIS IDs | Strong support. |
| section_id | Canonical field | string | LMS section identifier | Canvas object model / enrollments | Raw canonical concept. |
| sis_section_id | Canonical field | string/null | SIS-linked section identifier | Canvas SIS IDs | Strong support. |
| event_timestamp | Canonical field | timestamp | Timestamp of activity/event/submission | Canvas event/submission resources | Strong support at raw-event level. |
| event_type | Canonical field | string | Auth, page view, submission, discussion, quiz, etc. | Canvas event/resource model | Better raw building block than only storing aggregates. |
| enrollment_state | Canonical field | string | Enrollment status/state | Canvas Enrollments | Useful raw enrollment field. |
| lms_enrollment_state_at_term_end | Canonical field | enum/string | Final enrollment state in the LMS course at term end: `active`, `completed`, `inactive`, `deleted` | Canvas Enrollments (`enrollment_state`) | Needed to distinguish students who officially completed a course from those administratively removed. Without this, it is impossible to separate genuine completion from administrative deletion. |
| last_activity_at | Canonical field | timestamp/null | Last activity timestamp available from platform | Canvas Enrollments | Strong official field support. |
| total_activity_time_seconds | Canonical field | integer | Per-enrollment total activity time in seconds as reported by the platform for this course enrollment | Canvas Enrollments / analytics-related docs | This is the raw per-enrollment value from Canvas, not an aggregated sum. Document the unit as seconds, not vague "minutes." See also the derived version in 3B. |
| submission_late | Canonical field | boolean/null | Whether submission is late | Canvas Submissions | Official field. |
| submission_missing | Canonical field | boolean/null | Whether submission is missing | Canvas Submissions | Official field. |
| submitted_at | Canonical field | timestamp/null | Submission timestamp | Canvas Submissions | Official field. |
| grade | Canonical field | string/null | Submission/course grade representation | Canvas Submissions | Official field. |

> **Removed field — `lms_platform`**: Previously classified as Metadata / provenance. Like `registration_system` in the SIS table, this field holds a single repeated value (e.g., `Canvas`) and belongs in dataset-level metadata, not in individual event rows.

### 3B) Derived term analytics fields

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| academic_year | Derived analytics field | string | Term-level aggregation key | Join from calendar/term mapping | Derived from institution term mapping. |
| term | Derived analytics field | string | Term-level aggregation key | Join from calendar/term mapping | Derived from institution term mapping. |
| distinct_course_count | Derived analytics field | integer | Count of distinct LMS courses in term | Derived from enrollments | Better than vague `course_shells`. |
| login_count | Derived analytics field | integer | Count of login/auth events | Derived from auth/logins/events | Distribution is power-law / Zipfian: highly engaged students dominate total logins. Model as log-normal or Pareto with floor at 1 for enrolled students. |
| active_days_count | Derived analytics field | integer | Count of unique activity days | Derived from timestamps | Beta-distributed scaled to term length; bounded by [0, term_length_days]. |
| page_views | Derived analytics field | integer | Count of page-view events | Derived from user activity/page views | Log-normal distribution; correlated with login_count. |
| submissions_count | Derived analytics field | integer | Count of submissions | Derived from submission facts | Bimodal: spike near 0 for withdrawn/disengaged students, near-normal for active students. |
| discussion_posts_count | Derived analytics field | integer | Count of discussion posts | Derived from discussion events/resources | Safe derivation. |
| quiz_attempts_count | Derived analytics field | integer | Count of quiz attempts | Derived from quiz submission facts | Safe derivation. |
| file_downloads_count | Derived analytics field | integer | Count of file-access/download events | Derived from file events/page views | Platform implementation may vary. |
| weekend_events_count | Derived analytics field | integer | Count of events on weekends | Derived from timestamp bucketing | Valid analytical feature. |
| late_night_events_count | Derived analytics field | integer | Count of events in defined late-night window | Derived from timestamp bucketing | Valid analytical feature. |
| total_activity_time_seconds | Derived analytics field | integer | Term-level sum of per-enrollment activity time in seconds across all courses | Canvas official activity-time concept | This is the **aggregated** counterpart of the raw per-enrollment `total_activity_time_seconds` in 3A. Keep units explicit. |
| first_activity_date | Derived analytics field | date | First observed activity in term | Derived from event timestamps | Valid analytical feature. |
| last_activity_date | Derived analytics field | date | Last observed activity in term | Derived from event timestamps or `last_activity_at` | Valid analytical feature. |
| missing_submission_count | Derived analytics field | integer | Count of missing submissions | Derived from `missing` flag in submissions | Better than pretending this is a raw canonical LMS column. |
| assignment_count_total | Derived analytics field | integer | Total number of graded assignments published in the student's LMS courses during the term | Derived from Canvas assignment/submission data | Without this denominator, `submissions_count` and `missing_submission_count` cannot be interpreted as rates. A student with 2 submissions out of 3 assignments is very different from 2 out of 20. Always derive this alongside submission counts. |
| content_interaction_count | Derived analytics field | integer | Count of interactions with core course content such as pages, files, videos, or modules | Derived from LMS page/file/module events; learning-analytics literature | Useful when you want a closer proxy for learner-content interaction than raw page views alone. Optional, because the exact event taxonomy varies by platform. |
| forum_post_length_avg | Derived analytics field | integer | Average discussion-post length in words or characters for the term | Derived from discussion post bodies | Useful when you want to distinguish a student who posts frequently but superficially from one who writes fewer, more substantive posts. Treat as optional because discussion exports are often incomplete. |
| assignment_review_count | Derived analytics field | integer | Count of times the student viewed assignment feedback, rubric results, or grading comments | Derived from LMS feedback-view or assignment-detail events | Useful for engagement-with-feedback analyses. Strong as an analytic feature, weaker as a universally available platform field. |
| lms_activity_regularity_index | Derived analytics field | decimal (0–1) | Index of how evenly activity is spread across the term, with higher values meaning more regular engagement | Derived from event timestamps | Useful for distinguishing steady work from last-minute cramming. Document the exact formula used (for example, inverse variance or entropy-based regularity). |
| current_grade_visible_flag | Institution-specific extension | boolean | Whether current grade is visible to student | Local course/platform configuration | Possible, but not a strong cross-platform canonical field. |

### Recommended strong-core LMS modeling approach

**Raw layer**:
`lms_user_id, sis_user_id, integration_id, course_id, sis_course_id, section_id, sis_section_id, event_timestamp, event_type, enrollment_state, lms_enrollment_state_at_term_end, last_activity_at, total_activity_time_seconds, submission_late, submission_missing, submitted_at, grade`

**Derived term analytics layer**:
`student_id_or_sis_user_id, academic_year, term, distinct_course_count, login_count, active_days_count, page_views, submissions_count, assignment_count_total, discussion_posts_count, quiz_attempts_count, weekend_events_count, late_night_events_count, total_activity_time_seconds, first_activity_date, last_activity_date, missing_submission_count, content_interaction_count, lms_activity_regularity_index`

---

## 4) Financial Aid (`financial_aid`)

Recommended grain: either **one row per student per aid year** or **one row per student per term with a clearly documented annual-to-term allocation rule**.

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| student_id | Canonical field | string | Stable institutional student identifier | PDP financial aid file linkage requirement | Use canonical student key. |
| erp_person_id | Institution-specific extension | string/null | ERP-local person identifier | Local ERP/finance system | Prefer generic `erp_person_id` over vendor-specific names. |
| academic_year | Canonical field | string | Aid/reporting year | PDP financial aid file; IPEDS annual collection logic | Strong support. |
| term | Institution-specific extension | string/null | Local term-level aid allocation | Local termization of annual aid | Valid if you explicitly model term-based packaging/disbursement. |
| fafsa_filed_flag | Canonical field | boolean | Whether the student submitted a FAFSA for this aid year | Federal Student Aid FAFSA filing records; PDP financial-aid concepts | Distinct from `applied_aid_flag`: a student can file FAFSA and receive no aid, or receive institutional-only aid without filing FAFSA. Suggested Bernoulli p ≈ 0.65–0.70 nationally. |
| applied_aid_flag | Canonical field | boolean | Whether an aid application or aid-processing record exists at this institution for the aid year | PDP financial aid concepts | More source-faithful than loosely equating with FAFSA receipt. |
| need_index_regime | Canonical field | enum/string | `EFC` for aid years through 2023-24; `SAI` for aid years 2024-25 onward | Federal Student Aid SAI documentation | **Important**: EFC and SAI are not interchangeable. The FAFSA Simplification Act replaced EFC with SAI effective AY2024-25. Any generator must gate on aid year; mixing regimes without year logic will produce anachronistic data. |
| need_index_value | Canonical field | integer/null | EFC or SAI value depending on regime and aid year | Federal Student Aid; IPEDS SAI-era reporting context | SAI may be negative (new FAFSA rules allow values below zero for high-need students). Store the value only alongside a clear regime/year label. Distribution is right-skewed with a spike at or near 0 for Pell-eligible students. |
| pell_amount | Canonical field | currency/integer | Pell Grant amount disbursed or packaged for the aid year | PDP financial aid file; IPEDS SFA | Strong support. For AY2025-26, maximum award is $7,395; average award nationally was approximately $5,300 for AY2023-24. About 32% of college students receive Pell. Model as zero-inflated: approximately 68% zero, remainder drawn from a truncated distribution within [$740, $7,395]. |
| federal_seog_amount | Canonical field | currency/integer | Federal SEOG amount | PDP financial aid file | Strong support. Note: institutions must match federal SEOG allocations with their own funds; the federal share may represent no more than 75% of the total award. Store the total award amount and document the federal/institutional split if needed. |
| state_grant_need_based | Canonical field | currency/integer | Need-based state grant amount | PDP financial aid file | Strong support when separate buckets are available. |
| state_grant_non_need_based | Canonical field | currency/integer | Non-need-based state grant amount | PDP financial aid file | Strong support when separate buckets are available. |
| institutional_grant_need_based | Canonical field | currency/integer | Need-based institutional grant amount | PDP financial aid file | Strong support when separate buckets are available. |
| institutional_grant_merit | Canonical field | currency/integer | Merit-based institutional grant amount | PDP financial aid file | Better than lumping all institutional grants together. |
| institutional_grant_other | Canonical field | currency/integer | Other institutional grant amount | PDP financial aid file | Useful when not cleanly need-based or merit. |
| federal_loan_amount | Canonical field | currency/integer | Federal loan amount | Aid concepts supported by federal/IPEDS/PDP contexts | If you split further, document subtypes. Approximately 43% of undergraduates take federal loans; modal annual borrowing among borrowers is $5,500–$7,500. |
| subsidized_loan_amount | Institution-specific extension | currency/integer | Subsidized federal loan amount | Local/detail-level packaging | Good if source system distinguishes it. |
| unsubsidized_loan_amount | Institution-specific extension | currency/integer | Unsubsidized federal loan amount | Local/detail-level packaging | Good if source system distinguishes it. |
| parent_plus_amount | Canonical field | currency/integer | Parent PLUS amount | PDP / federal aid categories | Strong support for undergraduate aid context. |
| grad_plus_amount | Institution-specific extension | currency/integer | Graduate PLUS amount | Local/detail-level packaging | Only relevant for graduate/professional scope. |
| private_loan_amount | Canonical field | currency/integer | Private or alternative loan amount | PDP "other loan" style concepts | Strong support. |
| federal_work_study_amount | Canonical field | currency/integer | Federal work-study amount | PDP financial aid file | Strong support. |
| state_work_study_amount | Canonical field | currency/integer | State work-study amount | PDP financial aid file | Supported in PDP-style decomposition. |
| institutional_work_study_amount | Canonical field | currency/integer | Institutional work-study amount | PDP financial aid file | Supported in PDP-style decomposition. |
| cost_of_attendance | Canonical field | currency/integer | Institutionally certified total cost of attendance for the aid year, including tuition, housing, meals, books, transportation, and personal expenses | IPEDS Cost component; federal COA definition | Distinct from `tuition_and_fees` alone. Use this as the anchor for need calculations and `unmet_need`. Typical ranges: public in-state ≈ $27k/yr total, private nonprofit ≈ $58k/yr total (IPEDS 2023-24). |
| tuition_and_fees | Canonical field | currency/integer | Tuition and required fees | IPEDS Cost; aid packaging context | Strong support. This is a component of `cost_of_attendance`, not a substitute for it. |
| housing_charge | Canonical field | currency/integer | Housing-related charge | IPEDS Cost / COA logic | Document whether this is housing-only or part of a bundled food-and-housing charge. |
| meal_plan_charge | Canonical field | currency/integer | Meal/food-related charge | IPEDS Cost / COA logic | Strong support. |
| total_grants | Derived analytics field | currency/integer | Sum of all grant and scholarship fields | Derived from aid components | Useful convenience field. |
| total_loans | Derived analytics field | currency/integer | Sum of all loan fields | Derived from loan components | Useful convenience field. |
| total_work_study | Derived analytics field | currency/integer | Sum of all work-study fields | Derived from work-study components | Useful convenience field. |
| total_aid | Derived analytics field | currency/integer | Sum of all aid components | Derived from raw aid components | Useful convenience field. |
| unmet_need | Derived analytics field | currency/integer | Cost of attendance minus total aid package; the financial gap remaining after all aid is applied | Derived from `cost_of_attendance` minus `total_aid` | Critical for financial hardship and stop-out modeling. A student with high unmet need is significantly more likely to stop out. Always define the formula explicitly. |
| need_based_applicant_type | Derived analytics field | enum/string | `pell_recipient`, `need_based_non_pell`, `non_need_based_only`, `no_aid` | Derived from Pell, grant, and total-aid components in this table | Useful compact segmentation for packaging analyses. Keep the derivation rule explicit so that "need-based" is not interpreted as a subjective label. |
| net_charges_before_payments | Derived analytics field | currency/integer | Charges minus grants, loans, and work-study as modeled | Derived business value | Define formula explicitly. |
| balance_due | Institution-specific extension | currency/integer | Bursar/account balance remaining | Local student accounts logic | Very real operational field, but not a canonical public aid element. |
| refund_amount | Institution-specific extension | currency/integer | Refund produced by aid/payment overage | Local bursar logic | Real, but not a public aid-standard element. |
| aid_package_status | Institution-specific extension | enum/string | `offered`, `accepted`, `disbursed`, etc. | Local financial-aid workflow | Workflow-specific rather than standard reporting field. |

### Recommended strong-core financial-aid fields

`student_id, academic_year, fafsa_filed_flag, applied_aid_flag, need_index_regime, need_index_value, pell_amount, federal_seog_amount, state_grant_need_based, state_grant_non_need_based, institutional_grant_need_based, institutional_grant_merit, institutional_grant_other, federal_loan_amount, parent_plus_amount, private_loan_amount, federal_work_study_amount, state_work_study_amount, institutional_work_study_amount, cost_of_attendance, tuition_and_fees, housing_charge, meal_plan_charge, unmet_need`

---

## 5) Registrar Course Enrollments (`registrar_course_enrollments`)

Recommended grain: **one row per student per course section per term**.

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| student_id | Canonical field | string | Stable student identifier | PDP Course file | Strong support. |
| academic_year | Canonical field | string | Academic year for course record | CEDS/PDP | Strong support. |
| term | Canonical field | string | Academic term | CEDS/PDP | Strong support. |
| course_section_id | Canonical field | string | Stable course-section identifier | PDP Course file section identifiers | Prefer generic name over vendor-specific `workday_course_section_id`. |
| course_prefix | Canonical field | string | Subject/prefix such as `CS` or `MATH` | PDP Course file | Strong support. |
| course_number | Canonical field | string | Course number such as `101` | PDP Course file | Strong support. |
| course_name | Canonical field | string | Course title/name | PDP Course file | Strong support. |
| course_cip_code | Canonical field | string | CIP code aligned to course/program | PDP course file / CIP-aligned concepts | Strong support. |
| course_level_type | Canonical field | enum/string | Lower division / upper division / graduate, or documented mapping | CEDS section/course-level concepts | Use documented mapping if derived from numbering bands. |
| delivery_method | Canonical field | enum/string | Face-to-face / online / hybrid or mapped PDP code | PDP Course file; IPEDS distance education concepts | Strong support. Suggested distribution post-2020: Face-to-face ≈ 55%, Online ≈ 35%, Hybrid ≈ 10%. |
| credits_attempted_course | Canonical field | numeric | Credits attempted for this course | PDP Course file | Strong support. Distribution is Zipfian: 3-credit courses dominate (~65%), followed by 4, 1, and 2 credits in decreasing frequency. |
| credits_earned_course | Canonical field | numeric | Credits earned for this course | PDP Course file | Strong support. |
| grade | Canonical field | string/null | Course grade/outcome code | PDP Course file | Strong support. Suggested distribution: A ≈ 40%, B ≈ 32%, C ≈ 17%, D ≈ 4%, F ≈ 3%, W ≈ 4%. |
| course_begin_date | Canonical field | date/null | Course begin date where available | PDP course/date concepts | Strong support if present in source. |
| course_end_date | Canonical field | date/null | Course end date where available | PDP course/date concepts | Strong support if present in source. |
| grading_basis | Institution-specific extension | enum/string | `graded`, `pass_fail`, `audit`, etc. | Local registrar rule set | Reasonable, but not strongly standardized across the sources used here. |
| letter_grade | Institution-specific extension | string/null | `A`, `B`, `C`, `W`, `I`, etc. | Local display-level grade representation | Often useful, but usually a local representation of the broader `grade` concept. |
| grade_points | Institution-specific extension | decimal/null | Numeric grade-point equivalent | Local grading scale | Fine locally; document scale if used. |
| repeat_flag | Institution-specific extension | boolean | Whether the course is a repeated attempt | Local registrar/course-history logic | Real but institution-specific. |
| withdrawal_flag | Derived analytics field | boolean | Derived from grade/status indicating withdrawal | Derived from grade/outcome codes | Prefer deriving from grade/status when possible. |
| enrollment_status | Institution-specific extension | enum/string | `enrolled`, `dropped`, `withdrawn`, `completed`, etc. | Local course-status rules | Realistic, but exact categories vary a lot. |
| instructor_id | Institution-specific extension | string | Instructor identifier | Local scheduling/HR link | Useful but not a canonical public student-course field. |
| meeting_pattern | Institution-specific extension | string/null | Schedule pattern | Local section scheduling | Operational scheduling field. |
| section_capacity | Institution-specific extension | integer | Capacity of section | Local section scheduling | Operational field, not usually canonical research microdata. |
| section_enrollment | Derived analytics field | integer | Section headcount | Derived from section enrollments | Better derived than stored in each student-course row. |
| grade_posted_date | Institution-specific extension | date/null | Date grade was posted | Local registrar workflow | Useful operationally, not canonical here. |

### Recommended strong-core registrar fields

`student_id, academic_year, term, course_section_id, course_prefix, course_number, course_name, course_cip_code, course_level_type, delivery_method, credits_attempted_course, credits_earned_course, grade, course_begin_date, course_end_date`

---

## 5A) Faculty Course Assignments (`faculty_courses`)

Recommended grain: **one row per instructor per course section per term**.

This table is optional, but it is worth adding when you want the synthetic dataset to support faculty workload analyses, instructor-course linkage, section staffing realism, or student-faculty interaction studies. The section-to-instructor link itself is usually local scheduling data; the instructor workforce attributes below are well aligned with IPEDS Human Resources reporting.

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| instructor_id | Canonical field | string | Stable institutional instructor identifier | Local scheduling/HR linkage; IPEDS HR context | Use a stable local key that can join course assignments to HR-style attributes. |
| academic_year | Canonical field | string | Academic year for the teaching assignment | CEDS / PDP academic-year concepts | Strong support. |
| term | Canonical field | string | Academic term for the teaching assignment | CEDS / PDP term concepts | Strong support. |
| course_section_id | Canonical field | string | Course section taught by the instructor | Local scheduling linked to `registrar_course_enrollments.course_section_id` | Canonical as a join field inside the local schema, even though the assignment itself is institution-managed. |
| faculty_rank | Canonical field | enum/string | `Professor`, `Associate_Professor`, `Assistant_Professor`, `Instructor`, `Lecturer`, `Adjunct`, or mapped local equivalent | IPEDS Human Resources component | Strongly supported for full-time instructional staff reporting. |
| tenure_status | Canonical field | enum/string | `Tenured`, `On_Tenure_Track`, `Not_on_Tenure_Track`, or mapped local equivalent | IPEDS Human Resources component | Strong support for faculty-workforce composition analyses. |
| employment_status | Canonical field | enum/string | `Full-time`, `Part-time` | IPEDS Human Resources component | Strong support. |
| teaching_load_credits | Derived analytics field | numeric | Total credit hours taught by the instructor in the term | Derived from assigned section credits | Useful convenience field for workload analysis. |
| home_department | Institution-specific extension | string | Instructor's home department or unit | Local HR / academic-organization hierarchy | Very realistic, but institution-specific. |
| primary_instruction_mode | Derived analytics field | enum/string | Dominant delivery mode of the instructor's assigned sections in the term | Derived from linked course-section delivery methods | Useful when modeling modality shifts or online-heavy teaching loads. |

### Recommended strong-core faculty-course fields

`instructor_id, academic_year, term, course_section_id, faculty_rank, tenure_status, employment_status`

---

## 6) Advising Holds (`advising_holds`)

Recommended grain: **one row per hold event**.

This table is realistic and useful, but it should be presented explicitly as an **institution-specific extension table**, not as a cross-institution canonical public schema.

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| hold_id | Institution-specific extension | string/uuid | Unique hold event identifier | Local event-table design | Good engineering practice, but not a public higher-ed standard field. |
| student_id | Institution-specific extension | string | Stable student identifier | Local linkage key | Fine and necessary for local modeling. |
| term | Institution-specific extension | string/null | Term to which hold is attached, if term-scoped | Local business rule | Some holds are not term-specific. |
| hold_type | Institution-specific extension | enum/string | Advising / financial / conduct / immunization / registrar, etc. | Local hold taxonomy | No strong cross-institution canonical public standard found in sources reviewed. |
| source_office | Institution-specific extension | string | Office placing or owning the hold | Local workflow | Operational field. |
| severity | Institution-specific extension | enum/string | `info`, `soft_block`, `hard_block`, etc. | Local workflow | Operational field. |
| active_flag | Derived analytics field | boolean | Whether hold is currently active | Derived from placement/clearance dates | Safe derived field. |
| blocks_registration | Institution-specific extension | boolean | Whether hold blocks registration | Local policy rule | Real but institution-specific. |
| blocks_transcript | Institution-specific extension | boolean | Whether hold blocks transcript release | Local policy rule | Real but institution-specific. |
| hold_reason_code | Institution-specific extension | string | Local reason code | Local code set | Operational field. |
| note_visibility | Institution-specific extension | enum/string | `internal`, `student_visible`, `restricted`, etc. | Local workflow/security rule | Operational field. |
| resolution_channel | Institution-specific extension | enum/string | `advisor`, `payment`, `manual_review`, etc. | Local workflow | Operational field. |
| placed_date | Institution-specific extension | date | Hold placement date | Local event date | Reasonable local field. |
| cleared_date | Institution-specific extension | date/null | Hold clearance date | Local event date | Reasonable local field. |

### Recommended treatment

Name this table something explicit such as:

`local_postsecondary_holds`

and document that it is a **local operational extension**, not a public standards-backed canonical table.

---

## 7) Identity Crosswalk (`identity_crosswalk`)

Recommended grain: **one row per identity mapping version**.

This is conceptually justified for integrated university data systems, but it is best described as an **integration / entity-resolution support table**, not a canonical institutional reporting table.

| Field | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| student_id | Canonical field | string | Canonical SIS student identifier | PDP / CEDS student identifier concepts | Good canonical anchor key. |
| lms_user_id | Institution-specific extension | string/null | LMS-local user identifier | Canvas Logins / Users | Good cross-system linkage field. |
| sis_user_id | Canonical field | string/null | SIS-linked LMS identifier | Canvas Logins / Enrollments / SIS IDs | Strongly supported as a linkage identifier. |
| integration_id | Canonical field | string/null | Integration identifier in LMS or trusted-account integration | Canvas Logins / Enrollments / SIS IDs | Strongly supported. |
| erp_person_id | Institution-specific extension | string/null | ERP-local identity | Local ERP system | Good linkage field, but local. |
| institutional_email | Institution-specific extension | string/null | Institutional email address | Local identity management | Useful as a secondary matching attribute, not a primary identifier. |
| effective_start_date | Institution-specific extension | date | Start date for mapping validity | Standard temporal entity-resolution practice | Good integration-layer field. |
| effective_end_date | Institution-specific extension | date/null | End date for mapping validity | Standard temporal entity-resolution practice | Good integration-layer field. |
| active_flag | Derived analytics field | boolean | Whether mapping is currently active | Derived from validity dates or state | Safe derived field. |
| source_system | Metadata / provenance | string | `SIS`, `LMS`, `ERP`, `IDM`, etc. | Data lineage / provenance | Valuable metadata. |
| match_rule | Institution-specific extension | string | `exact_id`, `email_match`, `manual_merge`, etc. | Entity-resolution lineage | Useful provenance field. |
| match_confidence | Institution-specific extension | decimal | Confidence score from 0 to 1 | Entity-resolution / MDM logic | Useful when modeling uncertain linkage. |

### Recommended treatment

Name this table something explicit such as:

`identity_crosswalk_integration`

and document that it is an **integration-layer support table**.

---

## 8) Generator knobs for fragmented synthetic data (`run.sh`)

This section describes the **generator knobs** used to create fragmented or partially messy synthetic data.

They are configuration controls that let the generator vary:

- dataset size and time span;
- student trajectory changes such as major changes or stop-outs;
- cross-system fragmentation such as missing LMS rows or missing financial-aid rows;
- operational friction such as holds;
- identity linkage problems such as mismatched crosswalk keys; and
- reproducibility / output controls such as seed and schema version.

### Important interpretation note

Some knobs correspond to **real-world higher-ed phenomena** that are supported by external sources.
Examples include stop-outs, major changes, transfer mobility, registration holds, incomplete financial-aid coverage, and linkage errors.

Other knobs are **engineering controls** such as the random seed or output directory.
These do not represent student behavior; they support reproducibility, documentation, and traceability, which are also important for research-ready and synthetic datasets.

### Current knobs

| Knob | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| `STUDENTS` | Generator scope knob | integer | Number of synthetic students to generate | Synthetic-data provenance / data-card style documentation; research-ready documentation principles | Engineering control for dataset scale. Not a higher-ed concept by itself. |
| `START_TERM` | Generator scope knob | string | Starting academic term for generation, e.g. `2023FA` | PDP Data Submission Guide (academic year and term structure) | Keep consistent with documented academic-year / term logic. |
| `TERMS` | Generator scope knob | integer | Number of sequential academic terms to simulate | PDP Data Submission Guide (term-based reporting) | Controls time horizon of trajectories. |
| `SEED` | Reproducibility / provenance knob | integer | Deterministic random seed for reproducible synthetic generation | Synthetic data provenance and reporting guidance | Strongly recommended so the same run can be reproduced. |
| `OUT_DIR` | Reproducibility / provenance knob | string/path | Output directory for generated files | Synthetic data provenance and metadata guidance | Operational control; helps organize versioned outputs. |
| `MAJOR_CHANGE_RATE` | Student-trajectory fragmentation knob | decimal (0–1) | Probability that a student's major/program changes after initial admission terms | NCES Beginning College Students Who Change Their Majors Within 3 Years | Approximately one-third of first-time students change their major at least once within three years (NCES). Per-term probability: ≈ 5–10%. Use modest rates for a realistic baseline; higher only for stress tests. |
| `STOPOUT_RATE` | Student-trajectory fragmentation knob | decimal (0–1) | Probability of a stop-out transition between terms | NCES stopout definition; NCES persistence/attainment tables | NCES reports a six-year stop-out rate of 25.1% for full-time students and 51.7% for part-time students (2019 cohort). Per-term probability: ≈ 4–8% for full-time, ≈ 10–15% for part-time. Model as a break in enrollment, not merely a course drop. |
| `LMS_MISSING_RATE` | Cross-system coverage-missingness knob | decimal (0–1) | Probability that expected LMS activity rows are missing for an enrolled student-term | Research-ready administrative data literature; official LMS data concepts | This should represent **data coverage missingness**, not "student did nothing." |
| `FIN_MISSING_RATE` | Cross-system coverage-missingness knob | decimal (0–1) | Probability that expected financial-aid rows are missing for an enrolled student-term or aid year | PDP Data Submission Guide (financial-aid file is optional); research-ready documentation principles | Best interpreted as system/extract missingness unless you separately model non-applicants. |
| `HOLD_RATE` | Operational-friction knob | decimal (0–1) | Probability that a student-term gets a hold record | Official university registrar / bursar hold documentation | Realistic local-operational fragmentation; exact hold taxonomy is institution-specific. |
| `CROSSWALK_MISMATCH_RATE` | Identity-linkage error knob | decimal (0–1) | Probability that cross-system keys are mismatched or swapped | Record-linkage literature on false matches and missed matches | Useful for stress-testing linkage robustness. Keep below 2% for realistic operational noise; 5%+ only for severe integration failure scenarios. |
| `SCHEMA_VERSION` | Output/schema knob | enum | `slim`, `wide`, or `both` | Research-ready documentation principles; synthetic-data provenance/versioning guidance | Useful when you want to compare a canonical core schema against a wider operational export. |

> **Removed knob — `PRETTY_JSON`**: This is a runtime output formatting flag (whether to pretty-print JSON). It has no bearing on data realism, fragmentation modeling, or schema design, and does not belong in a schema documentation table. Move to a separate runtime configuration file or CLI help text.

### Additional knobs that are worth adding

These are not mandatory, but they would make the generator better at producing **realistically fragmented administrative trajectories** instead of only row-level missingness.

| Knob | Classification | Suggested type | Recommended semantics / allowed values | Most specific supporting source(s) | Notes |
|---|---|---|---|---|---|
| `REENROLL_AFTER_STOPOUT_RATE` | Student-trajectory fragmentation knob | decimal (0–1) | Probability that a stopped-out student returns after one or more future terms | NCES stopout definition; NSC transfer/returning transfer reporting | Important because a stop-out is conceptually a **break followed by later enrollment**, not always a permanent exit. Suggested range: 30–50% returning within 2 years. |
| `WITHDRAWAL_RATE` | Student-trajectory fragmentation knob | decimal (0–1) | Probability of withdrawal distinct from stop-out, e.g. institutional or course-level withdrawal | NCES Six-year Withdrawal, Stopout, and Transfer Rates | Helps separate short-run course/term withdrawal from multi-term stop-out behavior. |
| `TRANSFER_OUT_RATE` | Student-mobility fragmentation knob | decimal (0–1) | Probability that a student leaves and reappears through transfer mobility rather than simple stop-out | NSC Transfer Enrollment and Pathways | Useful if the synthetic data is meant to stress cross-institution or re-entry logic. |
| `IDENTIFIER_MISSING_RATE` | Identity-linkage error knob | decimal (0–1) | Probability that a linkage key such as `sis_user_id`, institutional email, or ERP key is absent | Record-linkage literature on linkage errors caused by missing identifiers or data-quality problems | Different from a mismatch: this simulates **missing keys**, not wrong keys. |
| `HOLD_CLEARANCE_LAG_DAYS` | Operational-friction knob | integer | Typical number of days before a hold is cleared after being placed | Official university hold documentation | Makes holds behave more like real operational events instead of one-term static flags. |
| `AID_APPLICATION_RATE` | Financial-aid process knob | decimal (0–1) | Probability that a student has an aid application / aid-processing record at all | PDP financial-aid structure; aid-application / need-index concepts | Useful to separate **no aid process exists** from **aid data exist but the extract is missing**. |
| `TERM_CODE_STYLE` | Output/schema knob | enum | Local term-code style such as `YYYYFA`, split `academic_year + term`, or both | PDP Data Submission Guide; CEDS academic-year / term concepts | Helpful when testing interoperability across different term encodings. |
| `MISSINGNESS_PATTERN` | Advanced realism knob | enum | `mcar`, `mar_by_term`, `mar_by_student_group`, `system_outage_burst` | Research-ready documentation principles; synthetic-data transparency guidance | Useful when you want missingness to be conditional or bursty rather than uniform random. |

### Recommended interpretation of the most important fragmentation knobs

For practical use, the most important knobs are:

- `STOPOUT_RATE`: student disappears for a meaningful enrollment gap.
- `REENROLL_AFTER_STOPOUT_RATE`: student later returns.
- `TRANSFER_OUT_RATE`: student leaves through mobility rather than pure disappearance.
- `LMS_MISSING_RATE`: expected LMS rows are absent even though the student is enrolled.
- `FIN_MISSING_RATE`: expected aid rows are absent because of extraction or coverage gaps.
- `CROSSWALK_MISMATCH_RATE` and `IDENTIFIER_MISSING_RATE`: records cannot be cleanly linked across systems.

Together, these create the three main flavors of fragmentation:

1. **trajectory fragmentation** (the student path over time is interrupted);
2. **coverage fragmentation** (some systems are incomplete); and
3. **linkage fragmentation** (the systems exist but do not join cleanly).

### Recommended distributions for fields and knobs

This table maps each major field and knob to the real-world statistical distribution that best approximates it, sourced from NCES, IPEDS, and empirical research. Use these as generator targets, not as hard claims about any specific institution.

| Table | Field / Knob | Distribution | Key parameters and notes |
|---|---|---|---|
| SIS Enrollments | `term` | Categorical, non-uniform | Fall ≈ 40%, Spring ≈ 35%, Summer ≈ 15%, Winter ≈ 10% (if offered) |
| SIS Enrollments | `student_level` | Categorical, non-uniform | Undergraduate ≈ 75%, Graduate ≈ 20%, Professional ≈ 3%, Nondegree ≈ 2% |
| SIS Enrollments | `class_level` | Categorical, monotone-decreasing | Freshman > Sophomore > Junior > Senior due to attrition; roughly 28/24/24/24 before attrition adjustment |
| SIS Enrollments | `term_gpa` | Left-skewed normal, bounded [0, 4] | μ ≈ 3.0–3.2, σ ≈ 0.5; truncate at 0 and 4.0; left-skewed due to grade inflation |
| SIS Enrollments | `cumulative_gpa` | Left-skewed normal, tighter than term_gpa | μ ≈ 3.15–3.2; slightly higher mean and tighter spread than term_gpa due to averaging across terms |
| SIS Enrollments | `credits_attempted` | Bimodal | Full-time peak near 15 credits; part-time peak near 6–9 credits |
| SIS Enrollments | `full_time_flag` | Bernoulli | p ≈ 0.60 full-time nationally; varies sharply by institution type |
| SIS Enrollments | `major_cip_code` | Zipfian / heavy-tailed | Business, Health Sciences, and Social Sciences dominate; many rare CIP codes appear only a handful of times |
| Student Demographics | `first_gen_flag` | Bernoulli | p ≈ 0.33 first-generation nationally |
| Student Demographics | `race_ethnicity` | Categorical, non-uniform | White ≈ 54%, Hispanic ≈ 20%, Black ≈ 13%, Asian ≈ 7%, Two or more ≈ 4%, other/unknown ≈ 2% (IPEDS 2022-23 undergraduate enrollment) |
| Student Demographics | `gender` | Categorical | Female ≈ 57%, Male ≈ 42%, Another/Unknown ≈ 1% (IPEDS undergraduate) |
| Financial Aid | `fafsa_filed_flag` | Bernoulli | p ≈ 0.65–0.70 of enrolled students file FAFSA nationally |
| Financial Aid | `pell_amount` | Zero-inflated, right-skewed | ≈ 68% zero; remainder drawn from truncated distribution within [$740, $7,395]; average among recipients ≈ $5,300 |
| Financial Aid | `need_index_value` (SAI) | Right-skewed with spike near 0 | Many Pell-eligible students have SAI = 0 or negative; long right tail for high-income students |
| Financial Aid | `federal_loan_amount` | Zero-inflated, right-skewed | ≈ 57% zero (non-borrowers); modal annual amount among borrowers ≈ $5,500–$7,500 |
| Financial Aid | `cost_of_attendance` | Institutional constant per aid year, bimodal across institution types | Public in-state ≈ $27k/yr total; private nonprofit ≈ $58k/yr total; not student-level random |
| LMS Activity | `login_count` | Power-law / log-normal | Highly engaged students dominate; model as log-normal or Pareto with floor at 1 for enrolled students |
| LMS Activity | `page_views` | Log-normal | Similar shape to login_count; positively correlated |
| LMS Activity | `submissions_count` | Bimodal | Spike near 0 for withdrawn/disengaged students; near-normal for active students |
| LMS Activity | `active_days_count` | Beta-distributed scaled to term length | Bounded [0, term_length_days]; most students cluster in middle ranges |
| LMS Activity | `submission_late` / `submission_missing` | Bernoulli, correlated with GPA | Late rate ≈ 10–20%; missing rate ≈ 5–15% |
| Registrar | `grade` | Categorical, left-skewed | A ≈ 40%, B ≈ 32%, C ≈ 17%, D ≈ 4%, F ≈ 3%, W ≈ 4% |
| Registrar | `credits_attempted_course` | Discrete Zipfian | 3-credit courses ≈ 65%; then 4, 1, 2 credits in decreasing frequency |
| Registrar | `delivery_method` | Categorical, post-COVID distribution | Face-to-face ≈ 55%, Online ≈ 35%, Hybrid ≈ 10% |
| Generator Knobs | `STOPOUT_RATE` | Bernoulli per term | Per-term p ≈ 4–8% for full-time students; ≈ 10–15% for part-time (NCES 2019 cohort six-year rates) |
| Generator Knobs | `MAJOR_CHANGE_RATE` | Bernoulli per student per year | ≈ one-third of students change major at least once within three years (NCES); per-term p ≈ 5–10% |
| Generator Knobs | `REENROLL_AFTER_STOPOUT_RATE` | Bernoulli per stopped-out student | No single national rate; suggest 30–50% returning within 2 years |
| Generator Knobs | `CROSSWALK_MISMATCH_RATE` | Bernoulli, very low | Keep below 2% for realistic operational noise; 5%+ only for stress tests |
| Generator Knobs | `FAFSA_FILED_FLAG` (via `AID_APPLICATION_RATE`) | Bernoulli | p ≈ 0.65–0.70 nationally |

### Caution on distributions

Unless you have a target institution in mind, these knobs should usually be treated as **scenario controls** rather than claims about national base rates.

For example:

- a very low `CROSSWALK_MISMATCH_RATE` can represent ordinary operational keying/integration noise;
- a moderate `LMS_MISSING_RATE` may represent partial data extraction or system non-coverage;
- a higher `STOPOUT_RATE` or `WITHDRAWAL_RATE` may be appropriate only for stress tests, not for a "typical institution" baseline.

In other words, the knobs are best used to create **controlled realism** rather than fake precision.

---

## 9) Governance and deployment notes

These notes come from the practical AI/project guidance and are intentionally separated from the field dictionaries. They matter when the dataset is used for demos, modeling, or decision support.

### Privacy and access

- **FERPA matters even in postsecondary settings**. Once a student attends a postsecondary institution, FERPA rights belong to the student as the eligible student. Do not design the project around broad access to raw identifiable student microdata unless the institution has explicitly authorized it and the workflow is compliant.
- For general experimentation, prefer **synthetic data**, **de-identified data**, or **public aggregate data** such as IPEDS rather than assuming access to a giant raw institutional extract.

### Modeling guardrails

- Treat any predictive or recommendation workflow as a **decision-support system**, not as an autonomous replacement for advisors, registrars, or aid officers.
- Prefer models and features that are reasonably **explainable** to campus stakeholders. In practice, this means documenting why a student, course, or section is being flagged rather than returning only a score from an opaque model.
- Document potential **bias pathways** explicitly, especially when using demographic, financial, academic-history, or engagement features. Historical administrative data can encode prior inequities.
- Keep a clear distinction between **operational fields**, **derived analytics**, and **model outputs**. A schema document should not quietly smuggle model labels into raw administrative tables.

### Practical implementation note

If the immediate goal is to build a proof-of-concept AI/data-science system on top of this schema, the easiest domains are usually those with well-structured operational data already represented here, such as course registration, course progression, transfer-credit evaluation support, or aid-packaging analysis. Admissions or high-stakes intervention systems require more privacy review, stronger bias controls, and much tighter governance.
