import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('connect_player', str(Path(__file__).resolve().parents[1] / 'slavik-spotify-player'))
spec = importlib.util.spec_from_loader(loader.name, loader)
a = importlib.util.module_from_spec(spec)
loader.exec_module(a)

class ConnectTests(unittest.TestCase):
    def test_backend_list_and_format_contract(self):
        available = a.parse_backends('Available backends:\n- alsa (default)\n- pipe\n- subprocess\n')
        music, sink = a.commands(available)
        self.assertIn('pipe', music)
        self.assertNotIn('--passthrough', music)
        self.assertIn('S16', music)
        self.assertIn('--rate=44100', sink)
        self.assertIn('--channels=2', sink)
        self.assertIn('--device=voice_aec_sink', sink)
        native, sink = a.commands({'pulseaudio', 'pipe'})
        self.assertIsNone(sink)
        self.assertIn('voice_aec_sink', native)
        with self.assertRaises(RuntimeError): a.commands({'alsa'})

    def test_configured_scratch_must_work_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'TMPDIR': directory}):
                a.check_temp_directory()
                self.assertEqual(list(Path(directory).iterdir()), [])
            with patch.dict(os.environ, {'TMPDIR': str(Path(directory) / 'missing')}):
                with self.assertRaisesRegex(RuntimeError, 'temporary directory not writable'):
                    a.check_temp_directory()

    def test_real_pipe_passes_pcm_and_exits_without_orphan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            librespot = root / 'librespot'
            pacat = root / 'pacat'
            result = root / 'pcm'
            librespot.write_text('#!/usr/bin/python3\nimport sys\nif sys.argv[-1] == "?": print("Available backends:\\n- pipe")\nelse: sys.stdout.buffer.write(bytes(range(128)))\n')
            pacat.write_text('#!/usr/bin/python3\nimport os,sys\nopen(os.environ["TEST_PCM_OUTPUT"],"wb").write(sys.stdin.buffer.read())\n')
            librespot.chmod(0o700); pacat.chmod(0o700)
            with patch.dict(os.environ, {'TEST_PCM_OUTPUT': str(result), 'TMPDIR': str(root)}):
                self.assertEqual(a.run_player(str(librespot), str(pacat)), 0)
            self.assertEqual(result.read_bytes(), bytes(range(128)))

if __name__ == '__main__': unittest.main()
