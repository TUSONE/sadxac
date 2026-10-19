#!/usr/bin/env bash
# Compatibility entry point; applies the current physical 60% baseline.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec bash tune-audio.sh --output-only
