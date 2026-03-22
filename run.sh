#!/usr/bin/env bash
set -euo pipefail

# Shared parameters
STUDENTS="${STUDENTS:-500}"                        # Number of students to generate
START_TERM="${START_TERM:-2023FA}"                 # Starting academic term in YYYYFA/YYYYSP/YYYYSU format
TERMS="${TERMS:-6}"                                # Number of sequential terms
SEED="${SEED:-12345}"                              # Deterministic RNG seed for fragmented run
OUT_DIR="${OUT_DIR:-./out}"                        # Fragmented output directory

# Fragmented run controls (per eligible term/transition)
MAJOR_CHANGE_RATE="${MAJOR_CHANGE_RATE:-0.06}"      # Probability of major changes after admission terms
STOPOUT_RATE="${STOPOUT_RATE:-0.05}"                # Probability of stop-out between terms
REENROLL_AFTER_STOPOUT_RATE="${REENROLL_AFTER_STOPOUT_RATE:-0.40}" # Re-enrollment probability after stopout
WITHDRAWAL_RATE="${WITHDRAWAL_RATE:-0.03}"          # Withdrawal probability (distinct from stopout)
TRANSFER_OUT_RATE="${TRANSFER_OUT_RATE:-0.015}"     # Transfer-out probability

# System/data missingness (for enrolled students)
LMS_MISSING_RATE="${LMS_MISSING_RATE:-0.08}"        # Probability of missing expected LMS rows for enrolled students
FIN_MISSING_RATE="${FIN_MISSING_RATE:-0.20}"        # Probability of missing expected financial aid rows for enrolled students

# Administrative friction / identity issues
HOLD_RATE="${HOLD_RATE:-0.10}"                      # Probability of advising holds per student/term
CROSSWALK_MISMATCH_RATE="${CROSSWALK_MISMATCH_RATE:-0.01}" # Probability of identity crosswalk mismatch/swapped keys
IDENTIFIER_MISSING_RATE="${IDENTIFIER_MISSING_RATE:-0.01}"  # Probability linkage identifiers are null
HOLD_CLEARANCE_LAG_DAYS="${HOLD_CLEARANCE_LAG_DAYS:-14}"    # Typical days before hold clears
AID_APPLICATION_RATE="${AID_APPLICATION_RATE:-0.70}"        # Aid process/application probability
TERM_CODE_STYLE="${TERM_CODE_STYLE:-both}"                  # packed | split | both
MISSINGNESS_PATTERN="${MISSINGNESS_PATTERN:-mcar}"          # mcar | mar_by_term | mar_by_student_group | system_outage_burst

# Output formatting
PRETTY_JSON="${PRETTY_JSON:-true}"                  # Runtime output formatting flag (not a schema knob).
SCHEMA_VERSION="${SCHEMA_VERSION:-both}"            # slim | wide | both

# Baseline generation controls
GENERATE_BASELINE="${GENERATE_BASELINE:-true}"               # true | false
BASELINE_SEED="${BASELINE_SEED:-${SEED}}"                    # Deterministic RNG seed for baseline run
BASELINE_OUT_DIR="${BASELINE_OUT_DIR:-${OUT_DIR%/}_baseline}"
BASELINE_MAJOR_CHANGE_RATE="${BASELINE_MAJOR_CHANGE_RATE:-0.0}"
BASELINE_STOPOUT_RATE="${BASELINE_STOPOUT_RATE:-0.0}"
BASELINE_REENROLL_AFTER_STOPOUT_RATE="${BASELINE_REENROLL_AFTER_STOPOUT_RATE:-1.0}"
BASELINE_WITHDRAWAL_RATE="${BASELINE_WITHDRAWAL_RATE:-0.0}"
BASELINE_TRANSFER_OUT_RATE="${BASELINE_TRANSFER_OUT_RATE:-0.0}"
BASELINE_LMS_MISSING_RATE="${BASELINE_LMS_MISSING_RATE:-0.0}"
BASELINE_FIN_MISSING_RATE="${BASELINE_FIN_MISSING_RATE:-0.0}"
BASELINE_HOLD_RATE="${BASELINE_HOLD_RATE:-0.0}"
BASELINE_CROSSWALK_MISMATCH_RATE="${BASELINE_CROSSWALK_MISMATCH_RATE:-0.0}"
BASELINE_IDENTIFIER_MISSING_RATE="${BASELINE_IDENTIFIER_MISSING_RATE:-0.0}"
BASELINE_HOLD_CLEARANCE_LAG_DAYS="${BASELINE_HOLD_CLEARANCE_LAG_DAYS:-14}"
BASELINE_AID_APPLICATION_RATE="${BASELINE_AID_APPLICATION_RATE:-1.0}"
BASELINE_MISSINGNESS_PATTERN="${BASELINE_MISSINGNESS_PATTERN:-mcar}"

