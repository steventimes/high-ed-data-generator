#!/usr/bin/env bash
set -euo pipefail

# Parameters
STUDENTS="${STUDENTS:-500}"                 # Number of students to generate
START_TERM="${START_TERM:-2023FA}"          # Starting academic term in YYYYFA/YYYYSP/YYYYSU format
TERMS="${TERMS:-6}"                         # Number of sequential terms
SEED="${SEED:-12345}"                       # Deterministic RNG seed for reproducible output
OUT_DIR="${OUT_DIR:-./out}"                 # Output directory

# Student dynamics (per eligible term/transition)
MAJOR_CHANGE_RATE="${MAJOR_CHANGE_RATE:-0.03}"      # Probability of major changes after admission terms
STOPOUT_RATE="${STOPOUT_RATE:-0.015}"               # Probability of stop-out between terms

# System/data missingness (for enrolled students)
LMS_MISSING_RATE="${LMS_MISSING_RATE:-0.06}"        # Probability of missing Moodle activity rows for enrolled students
FIN_MISSING_RATE="${FIN_MISSING_RATE:-0.25}"        # Probability of missing financial aid rows for enrolled students

# Administrative friction / identity issues
HOLD_RATE="${HOLD_RATE:-0.08}"                      # Probability of advising holds per student/term
CROSSWALK_MISMATCH_RATE="${CROSSWALK_MISMATCH_RATE:-0.001}" # Probability of identity crosswalk mismatch/swapped keys

# Output formatting
PRETTY_JSON="${PRETTY_JSON:-true}"                  # Whether to pretty print JSON output: true or false.


CMD=(
  cargo run --release --
  --students "${STUDENTS}"
  --start-term "${START_TERM}"
  --terms "${TERMS}"
  --seed "${SEED}"
  --out-dir "${OUT_DIR}"
  --major-change-rate "${MAJOR_CHANGE_RATE}"
  --stopout-rate "${STOPOUT_RATE}"
  --lms-missing-rate "${LMS_MISSING_RATE}"
  --fin-missing-rate "${FIN_MISSING_RATE}"
  --hold-rate "${HOLD_RATE}"
  --crosswalk-mismatch-rate "${CROSSWALK_MISMATCH_RATE}"
)

if [[ "${PRETTY_JSON}" == "true" ]]; then
  CMD+=(--pretty-json)
fi

"${CMD[@]}"