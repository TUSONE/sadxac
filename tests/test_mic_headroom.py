"""Execute the actual headroom script against fake hardware commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class HeadroomTests(unittest.TestCase):
    def run_trial(self, fail_backup=False):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            bin_dir = home / 'bin'
            bin_dir.mkdir()
            log = home / 'calls.jsonl'
            fake = '#!' + sys.executable + '\n' + '''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'id':
    print('tusone' if args == ['-un'] else '1000')
elif name == 'pgrep':
    raise SystemExit(1)
elif name == 'sudo':
    with open(os.environ['TRIAL_LOG'], 'a') as f:
        f.write(json.dumps(args) + '\\n')
    if args and args[0] == 'alsactl' and os.environ.get('TRIAL_FAIL') == '1':
        raise SystemExit(3)
    if args[:4] == ['amixer', '-c', '0', 'sget']:
        print('Capture 63 [100%] [30.00dB] [on]')
'''
            for name in ('sudo', 'id', 'pgrep'):
                script = bin_dir / name
                script.write_text(fake)
                script.chmod(0o700)
            environment = {**os.environ, 'HOME': str(home), 'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                           'TRIAL_LOG': str(log), 'TRIAL_FAIL': '1' if fail_backup else '0'}
            script = Path(__file__).resolve().parents[1] / 'mic-headroom.sh'
            result = subprocess.run(['bash', str(script), 'apply'], env=environment,
                                    capture_output=True, text=True, timeout=5)
            calls = [json.loads(row) for row in log.read_text().splitlines()]
            return result, calls

    def test_only_capture_changes_after_successful_backup(self):
        result, calls = self.run_trial()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1], ['amixer', '-c', '0', 'sset', 'Capture', '12dB'])
        self.assertEqual(calls[-2][0], 'alsactl')
        self.assertEqual(calls[-2][-2:], ['store', '0'])
        writes = [call for call in calls if 'sset' in call]
        self.assertEqual(writes, [calls[-1]])
        self.assertFalse(any('pactl' in call for call in calls))
        self.assertIn('Пока не сохраняем', result.stdout)

    def test_backup_failure_prevents_hardware_write(self):
        result, calls = self.run_trial(fail_backup=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('sset' in call for call in calls))