if [[ "${PRETTY_JSON}" != "true" && "${PRETTY_JSON}" != "false" ]]; then
  echo "PRETTY_JSON must be true or false (got: ${PRETTY_JSON})" >&2
  exit 1
fi

if [[ "${GENERATE_BASELINE}" != "true" && "${GENERATE_BASELINE}" != "false" ]]; then
  echo "GENERATE_BASELINE must be true or false (got: ${GENERATE_BASELINE})" >&2
  exit 1
fi

COMMON_ARGS=(
  --students "${STUDENTS}"
  --start-term "${START_TERM}"
  --terms "${TERMS}"
  --term-code-style "${TERM_CODE_STYLE}"
  --schema-version "${SCHEMA_VERSION}"
)

FRAGMENTED_CMD=(
  cargo run --release --
  "${COMMON_ARGS[@]}"
  --seed "${SEED}"
  --out-dir "${OUT_DIR}"
  --major-change-rate "${MAJOR_CHANGE_RATE}"
  --stopout-rate "${STOPOUT_RATE}"
  --reenroll-after-stopout-rate "${REENROLL_AFTER_STOPOUT_RATE}"
  --withdrawal-rate "${WITHDRAWAL_RATE}"
  --transfer-out-rate "${TRANSFER_OUT_RATE}"
  --lms-missing-rate "${LMS_MISSING_RATE}"
  --fin-missing-rate "${FIN_MISSING_RATE}"
  --hold-rate "${HOLD_RATE}"
  --crosswalk-mismatch-rate "${CROSSWALK_MISMATCH_RATE}"
  --identifier-missing-rate "${IDENTIFIER_MISSING_RATE}"
  --hold-clearance-lag-days "${HOLD_CLEARANCE_LAG_DAYS}"
  --aid-application-rate "${AID_APPLICATION_RATE}"
  --missingness-pattern "${MISSINGNESS_PATTERN}"
)

if [[ "${PRETTY_JSON}" == "true" ]]; then
  FRAGMENTED_CMD+=(--pretty-json)
fi

echo "Generating fragmented dataset -> ${OUT_DIR}"
"${FRAGMENTED_CMD[@]}"

if [[ "${GENERATE_BASELINE}" == "true" ]]; then
  if [[ "${BASELINE_OUT_DIR%/}" == "${OUT_DIR%/}" ]]; then
    echo "BASELINE_OUT_DIR must be different from OUT_DIR." >&2
    exit 1
  fi

  BASELINE_CMD=(
    cargo run --release --
    "${COMMON_ARGS[@]}"
    --seed "${BASELINE_SEED}"
    --out-dir "${BASELINE_OUT_DIR}"
    --major-change-rate "${BASELINE_MAJOR_CHANGE_RATE}"
    --stopout-rate "${BASELINE_STOPOUT_RATE}"
    --reenroll-after-stopout-rate "${BASELINE_REENROLL_AFTER_STOPOUT_RATE}"
    --withdrawal-rate "${BASELINE_WITHDRAWAL_RATE}"
    --transfer-out-rate "${BASELINE_TRANSFER_OUT_RATE}"
    --lms-missing-rate "${BASELINE_LMS_MISSING_RATE}"
    --fin-missing-rate "${BASELINE_FIN_MISSING_RATE}"
    --hold-rate "${BASELINE_HOLD_RATE}"
    --crosswalk-mismatch-rate "${BASELINE_CROSSWALK_MISMATCH_RATE}"
    --identifier-missing-rate "${BASELINE_IDENTIFIER_MISSING_RATE}"
    --hold-clearance-lag-days "${BASELINE_HOLD_CLEARANCE_LAG_DAYS}"
    --aid-application-rate "${BASELINE_AID_APPLICATION_RATE}"
    --missingness-pattern "${BASELINE_MISSINGNESS_PATTERN}"
  )

  if [[ "${PRETTY_JSON}" == "true" ]]; then
    BASELINE_CMD+=(--pretty-json)
  fi

  echo "Generating clean baseline dataset -> ${BASELINE_OUT_DIR}"
  "${BASELINE_CMD[@]}"
fi
