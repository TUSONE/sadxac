#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ $(id -un) != tusone ]]; then
    echo 'Запускайте от tusone, без sudo.' >&2
    exit 1
fi
ENV_FILE="${SLAVIK_ENV_FILE:-$HOME/.config/slavik/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi
export SLAVIK_AUDIO_HELPER=/usr/local/libexec/slavik-audio
exec .venv/bin/python -u gemini_assistant_dialog.py "$@"
