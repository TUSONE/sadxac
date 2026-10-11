"""Pulse stream ducking: attenuate only Spotify, preserve master/user changes."""
import json
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
    def __init__(self, change):
        self.change = change
        self.saved = {}

    def update(self, streams, active):
        present = {identity(s): s for s in streams if is_music(s)}
        for key in list(self.saved):
            original, reduced = self.saved[key]
            stream = present.get(key)
            if stream is None:
                del self.saved[key]
            elif not active:
                # A manual stream-volume change wins over our old snapshot.
                if volumes(stream) == reduced:
                    self.change(stream['index'], original)
                del self.saved[key]
        if active:
            for key, stream in present.items():
                if key not in self.saved:
                    original = volumes(stream)
                    # Pulse volume is cubic: .63 of control ~= .25 amplitude (-12 dB).
                    reduced = [round(v * .63) for v in original]
                    self.saved[key] = original, reduced
                    self.change(stream['index'], reduced)


def serve(environment):
    def pulse(*args):
        return subprocess.run(['/usr/bin/pactl', *args], env=environment,
                              capture_output=True, text=True, timeout=3, check=True).stdout
    def snapshot():
        return json.loads(pulse('--format=json', 'list', 'sink-inputs'))
    ducker = Ducker(lambda index, values: pulse('set-sink-input-volume', str(index),
                                               *(str(v) for v in values)))
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
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
