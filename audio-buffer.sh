#!/usr/bin/env bash
# Reversible runtime experiment, keeping 48 kHz and all mixer/AEC settings.
set -euo pipefail
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
mode=${1:-apply}
[[ $mode == apply || $mode == restore ]] || { echo 'Usage: bash audio-buffer.sh apply [960|1024] | restore' >&2; exit 1; }
quantum=${2:-960}
[[ $# -le 2 && ( $quantum == 960 || $quantum == 1024 ) ]] || { echo 'Expected quantum 960 or 1024' >&2; exit 1; }
sudo -v
voice_uid=$(id -u voice)
metadata() {
    sudo -n -u voice env XDG_RUNTIME_DIR="/run/user/$voice_uid" timeout 5 pw-metadata -n settings "$@"
}
current=$(metadata | python3 -c 'import re,sys; s=sys.stdin.read(); m=re.search(r"key:.*?clock.force-quantum.*?value:.*?([0-9]+)",s); assert m, "clock.force-quantum missing"; print(int(m[1]))')
umask 077
mkdir -p "$HOME/slavik-backups"
backup="$HOME/slavik-backups/force-quantum.previous"
applied="$HOME/slavik-backups/force-quantum.applied"
if [[ $mode == apply ]]; then
    if [[ ! -f $backup ]]; then printf '%s\n' "$current" > "$backup"; fi
    metadata 0 clock.force-quantum "$quantum"
    printf '%s\n' "$quantum" > "$applied"
    printf 'Runtime quantum: %s при 48 kHz (960 = 20 ms, 1024 = 21.3 ms). Повторите bash trace-audio.sh.\n' "$quantum"
else
    [[ -f $backup ]] || { echo 'Нет сохранённого значения.' >&2; exit 1; }
    previous=$(cat "$backup")
    [[ $previous =~ ^[0-9]+$ ]] || { echo 'Некорректный backup' >&2; exit 1; }
    expected=1024  # earlier script did not create an applied-value file
    if [[ -f $applied ]]; then expected=$(cat "$applied"); fi
    [[ $current == "$expected" ]] || { echo 'Quantum уже изменён другим действием; автоматическое восстановление отменено.' >&2; exit 1; }
    metadata 0 clock.force-quantum "$previous"
    rm -f -- "$backup" "$applied"
    printf 'Восстановлен force-quantum=%s\n' "$previous"
fi
printf '%s\n' 'Изменение только до перезапуска PipeWire. Аудиослужбы не перезапускались.'
