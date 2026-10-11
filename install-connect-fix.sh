#!/usr/bin/env bash
# Install the pinned ARMv7 build alongside the package binary, with rollback.
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $(id -un) == tusone ]] || { echo 'Run as tusone' >&2; exit 1; }
[[ $(dpkg --print-architecture) == armhf ]] || { echo 'This build requires ARMv7 armhf' >&2; exit 1; }
sudo -v
sudo systemctl cat slavik-spotify >/dev/null
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
(cd vendor/librespot-armhf && sha256sum -c SHA256SUMS)
gzip -dc vendor/librespot-armhf/librespot.gz > "$work/librespot"
chmod 755 "$work/librespot"
"$work/librespot" --version
"$work/librespot" --backend '?' > "$work/backends"
if ! grep -Eq '^[[:space:]]*-[[:space:]]*pulseaudio([[:space:]]|$)' "$work/backends"; then
    echo 'Expected PulseAudio backend; service unchanged' >&2
    exit 1
fi
backup="$HOME/slavik-backups/connect-fix-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$backup"
for name in slavik-librespot slavik-spotify-player; do
    if sudo test -f "/usr/local/libexec/$name"; then
        sudo cat "/usr/local/libexec/$name" > "$backup/$name"
    fi
done
sudo install -o root -g root -m 755 "$work/librespot" /usr/local/libexec/slavik-librespot
sudo install -o root -g root -m 755 slavik-spotify-player /usr/local/libexec/slavik-spotify-player
sudo systemctl restart slavik-spotify
sudo systemctl --no-pager --full status slavik-spotify
printf '%s\n' "Installed empty-target fix. Backup: $backup" 'Select Slavik again in Spotify; test three different songs.'
