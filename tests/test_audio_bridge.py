import contextlib
import io
import json
import os
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch


class AudioBridgeTests(unittest.TestCase):
    def bridge(self, value, current=65536):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return NS(stdout=json.dumps([{'name': 'voice_aec_sink', 'volume': {
                'front-left': {'value': current}, 'front-right': {'value': current}}}]))
        with patch('pwd.getpwnam', return_value=NS(pw_uid=os.getuid())), \
             patch('sys.argv', ['slavik-audio', 'volume', value]), \
             patch('subprocess.run', run), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(Path(__file__).resolve().parents[1] / 'slavik-audio'), run_name='__main__')
        return calls, stopped.exception.code, output.getvalue()

    def test_legacy_master_volume_cannot_change_fixed_outputs(self):
        for value in ('up', 'down', '50'):
            calls, code, text = self.bridge(value)
            self.assertIn('fixed at 60%', code)
            self.assertEqual(calls, [])

    def test_invalid_volume_never_runs_pactl(self):
        for value in ['101', '-1', '100; whoami', '１']:
            calls, code, _ = self.bridge(value)
            self.assertNotEqual(code, 0)
            self.assertEqual(calls, [])

    def test_inspect_allows_only_fixed_read_only_pulse_lists(self):
        for kind in ('modules', 'sinks', 'sources', 'sink-inputs', 'source-outputs'):
            with self.subTest(kind=kind), patch('pwd.getpwnam', return_value=NS(pw_uid=os.getuid())), \
                 patch('pwd.getpwuid', return_value=NS(pw_dir='/home/voice')), \
                 patch('sys.argv', ['slavik-audio', 'inspect', kind]), \
                 patch('subprocess.run', return_value=NS(returncode=0)) as run:
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'slavik-audio'), run_name='__main__')
                self.assertEqual(stopped.exception.code, 0)
                args, kwargs = run.call_args
                self.assertEqual(args[0], ['/usr/bin/pactl', '--format=json', 'list', kind])
                self.assertEqual(kwargs['env']['PULSE_SERVER'], f'unix:/run/user/{os.getuid()}/pulse/native')
                self.assertNotIn('PYTHONPATH', kwargs['env'])
                self.assertEqual(kwargs['timeout'], 8)

    def test_inspect_rejects_mutations_extra_args_and_shell_text(self):
        for args in (['inspect', 'set-source-volume'], ['inspect', 'sources; id'],
                     ['inspect', 'sources', 'extra']):
            with self.subTest(args=args), patch('pwd.getpwnam', return_value=NS(pw_uid=os.getuid())), \
                 patch('pwd.getpwuid', return_value=NS(pw_dir='/home/voice')), \
                 patch('sys.argv', ['slavik-audio', *args]), patch('subprocess.run') as run:
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'slavik-audio'), run_name='__main__')
                self.assertNotEqual(stopped.exception.code, 0)
                run.assert_not_called()
