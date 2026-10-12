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


def prepare_output(environment):
    """Move legacy master attenuation into existing streams; future voice is independent."""
    def pulse(*args):
        return subprocess.run(['/usr/bin/pactl', *args], env=environment,
                              capture_output=True, text=True, timeout=3, check=True).stdout
    with session_store(environment):
        sinks = json.loads(pulse('--format=json', 'list', 'sinks'))
        sink = next((s for s in sinks if s['name'] == 'voice_aec_sink'), None)
        if sink is None:
            raise RuntimeError('Missing voice_aec_sink')
        master = {ch: v['value'] for ch, v in sink['volume'].items()}
        if all(v == 65536 for v in master.values()):
            print('Общий AEC-выход: 100%; голос и музыка регулируются отдельно.')
            return
        streams = [s for s in json.loads(pulse('--format=json', 'list', 'sink-inputs'))
                   if s.get('sink') == sink['index']]
        plan = []
        fallback = sum(master.values()) / len(master)
        for stream in streams:
            old = volumes(stream)
            new = [round(v['value'] * master.get(ch, fallback) / 65536)
                   for ch, v in stream['volume'].items()]
            plan.append((stream['index'], old, new))
        backup = Path(environment['XDG_RUNTIME_DIR']) / 'slavik-duck' / 'master-migration.json'
        backup.write_text(json.dumps({'sink': sink['index'], 'master': master, 'streams': plan}))
        backup.chmod(0o600)
        changed = []
        try:
            for index, old, new in plan:
                changed.append((index, old))
                pulse('set-sink-input-volume', str(index), *(str(v) for v in new))
            pulse('set-sink-volume', 'voice_aec_sink', '100%')
        except (OSError, subprocess.SubprocessError):
            for index, old in reversed(changed):
                try:
                    pulse('set-sink-input-volume', str(index), *(str(v) for v in old))
                except (OSError, subprocess.SubprocessError):
                    pass
            try:
                pulse('set-sink-volume', 'voice_aec_sink', *(str(v) for v in master.values()))
            except (OSError, subprocess.SubprocessError):
                pass
            raise RuntimeError('Не удалось разделить уровни; проверьте music-levels.sh.') from None
        print('Убран двойной уровень громкости: AEC-выход 100%; громкость текущих потоков сохранена.')


def restore_hardware_output(environment):
    """Restore the WM8960 output path that may be zeroed by boot-time state restore."""
    def run(command):
        return subprocess.run(command, env=environment, capture_output=True,
                              text=True, timeout=3, check=True).stdout
    sinks = json.loads(run(['/usr/bin/pactl', '--format=json', 'list', 'sinks']))
    physical = next((s for s in sinks if s['name'] == 'alsa_output.platform-sound_i2s.stereo-fallback'), None)
    if physical is None:
        raise RuntimeError('Missing WM8960 physical sink')
    base = physical['base_volume']['value']
    if type(base) is not int or not 0 < base <= 65536:
        raise RuntimeError('Invalid WM8960 base volume')
    card = 'WM89602Mic'
    info = run(['/usr/bin/amixer', '-c', card, 'info'])
    if 'WM8960' not in info:
        raise RuntimeError('WM8960 mixer not found')
    path = Path(environment['XDG_RUNTIME_DIR']) / 'slavik-duck'
    path.mkdir(mode=0o700, exist_ok=True)
    backup = path / 'output-before-restore.json'
    # Keep the first snapshot in this boot; repeated starts must not overwrite it.
    if not backup.exists():
        with backup.open('x') as f:
            backup.chmod(0o600)
            json.dump({'physical': physical, 'mixer': run(['/usr/bin/amixer', '-c', card, 'scontents'])}, f)
    for control, value in [('Headphone', '0dB'), ('Speaker', '0dB'), ('Playback', '100%'),
                           ('Left Output Mixer PCM', 'on'), ('Right Output Mixer PCM', 'on')]:
        run(['/usr/bin/amixer', '-c', card, 'sset', control, value])
    run(['/usr/bin/pactl', 'set-sink-volume', physical['name'], str(base)])
    run(['/usr/bin/pactl', 'set-sink-mute', physical['name'], '0'])
    run(['/usr/bin/pactl', 'set-sink-mute', 'voice_aec_sink', '0'])
    print('WM8960: Headphone/Speaker 0 dB, PCM включён, физический выход восстановлен.')
