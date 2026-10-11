#!/usr/bin/env bash
# Separate, deliberate installation: keeps Gemini and PipeWire configuration unchanged.
set -euo pipefail
umask 077
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
case $(dpkg --print-architecture) in armhf|arm64|amd64|riscv64) ;; *) echo 'Unsupported architecture' >&2; exit 1;; esac
voice_uid=$(id -u voice)
sudo -v
sudo -n -H -u voice /usr/local/libexec/slavik-audio check
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg avahi-daemon
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if ! command -v librespot >/dev/null; then
    # Upstream repository, TLS-verified public key and apt signed Release metadata.
    curl --proto '=https' --tlsv1.2 -fsSL https://dtcooper.github.io/raspotify/key.asc -o "$work/key.asc"
    gpg --show-keys "$work/key.asc" >/dev/null
    printf '%s\n' 'deb [signed-by=/usr/share/keyrings/slavik-raspotify.asc] https://dtcooper.github.io/raspotify raspotify main' > "$work/repo.list"
    if [[ -f /etc/apt/sources.list.d/raspotify.list ]]; then
        echo 'Using existing Raspotify repository configuration without changing it.'
    else
        if [[ -f /etc/apt/sources.list.d/slavik-raspotify.list ]] &&
           ! cmp -s "$work/repo.list" /etc/apt/sources.list.d/slavik-raspotify.list; then
            echo 'Existing slavik-raspotify.list differs; preserve it and resolve manually.' >&2
            exit 1
        fi
        sudo install -m 644 "$work/key.asc" /usr/share/keyrings/slavik-raspotify.asc
        sudo install -m 644 "$work/repo.list" /etc/apt/sources.list.d/slavik-raspotify.list
    fi
    sudo apt-get update
    sudo apt-get install -y raspotify
fi
[[ -x /usr/bin/librespot ]] || { echo 'Expected /usr/bin/librespot' >&2; exit 1; }
# --help describes options; --backend '?' lists actually compiled backends.
/usr/bin/librespot --backend '?' > "$work/backends.txt"
if ! grep -Eq '^[[:space:]]*-[[:space:]]*(pulseaudio|pipe)([[:space:]]|$)' "$work/backends.txt"; then
    cat "$work/backends.txt"
    echo 'Neither pulseaudio nor pipe backend available. No service has been changed.' >&2
    exit 1
fi
[[ -x /usr/bin/pacat ]] || { echo 'Install pulseaudio-utils first' >&2; exit 1; }
[[ -f "$script_dir/slavik-spotify-player" ]] || { echo 'Missing slavik-spotify-player; update the checkout' >&2; exit 1; }
cat > "$work/slavik-spotify.service" <<UNIT
[Unit]
Description=Slavik Spotify Connect via voice AEC
After=network-online.target avahi-daemon.service
Wants=network-online.target avahi-daemon.service
[Service]
User=voice
Group=$(id -gn voice)
Environment=XDG_RUNTIME_DIR=/run/user/$voice_uid
Environment=PULSE_SERVER=unix:/run/user/$voice_uid/pulse/native
Environment=TMPDIR=/run/slavik-spotify
RuntimeDirectory=slavik-spotify
RuntimeDirectoryMode=0700
StateDirectory=slavik-spotify
StateDirectoryMode=0700
UMask=0077
ExecStart=/usr/local/libexec/slavik-spotify-player
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
UNIT
sudo install -d -m 755 /usr/local/libexec
if sudo test -f /usr/local/libexec/slavik-spotify-player; then
    mkdir -p "$HOME/slavik-backups"
    sudo cat /usr/local/libexec/slavik-spotify-player > "$HOME/slavik-backups/slavik-spotify-player-$(date +%Y%m%d-%H%M%S)-$$"
fi
sudo install -o root -g root -m 755 "$script_dir/slavik-spotify-player" /usr/local/libexec/slavik-spotify-player
sudo systemd-analyze verify "$work/slavik-spotify.service" || { echo 'Service verification failed' >&2; exit 1; }
if sudo test -f /etc/systemd/system/slavik-spotify.service; then
    mkdir -p "$HOME/slavik-backups"
    sudo cat /etc/systemd/system/slavik-spotify.service > "$HOME/slavik-backups/slavik-spotify-$(date +%Y%m%d-%H%M%S)-$$.service"
fi
# Use our own voice-owned service, so its Pulse credentials/AEC are correct.
if sudo systemctl cat raspotify.service >/dev/null 2>&1; then
    echo 'Disabling the package raspotify service; using slavik-spotify instead.'
    sudo systemctl disable --now raspotify.service
fi
sudo install -m 644 "$work/slavik-spotify.service" /etc/systemd/system/slavik-spotify.service
sudo systemctl daemon-reload
sudo systemctl enable --now avahi-daemon
sudo systemctl enable slavik-spotify
sudo systemctl restart slavik-spotify
sudo systemctl --no-pager --full status slavik-spotify
printf '%s\n' 'Now select Slavik in Spotify on a phone on the same network. Premium is required.'
printf '%s\n' 'A running service alone does not confirm playback. Play a track, then test --spotify-status and voice commands.'
