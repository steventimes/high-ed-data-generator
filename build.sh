#!/usr/bin/env bash
set -euo pipefail

cargo build --release -p fragmentation-cli

echo "Build complete: target/release/fragmentation-cli"
