import asyncio
import contextlib
import ctypes
import ctypes.util
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import random
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import gemini_assistant_dialog as agent
import mic_processing as mic
from rnnoise_processing import RNNoiseEnhancer, RateConverter, SpeechGain

RN = os.getenv('SLAVIK_RNNOISE_LIBRARY')
SP = os.getenv('SLAVIK_SPEEX_LIBRARY') or ctypes.util.find_library('speexdsp')


@unittest.skipUnless(RN and SP, 'Build classic RNNoise and install libspeexdsp1 for native tests')
class NativeRNNoiseTests(unittest.TestCase):
    def test_noise_reduction_duration_and_frame_boundaries(self):
        rng = random.Random(19)
        raw = mic.pcm(rng.randint(-600, 600) for _ in range(16000 * 2))
        a, b = RNNoiseEnhancer(RN, SP), RNNoiseEnhancer(RN, SP)
        try:
            whole = a.process(raw)
            fragmented = b''.join(b.process(raw[i:i+320]) for i in range(0, len(raw), 320))
            self.assertEqual(whole, fragmented)
            self.assertEqual(len(whole), len(raw))
            rms = lambda values: math.sqrt(sum(v*v for v in values) / len(values))
            self.assertLess(rms(mic.samples(whole)[-8000:]), rms(mic.samples(raw)[-8000:]) * .7)
            self.assertTrue(0 <= a.probability <= 1)
            self.assertLessEqual(max(abs(v) for v in mic.samples(whole)), 30000)
            with self.assertRaises(ValueError):
                a.process(b'\0' * 321)
        finally:
            a.close()
            b.close()

    def test_silence_and_idempotent_close(self):
        dsp = RNNoiseEnhancer(RN, SP)
        self.assertEqual(dsp.process(b'\0' * 1280), b'\0' * 1280)
        dsp.close()
        dsp.close()
        with self.assertRaises(RuntimeError):
            dsp.process(b'\0' * 320)

    def test_rate_conversion_preserves_pitch_and_sample_count(self):
        lib = ctypes.CDLL(SP)
        up, down = RateConverter(lib, 16000, 48000), RateConverter(lib, 48000, 16000)
        source = [round(10000 * math.sin(2*math.pi*440*i/16000)) for i in range(16000)]
        output = []
        try:
            for i in range(0, len(source), 160):
                output.extend(down.convert(up.convert(source[i:i+160], 480), 160))
        finally:
            up.close()
            down.close()
        self.assertEqual(len(output), len(source))
        stable = output[1000:]
        crossings = sum(a <= 0 < b for a, b in zip(stable, stable[1:]))
        self.assertAlmostEqual(crossings / (len(stable)/16000), 440, delta=2)
        with self.assertRaises(RuntimeError):
            up.convert([0] * 160, 480)


