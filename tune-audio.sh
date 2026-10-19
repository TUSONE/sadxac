#!/usr/bin/env bash
# WM8960 baseline: avoid analog overdrive that AEC cannot undo.
set -euo pipefail
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
[[ $# == 0 || ( $# == 1 && $1 == --output-only ) ]] || { echo 'Usage: bash tune-audio.sh [--output-only]' >&2; exit 1; }
sudo -v
voice_uid=$(id -u voice)
voice_pulse() {
    sudo -u voice env XDG_RUNTIME_DIR="/run/user/$voice_uid" \
        PULSE_SERVER="unix:/run/user/$voice_uid/pulse/native" pactl "$@"
}
output=alsa_output.platform-sound_i2s.stereo-fallback
input=alsa_input.platform-sound_i2s.stereo-fallback
# Physical output fixed at 60%; music and voice use separate stream levels.
for control in Headphone Speaker Playback 'Left Output Mixer PCM' 'Right Output Mixer PCM'; do
    sudo amixer -c 0 sget "$control" >/dev/null
done
if [[ $# == 0 ]]; then
    for control in Capture 'Left Input Boost Mixer LINPUT1' 'Right Input Boost Mixer RINPUT1'; do
        sudo amixer -c 0 sget "$control" >/dev/null
    done
    voice_pulse get-source-volume "$input" >/dev/null
fi
umask 077
backup="$HOME/slavik-backups/audio-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$backup"
sudo alsactl -f "$backup/alsa.state" store 0
voice_pulse --format=json list sinks > "$backup/sinks.json"
voice_pulse --format=json list sources > "$backup/sources.json"
for control in Headphone Speaker; do
    sudo amixer -c 0 sset "$control" 0dB >/dev/null
done
sudo amixer -c 0 sset Playback 100% >/dev/null
sudo amixer -c 0 sset 'Left Output Mixer PCM' on >/dev/null
sudo amixer -c 0 sset 'Right Output Mixer PCM' on >/dev/null
voice_pulse set-sink-volume "$output" 60%
voice_pulse set-sink-volume voice_aec_sink 100%
voice_pulse set-sink-mute "$output" 0
if [[ $# == 0 ]]; then
    # Reduce the diagnosed +30 dB PGA / +29 dB boost. Recalibrate DSP afterwards.
    voice_pulse set-source-volume "$input" 80%
    voice_pulse set-source-mute "$input" 0
    sudo amixer -c 0 sset 'Left Input Boost Mixer LINPUT1' 20dB >/dev/null
    sudo amixer -c 0 sset 'Right Input Boost Mixer RINPUT1' 20dB >/dev/null
fi
sudo alsactl store 0
printf 'Резервная копия: %s\n' "$backup"
printf '%s\n' 'Физический Pulse-выход: 60%; voice_aec_sink: 100%.'
if [[ $# == 0 ]]; then
    printf '%s\n' 'Вход: Pulse 80%, boost 20 dB. Остановите музыку и повторите --calibrate-mic.'
fi
