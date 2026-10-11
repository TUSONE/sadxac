#!/usr/bin/env bash
# Maximum clean output; do not amplify the WM8960 by +6 dB or reset voice volume.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec bash tune-audio.sh --output-only
