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
