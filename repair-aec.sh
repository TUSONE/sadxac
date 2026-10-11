#!/usr/bin/env bash
# PipeWire 1.4.2 Pulse echo-cancel processes one 480-frame block per cycle.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
# Do not silently terminate the user's running dialog/recording.
if pgrep -u "$(id -u)" -f '[g]emini_assistant_dialog.py' >/dev/null; then
    echo 'Сначала остановите Славика/запись через Ctrl+C, затем повторите.' >&2
    exit 1
fi
sudo -v
voice_uid=$(id -u voice)
voice_env=(sudo -u voice env "XDG_RUNTIME_DIR=/run/user/$voice_uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$voice_uid/bus")
"${voice_env[@]}" systemctl --user is-active --quiet pipewire-pulse.service
bash audio-buffer.sh apply 480
printf '%s\n' 'Перезапуск Pulse/AEC: музыка кратко остановится.'
"${voice_env[@]}" systemctl --user restart pipewire-pulse.service
ready=0
for attempt in $(seq 1 25); do
    if sudo -n -H -u voice /usr/local/libexec/slavik-audio check >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep .2
done
if [[ $ready != 1 ]]; then
    echo 'AEC не появился после перезапуска. Пришлите --diagnose; не запускайте диалог.' >&2
    exit 1
fi
sudo systemctl restart slavik-spotify.service
printf '%s\n' 'AEC пересоздан, quantum=480. Выберите Slavik в Spotify и запустите Славика.'
printf '%s\n' 'Это runtime-исправление; после перезапуска самого PipeWire потребуется применить снова.'
