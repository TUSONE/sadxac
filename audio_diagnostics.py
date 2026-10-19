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
              'mute', 'corked', 'volume', 'base_volume', 'sink', 'source', 'owner_module',
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
                arguments = shlex.split(row.get('argument', ''))
                safe['aec_arguments'] = {k: v for token in arguments
                                         if '=' in token for k, v in [token.split('=', 1)] if k in allowed}
                options = {'analog_gain_control', 'digital_gain_control', 'noise_suppression',
                           'voice_detection', 'extended_filter', 'high_pass_filter',
                           'mobile', 'comfort_noise', 'drift_compensation'}
                for token in arguments:
                    if token.startswith('aec_args='):
                        safe['aec_options'] = {k: v for option in shlex.split(token.split('=', 1)[1])
                            if '=' in option for k, v in [option.split('=', 1)]
                            if k in options and v in ('0', '1', 'true', 'false')}

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
    # The installed sudo rule authorizes the fixed bridge, not `sudo env pactl`.
    # That old route returned command-exited-1 for every node on the real board.
    base = ['sudo', '-n', '-H', '-u', 'voice',
            os.getenv('SLAVIK_AUDIO_HELPER', '/usr/local/libexec/slavik-audio'), 'inspect']
    report['pulse'] = {}
    for kind in ('modules', 'sinks', 'sources', 'sink-inputs', 'source-outputs'):
        raw = command(base + [kind])
        if isinstance(raw, dict):
            report['pulse'][kind] = {**raw, 'hint': 'Обновите установленный аудиомост и sudoers: bash install-linux.sh.'}
            continue
        try:
            report['pulse'][kind] = summarize_nodes(kind, json.loads(raw))
        except (ValueError, TypeError, KeyError):
            report['pulse'][kind] = {'error': 'invalid Pulse JSON'}
    report['aec_reference'] = reference_routing(report['pulse'])
    path = Path(os.getenv('SLAVIK_MIC_CALIBRATION', str(Path.home() / '.config/slavik/mic-calibration.json')))
    try:
        profile = json.loads(path.read_text())
        report['calibration'] = {key: profile[key] for key in ('source', 'rate', 'channel',
             'gain', 'noise_rms', 'speech_rms', 'snr_db', 'raw_clipping_fraction') if key in profile}
    except (OSError, ValueError, TypeError):
        report['calibration'] = {'error': 'profile unavailable'}
    return report


def reference_routing(pulse):
    """Check actual Pulse routes, not merely that named AEC nodes exist."""
    kinds = ('sinks', 'sources', 'sink-inputs', 'modules')
    if any(not isinstance(pulse.get(k), list) for k in kinds):
        return {'status': 'unknown', 'warnings': ['Не удалось прочитать маршруты Pulse.']}
    sink = next((s for s in pulse['sinks'] if s.get('name') == 'voice_aec_sink'), None)
    source = next((s for s in pulse['sources'] if s.get('name') == 'voice_aec_source'), None)
    monitor = next((s for s in pulse['sources'] if s.get('name') == 'voice_aec_sink.monitor'), None)
    warnings, streams = [], []
    if not sink or not source:
        warnings.append('Нет пары voice_aec_sink / voice_aec_source.')
    if not monitor:
        warnings.append('Нет monitor опорного сигнала voice_aec_sink.monitor.')
    if sink and source and sink.get('owner_module') is not None and source.get('owner_module') is not None and sink['owner_module'] != source['owner_module']:
        warnings.append('AEC sink и source принадлежат разным модулям.')
    for stream in pulse['sink-inputs']:
        props = stream.get('properties') or {}
        binary = props.get('application.process.binary', '').rsplit('/', 1)[-1]
        name = props.get('application.name', '')
        if binary not in ('librespot', 'slavik-librespot', 'pacat') and name not in ('SlavikVoice', 'SlavikSpotify'):
            continue
        routed = bool(sink and str(stream.get('sink')) == str(sink['index']))
        streams.append({'index': stream['index'], 'application': name or binary, 'through_aec': routed})
        if not routed:
            warnings.append(f"Поток {stream['index']} ({name or binary}) обходит опору AEC.")
    return {'status': 'warning' if warnings else 'routing_ok', 'streams': streams,
            'reference': 'voice_aec_sink.monitor', 'warnings': warnings,
            'note': 'Маршрутизация не доказывает акустическую эффективность. Нужен AEC-тест с музыкой и речью.'}


def print_diagnostics():
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
    if os.getenv('SPOTIFY_ENABLED', '0') == '1':
        from spotify_api import spotify_command
        print(spotify_command({'action': 'status'}))
