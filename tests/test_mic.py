import json
import math
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import mic_processing as m

class MicrophoneTests(unittest.TestCase):
    def test_dc_rejection_keeps_continuous_stream(self):
        processor = m.InputProcessor()
        raw = m.pcm([2000] * 640)
        first = m.samples(processor.process(raw))
        for _ in range(10): last = m.samples(processor.process(raw))
        self.assertEqual(len(last), 640)
        self.assertLess(max(abs(x) for x in last), 2)
        self.assertGreater(max(first), 1000)

    def test_chunk_boundary_invariance(self):
        values = [int(5000*math.sin(2*math.pi*300*i/16000)) for i in range(6400)]
        whole = m.InputProcessor(.8).process(m.pcm(values))
        fragmented = m.InputProcessor(.8)
        parts = b''.join(fragmented.process(m.pcm(values[i:i+640])) for i in range(0, len(values), 640))
        self.assertEqual(whole, parts)

    def test_gain_and_limiter_prevent_added_clipping(self):
        processor = m.InputProcessor(4)
        raw = m.pcm([0, 15000, -15000] * 200)
        processed = m.samples(processor.process(raw))
        self.assertLessEqual(max(abs(x) for x in processed), 30000)
        self.assertEqual(processor.limited_frames, 1)

    def test_measured_profile_and_low_signal(self):
        def frames(rms, peak, clipped=0):
            return [{'rms': rms, 'peak': peak, 'clipped': clipped, 'count': 640} for _ in range(75)]
        profile = m.calibration(frames(50, 150), frames(1000, 5000), 'aec')
        self.assertAlmostEqual(profile['gain'], 2.6)
        self.assertGreater(profile['snr_db'], 20)
        with self.assertRaises(RuntimeError): m.calibration(frames(500, 800), frames(550, 800), 'aec')
        with self.assertRaises(RuntimeError): m.calibration(frames(50, 100), frames(1000, 32768, 10), 'aec')

    def test_profile_private_and_bound_to_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'profile.json'
            data = {'schema': 1, 'source': 'source', 'rate': 16000, 'channel': 1, 'gain': 2}
            m.save_profile(data, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch.dict(os.environ, {'SLAVIK_MIC_CALIBRATION': str(path)}, clear=True):
                self.assertEqual(m.load_processor('source').gain, 2)
                self.assertEqual(m.load_processor('different-source').gain, 1)

if __name__ == '__main__': unittest.main()


class NearSpeechTests(unittest.TestCase):
    def test_music_floor_click_dc_and_sustained_voice(self):
        import struct
        from mic_processing import NearSpeech
        detector = NearSpeech()
        block = lambda amplitude: struct.pack('<hh', amplitude, -amplitude) * 320
        for i in range(20):
            detector.feed(block(30), i * .04)
        self.assertEqual(detector.until, 0)
        detector.feed(block(10000), 1)
        detector.feed(block(30), 1.04)
        self.assertEqual(detector.until, 0)
        for i in range(5):
            detector.feed(struct.pack('<h', 1000) * 640, 2 + i * .04)
        self.assertEqual(detector.until, 0)
        for i in range(3):
            detector.feed(block(750), 3 + i * .04)
        self.assertGreater(detector.until, 5)
