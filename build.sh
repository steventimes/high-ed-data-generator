#!/usr/bin/env bash
set -euo pipefail

cargo build --release

echo "Build complete: target/release/higher-ed-synth"