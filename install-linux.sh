#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $(id -un) == tusone ]] || { echo 'Install as tusone' >&2; exit 1; }
[[ $(pwd -P) == "$HOME/slavik" ]] || { echo 'Extract into ~/slavik' >&2; exit 1; }
packages=()
for name in python3 python3-venv python3-dev build-essential libffi-dev libssl-dev rustc cargo pkg-config pulseaudio-utils sudo libspeexdsp1 git; do
    packages+=("$name")
done
# ARMv7 may need source builds for pydantic-core/cryptography. Keep TLS/signatures enabled.
sudo -v
sudo apt-get update
sudo apt-get install -y "${packages[@]}"
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit('Python >=3.11 required')
PY
id voice >/dev/null
backup="$HOME/slavik-backups/infrastructure-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$backup"
for pair in '/usr/local/libexec/slavik-audio:slavik-audio' '/etc/sudoers.d/slavik-audio:slavik-sudoers' '/usr/local/libexec/music_duck.py:music_duck.py'; do
    source_path=${pair%:*}; name=${pair##*:}
    if sudo test -f "$source_path"; then sudo cat "$source_path" > "$backup/$name"; fi
done
sudo install -d -o root -g root -m 755 /usr/local/libexec
sudo install -o root -g root -m 755 slavik-audio /usr/local/libexec/slavik-audio
sudo install -o root -g root -m 644 music_duck.py /usr/local/libexec/music_duck.py
voice_home=$(getent passwd voice | cut -d: -f6)
voice_group=$(id -gn voice)
quantum_config="$voice_home/.config/pipewire/pipewire.conf.d/91-slavik-quantum.conf"
if sudo test -f "$quantum_config"; then sudo cat "$quantum_config" > "$backup/voice-quantum.conf"; fi
# Earlier root installs may own these directories. Repair only the three
# configuration directories, not the entire home or unrelated files.
sudo install -d -o voice -g "$voice_group" -m 700 -- \
    "$voice_home/.config" \
    "$voice_home/.config/pipewire" \
    "$voice_home/.config/pipewire/pipewire.conf.d"
sudo install -o voice -g "$voice_group" -m 644 config/voice-quantum.conf "$quantum_config"
rules=$(mktemp)
trap 'rm -f "$rules"' EXIT
cat > "$rules" <<'RULES'
tusone ALL=(voice) NOPASSWD: /usr/local/libexec/slavik-audio capture *, /usr/local/libexec/slavik-audio playback *, /usr/local/libexec/slavik-audio check, /usr/local/libexec/slavik-audio volume *, /usr/local/libexec/slavik-audio duck, /usr/local/libexec/slavik-audio prepare
RULES
sudo visudo -cf "$rules"
sudo install -o root -g root -m 440 "$rules" /etc/sudoers.d/slavik-audio
python3 -m venv .venv
export CARGO_BUILD_JOBS=1
export MAKEFLAGS=-j1
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
mkdir -p "$HOME/.config/slavik"
chmod 700 "$HOME/.config/slavik"
if [[ ! -e "$HOME/.config/slavik/.env" ]]; then
    printf '%s\n' '# GEMINI_API_KEY=enter-key-here' 'GEMINI_MODEL=gemini-3.1-flash-live-preview' 'GEMINI_VOICE=Kore' 'SLAVIK_FOLLOWUP_SECONDS=3' > "$HOME/.config/slavik/.env"
fi
chmod 600 "$HOME/.config/slavik/.env"
chmod 700 start-gemini-assistant.sh
sudo -n -H -u voice /usr/local/libexec/slavik-audio check
printf '%s\n' 'Installed. Put your key in ~/.config/slavik/.env, then run ./start-gemini-assistant.sh --audio-test and --mic-test.'
