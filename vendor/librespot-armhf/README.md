# ARMv7 Connect fix

Upstream: https://github.com/librespot-org/librespot
Base revision: `939dc5ee` (the version observed on the user's board).
Patch: ../../patches/librespot-empty-skip-target.patch
License: MIT; upstream notice in LICENSE.librespot.

The desktop client supplied an empty track_uri. PlayingTrack::try_from selected
Some(Uri("")) before checking track_uid or track_index, so handle_load failed to
resolve that target and fell back to index 0. The patch ignores empty/whitespace
URI/UID fields, preserving normal URI > UID > index precedence. Four upstream
Rust regression tests cover empty URI with UID, empty identifiers with index,
normal URI precedence and missing target rejection.

The native PulseAudio backend and decoded S16 playback are preserved. The binary
uses ARMv7 GNU hard-float and system CA certificates. The package executable
/usr/bin/librespot is untouched; the reviewed build is installed as
/usr/local/libexec/slavik-librespot and selected by our voice-owned supervisor.
Installation restarts only slavik-spotify; OAuth/cache/mixer/AEC are unchanged.

Rebuild on a development machine with Docker using build.sh and a new empty build
path; do not compile on the 484 MB board. Dockerfile and Cargo.lock document the
build inputs. SHA256SUMS covers the compressed binary and license bundle.
