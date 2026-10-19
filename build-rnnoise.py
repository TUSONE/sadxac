#!/usr/bin/env python3
"""Build the small classic RNNoise locally from verified, pinned upstream source."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

COMMIT = 'cdf196b1e9de2f8ff1003328ebf9a4316477429d'
REPOSITORY = 'https://github.com/xiph/rnnoise.git'
DEFAULT_OUTPUT = Path.home() / '.local/lib/slavik/librnnoise-classic.so'
SOURCES = ('denoise.c', 'rnn.c', 'rnn_data.c', 'pitch.c', 'kiss_fft.c', 'celt_lpc.c')


def build(cache, output):
    cache, output = Path(cache).resolve(), Path(output).resolve()
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', 'v0.1', REPOSITORY, str(cache)], check=True)
    head = subprocess.check_output(['git', '-C', str(cache), 'rev-parse', 'HEAD'], text=True).strip()
    if head != COMMIT:
        raise RuntimeError('RNNoise source commit differs; existing cache preserved.')
    subprocess.run(['git', '-C', str(cache), 'diff', '--quiet', 'HEAD', '--', 'src', 'include', 'COPYING'], check=True)
    extra = subprocess.check_output(['git', '-C', str(cache), 'ls-files', '--others', '--', 'src', 'include'], text=True)
    if extra.strip():
        raise RuntimeError('Untracked source/header files in RNNoise cache; cache preserved.')
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    compiler = shlex.split(os.getenv('CC', 'cc'))
    flags = ['-O3', '-fPIC', '-shared', '-DRNNOISE_BUILD', '-DCOMPILE_OPUS',
             '-I' + str(cache / 'include'), '-I' + str(cache / 'src')]
    fd, temporary = tempfile.mkstemp(prefix='.rnnoise-', suffix='.so', dir=output.parent)
    os.close(fd)
    try:
        print('RNNoise classic v0.1: commit verified; compiling (one compiler process).', flush=True)
        subprocess.run([*compiler, *flags, *(str(cache / 'src' / name) for name in SOURCES),
                        '-lm', '-o', temporary], check=True)
        # Verify the actual produced library before replacing any installed copy.
        lib = ctypes.CDLL(temporary)
        lib.rnnoise_create.argtypes = []  # classic v0.1 ABI, not the newer model API
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_destroy.restype = None
        lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        state = lib.rnnoise_create()
        if not state:
            raise RuntimeError('RNNoise allocation failed')
        try:
            frame = (ctypes.c_float * 480)()
            probability = lib.rnnoise_process_frame(state, frame, frame)
            if not 0 <= probability <= 1 or any(not -1 <= value <= 1 for value in frame):
                raise RuntimeError('RNNoise silence smoke test failed')
        finally:
            lib.rnnoise_destroy(state)
        digest = hashlib.sha256(Path(temporary).read_bytes()).hexdigest()
        os.chmod(temporary, 0o700)
        os.replace(temporary, output)
        output.with_suffix('.COPYING').write_bytes((cache / 'COPYING').read_bytes())
        output.with_suffix('.json').write_text(json.dumps({'upstream': REPOSITORY, 'commit': COMMIT,
            'abi': 'classic-v0.1', 'sha256': digest, 'flags': flags[:5]}, indent=2) + '\n')
        print(f'Installed: {output}', flush=True)
        return output
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=Path.home() / '.cache/slavik-build/rnnoise-classic')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        build(args.cache, args.output)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise SystemExit(f'Build failed: {error}') from None
