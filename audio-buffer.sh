#!/usr/bin/env bash
# Reversible runtime experiment, keeping 48 kHz and all mixer/AEC settings.
set -euo pipefail
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
mode=${1:-apply}
[[ $mode == apply || $mode == restore ]] || { echo 'Usage: bash audio-buffer.sh [apply|restore]' >&2; exit 1; }
sudo -v
voice_uid=$(id -u voice)
metadata() {
    sudo -n -u voice env XDG_RUNTIME_DIR="/run/user/$voice_uid" timeout 5 pw-metadata -n settings "$@"
}
current=$(metadata | python3 -c 'import re,sys; s=sys.stdin.read(); m=re.search(r"key:.*?clock.force-quantum.*?value:.*?([0-9]+)",s); assert m, "clock.force-quantum missing"; print(int(m[1]))')
umask 077
mkdir -p "$HOME/slavik-backups"
backup="$HOME/slavik-backups/force-quantum.previous"
if [[ $mode == apply ]]; then
    if [[ ! -f $backup ]]; then printf '%s\n' "$current" > "$backup"; fi
    metadata 0 clock.force-quantum 1024
    printf '%s\n' 'Runtime quantum: 1024 (21.3 ms при 48 kHz). Теперь повторите bash trace-audio.sh.'
else
    [[ -f $backup ]] || { echo 'Нет сохранённого значения.' >&2; exit 1; }
    previous=$(cat "$backup")
    [[ $previous =~ ^[0-9]+$ ]] || { echo 'Некорректный backup' >&2; exit 1; }
    [[ $current == 1024 ]] || { echo 'Quantum уже изменён другим действием; автоматическое восстановление отменено.' >&2; exit 1; }
    metadata 0 clock.force-quantum "$previous"
    rm -- "$backup"
    printf 'Восстановлен force-quantum=%s\n' "$previous"
fi
printf '%s\n' 'Изменение только до перезапуска PipeWire. Аудиослужбы не перезапускались.'
