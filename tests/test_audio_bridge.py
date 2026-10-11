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

    def test_master_volume_is_clamped_and_only_targets_aec_sink(self):
        calls, code, text = self.bridge('up')
        self.assertEqual(code, 0)
        self.assertIn('100%', text)
        self.assertEqual(calls[1], ['/usr/bin/pactl', 'set-sink-volume', 'voice_aec_sink', '100%'])
        calls, code, text = self.bridge('down', current=0)
        self.assertIn('0%', text)
        self.assertEqual(calls[1][-1], '0%')

    def test_invalid_volume_never_runs_pactl(self):
        for value in ['101', '-1', '100; whoami', '１']:
            calls, code, _ = self.bridge(value)
            self.assertNotEqual(code, 0)
            self.assertEqual(calls, [])
