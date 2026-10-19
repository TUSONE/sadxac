"""Pulse stream ducking: attenuate only Spotify, preserve master/user changes."""
import json
import re
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


def music_playing(streams, saved=None):
    """A paused/corked, muted or silent-volume Connect stream is not audible music."""
    for stream in streams:
        if not is_music(stream) or stream.get('corked') is not False or stream.get('mute', False):
            continue
        current = volumes(stream)
        original, reduced = (saved or {}).get(identity(stream), (None, None))
        level = original if original is not None and current == reduced else current
        if any(v > 0 for v in level):
            return True
    return False


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

    def update(self, streams, active, listening=False):
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
                else:
                    original, previous = self.saved[key]
                    if volumes(stream) != previous:
                        continue  # a manual change wins over mode transitions
                # Name-only listening: silence music, not the player or master.
                # Ordinary speech: .8 Pulse control ~= -5.8 dB amplitude.
                reduced = [0 if listening else round(v * .8) for v in original]
                if key not in self.saved or self.saved[key][1] != reduced:
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
    streams = snapshot()
    ducker.update(streams, False)  # recover a prior crashed helper before ducking again
    print('music:on' if music_playing(streams, ducker.saved) else 'music:off', flush=True)
    active = False
    listening = False
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
                    if line not in (b'on', b'off', b'listen'):
                        raise ValueError('Expected on/off/listen')
                    active = line != b'off'
                    listening = line == b'listen'
                if len(pending) > 16:
                    raise ValueError('Invalid duck command')
            streams = snapshot()
            playing = music_playing(streams, ducker.saved)
            ducker.update(streams, active, listening)
            print('music:on' if playing else 'music:off', flush=True)
    finally:
        ducker.update(snapshot(), False)


def prepare_output(environment):
    """AEC master stays at unity; music/voice keep independent stream levels."""
    def pulse(*args):
        return subprocess.run(['/usr/bin/pactl', *args], env=environment,
                              capture_output=True, text=True, timeout=3, check=True).stdout
    with session_store(environment):
        sinks = json.loads(pulse('--format=json', 'list', 'sinks'))
        sink = next((s for s in sinks if s['name'] == 'voice_aec_sink'), None)
        if sink is None:
            raise RuntimeError('Missing voice_aec_sink')
        if any(v['value'] != 65536 for v in sink['volume'].values()):
            pulse('set-sink-volume', 'voice_aec_sink', '100%')
        print('Общий AEC-выход: 100%; голос и музыка регулируются отдельно.')


def restore_hardware_output(environment):
    """Restore the WM8960 output path that may be zeroed by boot-time state restore."""
    def run(command):
        return subprocess.run(command, env=environment, capture_output=True,
                              text=True, timeout=3, check=True).stdout
    sinks = json.loads(run(['/usr/bin/pactl', '--format=json', 'list', 'sinks']))
    physical = next((s for s in sinks if s['name'] == 'alsa_output.platform-sound_i2s.stereo-fallback'), None)
    if physical is None:
        raise RuntimeError('Missing WM8960 physical sink')
    base = round(65536 * .60)  # Latest user policy: physical output 60%, AEC unity.
    card = 'WM89602Mic'
    info = run(['/usr/bin/amixer', '-c', card, 'info'])
    if 'WM8960' not in info:
        raise RuntimeError('WM8960 mixer not found')
    path = Path(environment['XDG_RUNTIME_DIR']) / 'slavik-duck'
    path.mkdir(mode=0o700, exist_ok=True)
    mixer = run(['/usr/bin/amixer', '-c', card, 'scontents'])
    controls = {}
    for block in mixer.split("Simple mixer control"):
        match = re.match(r"\s+'([^']+)'", block)
        if match:
            controls[match[1]] = block
    backup = path / 'output-before-restore.json'
    # Keep the first snapshot in this boot; repeated starts must not overwrite it.
    if not backup.exists():
        with backup.open('x') as f:
            backup.chmod(0o600)
            json.dump({'physical': physical, 'mixer': mixer}, f)
    mixer_changed = False
    for control, value in [('Headphone', '0dB'), ('Speaker', '0dB'), ('Playback', '100%'),
                           ('Left Output Mixer PCM', 'on'), ('Right Output Mixer PCM', 'on')]:
        block = controls.get(control, '')
        levels = re.findall(r'\[(\d+)%\]', block)
        if value == 'on':
            already_set = '[on]' in block and '[off]' not in block
        elif value == '0dB':
            db = re.findall(r'\[(-?\d+(?:\.\d+)?)dB\]', block)
            # Preserve an already attenuated analogue output. Restore zeroed
            # channels and cap positive gain instead of forcing +6 dB each start.
            already_set = (len(levels) >= 2 and all(int(v) > 0 for v in levels)
                           and len(db) >= 2 and all(float(v) <= 0 for v in db))
        else:
            already_set = len(levels) >= 2 and all(int(v) == 100 for v in levels)
        if not already_set:
            run(['/usr/bin/amixer', '-c', card, 'sset', control, value])
            mixer_changed = True
    if mixer_changed or any(v['value'] != base for v in physical.get('volume', {}).values()):
        run(['/usr/bin/pactl', 'set-sink-volume', physical['name'], str(base)])
    if physical.get('mute') is not False:
        run(['/usr/bin/pactl', 'set-sink-mute', physical['name'], '0'])
    aec = next((s for s in sinks if s['name'] == 'voice_aec_sink'), None)
    if aec and aec.get('mute') is not False:
        run(['/usr/bin/pactl', 'set-sink-mute', 'voice_aec_sink', '0'])
    print('WM8960: PCM включён; физический Pulse-выход 60%.')
