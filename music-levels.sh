#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
mode=${1:-show}
[[ $mode == show || $mode == reset ]] || { echo 'Usage: bash music-levels.sh [show|reset]' >&2; exit 1; }
if [[ $mode == reset ]] && pgrep -u "$(id -u)" -f '[g]emini_assistant_dialog.py' >/dev/null; then
    echo 'Сначала остановите Славика через Ctrl+C. Музыку оставьте включённой.' >&2
    exit 1
fi
sudo -v
umask 077
python3 - "$mode" <<'PY'
import json, os, pwd, subprocess, sys, tempfile
from pathlib import Path
from music_duck import is_music
uid = pwd.getpwnam('voice').pw_uid
base = ['sudo', '-n', '-u', 'voice', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
        f'PULSE_SERVER=unix:/run/user/{uid}/pulse/native', 'pactl']
def pulse(*args):
    return subprocess.run(base + list(args), capture_output=True, text=True, check=True, timeout=5).stdout
def music():
    return [s for s in json.loads(pulse('--format=json', 'list', 'sink-inputs')) if is_music(s)]
def show(streams):
    for s in streams:
        levels = ', '.join(f"{ch}: {v.get('value_percent', str(round(v['value'] / 655.36)) + '%')} ({v.get('db', '?')} dB)" for ch,v in s['volume'].items())
        print(f"Spotify Pulse-поток {s['index']}: {levels}; mute={s.get('mute')}")
streams = music()
show(streams)
if not streams:
    raise SystemExit('Нет активного потока Spotify. Включите музыку на Slavik и повторите.')
if sys.argv[1] == 'reset':
    directory = Path.home() / 'slavik-backups'
    directory.mkdir(exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='music-levels-', suffix='.json', dir=directory)
    with os.fdopen(fd, 'w') as f:
        json.dump([{'index': s['index'], 'volume': s['volume'], 'mute': s.get('mute')} for s in streams], f)
    for s in streams:
        pulse('set-sink-input-volume', str(s['index']), '100%')
    print('Уровни музыкальных потоков возвращены к 100% (0 dB). Backup: ' + name)
    show(music())
for sink in json.loads(pulse('--format=json', 'list', 'sinks')):
    if sink['name'] in ('voice_aec_sink', 'alsa_output.platform-sound_i2s.stereo-fallback'):
        print('Общий выход (не изменён): ' + sink['name'] + ' ' + json.dumps(sink['volume'], ensure_ascii=False))
PY
