"""Pulse stream ducking: attenuate only Spotify, preserve master/user changes."""
import json
import fcntl
from pathlib import Path
import tempfile
from contextlib import contextmanager
import os
import select
import signal
import subprocess
import sys


def is_music(stream):
    props = stream.get('properties') or {}
    binary = props.get('application.process.binary', '').rsplit('/', 1)[-1]
    return (binary in ('librespot', 'slavik-librespot') or
            props.get('application.name') == 'SlavikSpotify')


def identity(stream):
    props = stream.get('properties') or {}
    return (stream['index'], stream.get('client'), props.get('application.process.id'),
            props.get('application.process.binary'), props.get('application.name'))


def volumes(stream):
    return [v['value'] for v in stream['volume'].values()]


class Ducker:
    def __init__(self, change, store=None):
        self.change = change
        self.store = store
        self.saved = store.load() if store else {}

    def persist(self):
        if self.store:
            self.store.save(self.saved)

    def update(self, streams, active):
        present = {identity(s): s for s in streams if is_music(s)}
        for key in list(self.saved):
            original, reduced = self.saved[key]
            stream = present.get(key)
            if stream is None:
                del self.saved[key]
                self.persist()
            elif not active:
                # A manual stream-volume change wins over our old snapshot.
                if volumes(stream) == reduced:
                    self.change(stream['index'], original)
                del self.saved[key]
                self.persist()
        if active:
            for key, stream in present.items():
                if key not in self.saved:
                    original = volumes(stream)
                    # Pulse volume is cubic: .63 of control ~= .25 amplitude (-12 dB).
                    reduced = [round(v * .63) for v in original]
                    self.saved[key] = original, reduced
                    self.persist()  # journal BEFORE changing Pulse volume
                    self.change(stream['index'], reduced)


class StateStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {}
        rows = json.loads(self.path.read_text())
        return {tuple(row['identity']): (row['original'], row['reduced']) for row in rows}

    def save(self, saved):
        fd, name = tempfile.mkstemp(prefix='.duck-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump([{'identity': list(key), 'original': values[0], 'reduced': values[1]}
                           for key, values in saved.items()], f)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


@contextmanager
def session_store(environment):
    path = Path(environment['XDG_RUNTIME_DIR']) / 'slavik-duck'
    path.mkdir(mode=0o700, exist_ok=True)
    with (path / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another Slavik duck session is active') from None
        yield StateStore(path / 'state.json')


def serve(environment):
    with session_store(environment) as store:
        _serve(environment, store)


def _serve(environment, store):
    def pulse(*args):
        return subprocess.run(['/usr/bin/pactl', *args], env=environment,
                              capture_output=True, text=True, timeout=3, check=True).stdout
    def snapshot():
        return json.loads(pulse('--format=json', 'list', 'sink-inputs'))
    ducker = Ducker(lambda index, values: pulse('set-sink-input-volume', str(index),
                                               *(str(v) for v in values)), store)
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    ducker.update(snapshot(), False)  # recover a prior crashed helper before ducking again
    active = False
    pending = b''
    try:
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 1)
            if ready:
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if line not in (b'on', b'off'):
                        raise ValueError('Expected on/off')
                    active = line == b'on'
                if len(pending) > 16:
                    raise ValueError('Invalid duck command')
            if active or ducker.saved:
                ducker.update(snapshot(), active)
    finally:
        ducker.update(snapshot(), False)