class GainTests(unittest.TestCase):
    def test_noise_is_not_boosted_and_speech_gain_is_bounded(self):
        gain = SpeechGain()
        for _ in range(100):
            gain.apply([100, -100] * 80, .01)
        self.assertEqual(gain.gain, 1)
        for _ in range(500):
            gain.apply([100, -100] * 80, .99)
        self.assertLessEqual(gain.gain, 4)
        self.assertGreater(gain.gain, 1)
        output = mic.samples(gain.apply([32000, -32000] * 80, .99))
        self.assertLessEqual(max(abs(v) for v in output), 30000)

    def test_source_and_channel_do_not_inherit_wrong_calibration(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / 'mic.json'
            mic.save_profile({'schema': 1, 'source': 'voice_aec_source', 'rate': 16000,
                              'channel': 1, 'gain': .4177}, profile)
            with patch.dict(os.environ, {'SLAVIK_MIC_CALIBRATION': str(profile), 'SLAVIK_MIC_ENHANCEMENT': 'off'}, clear=True):
                self.assertEqual(mic.load_processor(agent.PHYSICAL_MIC_SOURCE).gain, 1)
                with patch.dict(os.environ, {'SLAVIK_MIC_CHANNEL': '0'}):
                    processor = mic.load_processor('voice_aec_source')
                    self.assertEqual((processor.channel, processor.gain), (0, 1))
                    with patch.dict(os.environ, {'SLAVIK_MIC_GAIN': '2'}):
                        self.assertEqual(mic.load_processor('voice_aec_source').gain, 2)
                self.assertEqual(mic.load_processor('voice_aec_source').gain, .4177)
            self.assertEqual(json.loads(profile.read_text())['gain'], .4177)

    def test_physical_cli_overrides_old_source_gain_and_speex(self):
        async def entry(args):
            self.assertEqual(agent.MIC_SOURCE, agent.PHYSICAL_MIC_SOURCE)
            self.assertEqual(agent.os.getenv('SLAVIK_MIC_ENHANCEMENT'), 'off')
            self.assertEqual(agent.os.getenv('SLAVIK_MIC_GAIN'), '2.0')
            self.assertEqual(agent.os.getenv('SLAVIK_MIC_CHANNEL'), '0')
        with patch.dict(os.environ, {'GEMINI_MIC_SOURCE': 'voice_aec_source', 'SLAVIK_MIC_GAIN': '.4177', 'SLAVIK_MIC_ENHANCEMENT': 'speex'}), \
             patch.object(agent, 'MIC_SOURCE', 'voice_aec_source'), \
             patch.object(agent.sys, 'argv', ['agent', '--mic-source', 'physical', '--mic-gain', '2', '--mic-enhancement', 'off', '--mic-channel', 'left']), \
             patch.object(agent, 'entry', side_effect=entry):
            agent.main()

    def test_build_refuses_unexpected_source_before_touching_installed_library(self):
        spec = importlib.util.spec_from_file_location('rnnoise_builder', Path(__file__).resolve().parents[1] / 'build-rnnoise.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache'
            cache.mkdir()
            output = Path(folder) / 'installed.so'
            output.write_bytes(b'old-working-library')
            with patch.object(builder.subprocess, 'check_output', return_value='unexpected-commit\n'), \
                 patch.object(builder.subprocess, 'run') as execute:
                with self.assertRaisesRegex(RuntimeError, 'commit differs'):
                    builder.build(cache, output)
            execute.assert_not_called()
            self.assertEqual(output.read_bytes(), b'old-working-library')


class DirectCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_physical_mode_requires_address_during_own_voice(self):
        from test_agent import FakePlayer, FakeSession, message
        for text, authorized in (('Поставь паузу', False), ('Славик поставь паузу', True)):
            gate, player = agent.WakeGate(), FakePlayer()
            player.audio_until, player.queue = 110, asyncio.Queue()
            with patch.object(agent.time, 'monotonic', return_value=100), patch.object(agent, 'MIC_SOURCE', agent.PHYSICAL_MIC_SOURCE):
                gate.feed_transcript('Славик привет')
                with self.assertRaises(RuntimeError):
                    await agent.receiver(FakeSession([[message(input_transcription=NS(text=text)), message(b'answer', turn_complete=True)]]), player, gate)
            self.assertEqual(player.flushed, int(authorized))
            self.assertEqual(bool(player.audio), authorized)

    async def test_diagnostic_reports_both_raw_channels_and_processing_budget(self):
        class Capture:
            def __init__(self, **kwargs): pass
            async def start(self): pass
            async def close(self): pass
            async def read_stereo(self): return mic.pcm([-1000, -20, 1000, 20] * 320)
        processor = mic.InputProcessor()
        output = io.StringIO()
        with patch.object(agent, 'PulseCapture', Capture), patch.object(agent, 'load_processor', return_value=processor), contextlib.redirect_stdout(output):
            await agent.diagnostic('mic')
        self.assertEqual(output.getvalue().count('L_RMS=1000.0 R_RMS=20.0'), 10)
        self.assertIn('DSP_avg=', output.getvalue())
        self.assertIn('ms/40ms', output.getvalue())
