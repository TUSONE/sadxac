#!/usr/bin/env bash
# Reversible WM8960 mic PGA trial after measured ADC clipping. No output writes.
set -euo pipefail
umask 077
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
[[ $# == 1 && $1 == apply ]] || { echo 'Usage: bash mic-headroom.sh apply' >&2; exit 1; }
if pgrep -u "$(id -u)" -f '[g]emini_assistant_dialog.py' >/dev/null; then
    echo 'Остановите Славика/запись через Ctrl+C перед изменением входа.' >&2
    exit 1
fi
sudo -v
# Check the control before creating the backup or touching hardware.
sudo amixer -c 0 sget Capture
mkdir -p "$HOME/slavik-backups"
backup=$(mktemp -d "$HOME/slavik-backups/mic-headroom-XXXXXXXX")
sudo alsactl -f "$backup/alsa.state" store 0
# The supplied WM8960 report had Capture +30dB plus +20dB boost and ADC clips.
# +12dB PGA gives 18dB more headroom on that installation. Boost/ADC/output untouched.
sudo amixer -c 0 sset Capture 12dB
printf 'Резервная копия ALSA: %s/alsa.state\n' "$backup"
printf '%s\n' 'Capture: +12dB. Boost, ADC PCM, громкость колонки и Spotify не менялись.'
printf '%s\n' 'Пока не сохраняем новый уровень для перезагрузки. Повторите --aec-test на той же громкости/расстоянии.'
printf '%s\n' 'Для отмены этой пробы при исходных +30dB: sudo amixer -c 0 sset Capture 30dB'
