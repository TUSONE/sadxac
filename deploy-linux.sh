#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(id -un) == tusone ]] || { echo 'Deploy as tusone' >&2; exit 1; }
staging=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ "$staging" == "$HOME"/.slavik-upload-* ]] || { echo 'Unexpected staging path' >&2; exit 1; }
if [[ -e "$HOME/slavik" ]]; then
    mkdir -p "$HOME/slavik-backups"
    backup="$HOME/slavik-backups/slavik-$(date +%Y%m%d-%H%M%S)-$$"
    mv -- "$HOME/slavik" "$backup"
    printf 'Previous installation: %s\n' "$backup"
fi
mv -- "$staging" "$HOME/slavik"
cd -- "$HOME/slavik"
bash install-linux.sh
