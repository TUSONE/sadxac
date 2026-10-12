"""Read-only, bounded diagnostics. Never dumps environment, process arguments or tokens."""
import json
import os
import pwd
import subprocess
import time
from pathlib import Path


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=8)
        if result.returncode:
            return {'error': f'command exited {result.returncode}'}
        return result.stdout
    except (OSError, subprocess.SubprocessError) as error:
        return {'error': type(error).__name__}


def summarize_nodes(kind, rows):
    if not isinstance(rows, list):
        return {'error': 'unexpected Pulse response'}
    fields = ('index', 'name', 'state', 'sample_specification', 'channel_map',
              'mute', 'volume', 'base_volume', 'sink', 'source', 'owner_module',
              'buffer_usec', 'sink_usec', 'source_usec', 'monitor_of_sink')
    properties = ('application.name', 'application.process.binary', 'application.process.id', 'media.name', 'node.name', 'device.description')
    result = []
    for row in rows:
        safe = {key: row[key] for key in fields if key in row}
        safe['properties'] = {key: row.get('properties', {})[key] for key in properties
                              if key in row.get('properties', {})}
        if kind == 'modules':
            safe = {key: row[key] for key in ('index', 'name') if key in row}
            if 'echo-cancel' in row.get('name', ''):
                import shlex
                allowed = {'aec_method', 'source_master', 'sink_master', 'source_name',
                           'sink_name', 'rate', 'channels'}
                safe['aec_arguments'] = {k: v for token in shlex.split(row.get('argument', ''))
                                         if '=' in token for k, v in [token.split('=', 1)] if k in allowed}
        result.append(safe)
    return result


def snapshot():
    report = {'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'revision': command(['git', 'rev-parse', '--short', 'HEAD']),
              'librespot': command(['/usr/bin/librespot', '--version']),
              'local_librespot': command(['/usr/local/libexec/slavik-librespot', '--version']),
              'service': command(['systemctl', 'show', 'slavik-spotify', '-p', 'ActiveState',
                                  '-p', 'SubState', '-p', 'NRestarts', '-p', 'MainPID']),
              'processes': command(['ps', '-eo', 'user,pid,comm,pcpu,pmem', '--sort=-pcpu']),
              'mixer': command(['amixer', '-c', '0', 'scontents'])}
    try:
        uid = pwd.getpwnam('voice').pw_uid
    except KeyError:
        report['pulse'] = {'error': 'voice user missing'}
        return report
    base = ['sudo', '-n', '-H', '-u', 'voice', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
            f'PULSE_SERVER=unix:/run/user/{uid}/pulse/native', '/usr/bin/pactl']
    report['pulse'] = {}
    for kind in ('modules', 'sinks', 'sources', 'sink-inputs', 'source-outputs'):
        raw = command(base + ['--format=json', 'list', kind])
        if isinstance(raw, dict):
            report['pulse'][kind] = raw
            continue
        try:
            report['pulse'][kind] = summarize_nodes(kind, json.loads(raw))
        except (ValueError, TypeError, KeyError):
            report['pulse'][kind] = {'error': 'invalid Pulse JSON'}
    path = Path(os.getenv('SLAVIK_MIC_CALIBRATION', str(Path.home() / '.config/slavik/mic-calibration.json')))
    try:
        profile = json.loads(path.read_text())
        report['calibration'] = {key: profile[key] for key in ('source', 'rate', 'channel',
             'gain', 'noise_rms', 'speech_rms', 'snr_db', 'raw_clipping_fraction') if key in profile}
    except (OSError, ValueError, TypeError):
        report['calibration'] = {'error': 'profile unavailable'}
    return report


def print_diagnostics():
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
    if os.getenv('SPOTIFY_ENABLED', '0') == '1':
        from spotify_api import spotify_command
        print(spotify_command({'action': 'status'}))
