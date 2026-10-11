#!/usr/bin/env bash
# Rebuild outside the small board. Requires Docker and network access.
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
build_dir=${1:?Usage: build.sh /absolute/build-directory}
[[ "$build_dir" == /* ]] || { echo 'Use an absolute build directory' >&2; exit 1; }
mkdir -p "$build_dir"
git clone https://github.com/librespot-org/librespot.git "$build_dir/source"
git -C "$build_dir/source" checkout --detach 939dc5ee
git -C "$build_dir/source" apply "$repo/patches/librespot-empty-skip-target.patch"
docker build -t slavik-connect-builder -f "$repo/vendor/librespot-armhf/Dockerfile" "$repo/vendor/librespot-armhf"
mkdir -p "$build_dir/output" "$build_dir/cargo"
docker run --rm -v "$build_dir/source:/src" -v "$build_dir/output:/out" -v "$build_dir/cargo:/cache" \
    -e CARGO_TARGET_DIR=/out -e CARGO_HOME=/cache slavik-connect-builder sh -ec '
    git config --global --add safe.directory /src
    cargo test --locked -j 4 -p librespot-connect --no-default-features --features rustls-tls-native-roots skip_to_tests
    cargo build --locked -j 4 --release --target armv7-unknown-linux-gnueabihf --no-default-features --features pulseaudio-backend,with-libmdns,rustls-tls-native-roots
    arm-linux-gnueabihf-strip /out/armv7-unknown-linux-gnueabihf/release/librespot
    '
gzip -n -c "$build_dir/output/armv7-unknown-linux-gnueabihf/release/librespot" > "$build_dir/librespot.gz"
printf '%s\n' "Built: $build_dir/librespot.gz"
