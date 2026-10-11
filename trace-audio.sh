#!/usr/bin/env bash
# Observe underruns/graph changes; do not restart or reconfigure audio services.
set -euo pipefail
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
sudo -v
voice_uid=$(id -u voice)
voice_env=(sudo -n -u voice env "XDG_RUNTIME_DIR=/run/user/$voice_uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$voice_uid/bus")
umask 077
mkdir -p "$HOME/slavik-diagnostics"
report=$(mktemp -d "$HOME/slavik-diagnostics/runtime-XXXXXX")
started=$(date --iso-8601=seconds)
printf '20 секунд наблюдения. Оставьте музыку включённой; в другом окне остановите/запустите Славика при слышимом обрыве.\nКаталог: %s\n' "$report"
ps -eLo pid,tid,cls,rtprio,pri,ni,psr,pcpu,comm > "$report/scheduling-before.txt"
"${voice_env[@]}" timeout 5 pw-metadata -n settings > "$report/settings.txt" 2>&1 || true
"${voice_env[@]}" timeout 25 pw-top -b -n 20 > "$report/pw-top.txt" 2>&1 &
top_pid=$!
# Repeated sampling ties runnable load to observed graph errors, not just averages.
for sample in $(seq 1 20); do
    date --iso-8601=ns
    cat /proc/loadavg
    ps -eo pid,comm,pcpu,stat --sort=-pcpu | head -n 12 || true
    sleep 1
done > "$report/load.txt"
wait "$top_pid" || printf 'pw-top не завершился успешно; смотрите pw-top.txt.\n'
"${voice_env[@]}" timeout 8 journalctl --user -u pipewire -u pipewire-pulse -u wireplumber \
    --since "$started" -n 200 --no-pager > "$report/audio-journal.txt" 2>&1 || true
ps -eLo pid,tid,cls,rtprio,pri,ni,psr,pcpu,comm > "$report/scheduling-after.txt"
python3 - "$report" <<'PY'
import pathlib,sys
folder=pathlib.Path(sys.argv[1])
with (folder/'report.txt').open('w') as output:
    for path in sorted(folder.glob('*.txt')):
        if path.name != 'report.txt':
            output.write('\n===== '+path.name+' =====\n')
            output.write(path.read_text(errors='replace'))
print('Пришлите файл: '+str(folder/'report.txt'))
PY
