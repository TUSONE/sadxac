#!/usr/bin/env bash
# Explicit hardware-level setup. Voice volume remains controllable on voice_aec_sink.
set -euo pipefail
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
sudo -v
mkdir -p "$HOME/slavik-backups"
sudo alsactl -f "$HOME/slavik-backups/wm8960-before-max-$(date +%Y%m%d-%H%M%S).state" store 0
for control in Headphone Speaker Playback; do
    sudo amixer -c 0 sset "$control" 100%
done
sudo amixer -c 0 sset 'Left Output Mixer PCM' on
sudo amixer -c 0 sset 'Right Output Mixer PCM' on
voice_uid=$(id -u voice)
voice_pulse() {
    sudo -u voice env XDG_RUNTIME_DIR="/run/user/$voice_uid" \
        PULSE_SERVER="unix:/run/user/$voice_uid/pulse/native" pactl "$@"
}
voice_pulse set-sink-volume alsa_output.platform-sound_i2s.stereo-fallback 100%
voice_pulse set-sink-mute alsa_output.platform-sound_i2s.stereo-fallback 0
voice_pulse set-sink-volume voice_aec_sink 100%
voice_pulse set-sink-mute voice_aec_sink 0
sudo alsactl store 0
printf '%s\n' 'Board output: 100%. Voice commands adjust the shared AEC sink; the speaker knob remains independent.'
