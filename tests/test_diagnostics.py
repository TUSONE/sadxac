import json
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
import audio_diagnostics as d
import spotify_api as s


class DiagnosticTests(unittest.TestCase):
    def test_pulse_allowlist_excludes_credentials_and_unrelated_module_arguments(self):
        rows = [{'index': 1, 'name': 'module-echo-cancel',
                 'argument': 'source_master=physical source_name=voice_aec_source aec_method=webrtc password=secret',
                 'properties': {'application.name': 'Music', 'secret': 'must-not-log'}},
                {'index': 2, 'name': 'other', 'argument': 'password=secret'}]
        result = d.summarize_nodes('modules', rows)
        text = json.dumps(result)
        self.assertNotIn('secret', text)
        self.assertEqual(result[0]['aec_arguments']['source_master'], 'physical')
        self.assertNotIn('argument', result[1])
        result = d.summarize_nodes('sinks', rows)
        self.assertNotIn('secret', json.dumps(result))

    def test_diagnostics_fail_boundedly_without_leaking_command_error(self):
        with patch.object(d.subprocess, 'run', return_value=NS(returncode=1, stdout='secret', stderr='secret')):
            self.assertEqual(d.command(['test']), {'error': 'command exited 1'})
        with patch.object(d.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret', 8)):
            self.assertEqual(d.command(['test']), {'error': 'TimeoutExpired'})

    def test_spotify_status_reports_context_progress_and_restrictions(self):
        with patch.object(s, 'devices', return_value=[{'name': 'Slavik', 'is_active': True}]), \
             patch.object(s, 'api', return_value={'device': {'name': 'Slavik'}, 'is_playing': True,
                   'item': {'name': 'Third song', 'uri': 'spotify:track:third'},
                   'context': {'uri': 'spotify:playlist:abc'}, 'progress_ms': 2000,
                   'actions': {'disallows': {'skipping_next': True, 'pausing': False}},
                   'access_token': 'must-not-log'}):
            text = s.spotify_command({'action': 'status'})
        self.assertIn('Third song', text)
        self.assertIn('spotify:playlist:abc', text)
        self.assertIn('progress_ms=2000', text)
        self.assertIn('skipping_next', text)
        self.assertNotIn('must-not-log', text)

    def test_status_works_when_nothing_is_playing(self):
        with patch.object(s, 'devices', return_value=[]), patch.object(s, 'api', return_value=None):
            self.assertIn('Активное: нет', s.spotify_command({'action': 'status'}))


class ReferenceTests(unittest.TestCase):
    def topology(self, target=12):
        return {'sinks': [{'index': 12, 'name': 'voice_aec_sink', 'owner_module': 7}],
                'sources': [{'name': 'voice_aec_source', 'owner_module': 7}, {'name': 'voice_aec_sink.monitor'}],
                'modules': [],
                'sink-inputs': [{'index': 30, 'sink': target, 'properties': {'application.name': 'SlavikVoice'}},
                                {'index': 31, 'sink': target, 'properties': {'application.process.binary': '/usr/bin/librespot'}}]}

    def test_both_music_and_voice_must_feed_reference(self):
        good = d.reference_routing(self.topology())
        self.assertEqual(good['status'], 'routing_ok')
        self.assertTrue(all(s['through_aec'] for s in good['streams']))
        bad = self.topology()
        bad['sink-inputs'][1]['sink'] = 99
        result = d.reference_routing(bad)
        self.assertEqual(result['status'], 'warning')
        self.assertIn('обходит', result['warnings'][0])

    def test_missing_monitor_or_split_aec_is_reported(self):
        topology = self.topology()
        topology['sources'] = [{'name': 'voice_aec_source', 'owner_module': 8}]
        result = d.reference_routing(topology)
        self.assertEqual(len(result['warnings']), 2)
        self.assertEqual(d.reference_routing({})['status'], 'unknown')


class BridgeSnapshotTests(unittest.TestCase):
    def test_snapshot_uses_installed_helper_without_unauthorized_sudo_env(self):
        calls = []
        def command(args):
            calls.append(args)
            return '[]' if 'inspect' in args else ''
        with patch.object(d, 'command', side_effect=command), \
             patch.object(d.pwd, 'getpwnam', return_value=NS(pw_uid=1001)), \
             patch.dict(d.os.environ, {'SLAVIK_AUDIO_HELPER': '/usr/local/libexec/slavik-audio'}):
            result = d.snapshot()
        queries = [args for args in calls if 'inspect' in args]
        self.assertEqual(len(queries), 5)
        for args in queries:
            self.assertEqual(args[:7], ['sudo', '-n', '-H', '-u', 'voice', '/usr/local/libexec/slavik-audio', 'inspect'])
            self.assertNotIn('env', args)
        self.assertTrue(all(isinstance(v, list) for v in result['pulse'].values()))

    def test_aec_dsp_options_are_allowlisted_and_credentials_removed(self):
        module = {'name': 'module-echo-cancel', 'argument':
            'aec_method=webrtc aec_args="noise_suppression=1 digital_gain_control=0 password=secret custom=secret"'}
        result = d.summarize_nodes('modules', [module])[0]
        self.assertEqual(result['aec_options'], {'noise_suppression': '1', 'digital_gain_control': '0'})
        self.assertNotIn('secret', json.dumps(result))
