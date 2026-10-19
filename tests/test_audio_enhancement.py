import asyncio
import ctypes
import ctypes.util
import math
import os
import random
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import audio_enhancement as ae
import gemini_assistant_dialog as agent
import mic_processing as mic

NATIVE = os.getenv('SLAVIK_SPEEX_LIBRARY') or ctypes.util.find_library('speexdsp')


@unittest.skipUnless(NATIVE, 'Install libspeexdsp1 for native DSP integration tests')
class NativeDSPTests(unittest.TestCase):
    def test_noise_floor_reduction_and_exact_stream_duration(self):
        rng = random.Random(12)
        noise = [rng.randint(-500, 500) for _ in range(320 * 200)]
        dsp = ae.SpeexEnhancer(NATIVE, agc=False)
        try:
            output = mic.samples(dsp.process(mic.pcm(noise)))
        finally:
            dsp.close()
        self.assertEqual(len(output), len(noise))
        rms = lambda values: math.sqrt(sum(v*v for v in values) / len(values))
        self.assertLess(rms(output[-16000:]), rms(noise[-16000:]) * .7)

    def test_frame_boundaries_silence_and_close(self):
        a, b = ae.SpeexEnhancer(NATIVE), ae.SpeexEnhancer(NATIVE)
        try:
            self.assertEqual(a.process(b'\0' * 1280), b'\0' * 1280)
            self.assertEqual(b.process(b'\0' * 1280), b'\0' * 1280)
            raw = mic.pcm(round(1000 * math.sin(i*.15)) for i in range(320*20))
            whole = a.process(raw)
            pieces = b''.join(b.process(raw[i:i+640]) for i in range(0, len(raw), 640))
            self.assertEqual(whole, pieces)
            self.assertLessEqual(max(abs(v) for v in mic.samples(whole)), 30000)
            with self.assertRaises(ValueError):
                a.process(b'\0' * 641)
        finally:
            a.close()
            b.close()
        a.close()  # idempotent cleanup
        with self.assertRaises(RuntimeError):
            a.process(b'\0' * 640)

    def test_real_abi_agc_target_and_gain_ceiling(self):
        dsp = ae.SpeexEnhancer(NATIVE)
        try:
            if not dsp.agc_enabled:
                self.skipTest('Native fixed-point build lacks AGC')
            for request, expected in ((47, 4000), (31, 12), (27, 6)):
                value = ctypes.c_int()
                self.assertEqual(dsp.library.speex_preprocess_ctl(dsp.state, request, ctypes.byref(value)), 0)
                self.assertEqual(value.value, expected)
        finally:
            dsp.close()


class ActivityTests(unittest.TestCase):
    def test_vad_never_wakes_background_or_authorizes_a_command(self):
        detector = ae.ListenActivity(NS(is_speech=lambda *args: True))
        gate = agent.WakeGate()
        with patch.object(ae.time, 'monotonic', return_value=100):
            detector.feed(mic.pcm([1000, -1000] * 640), gate)
            self.assertFalse(gate.awake)
            self.assertFalse(gate.response_allowed)
            self.assertEqual(gate.wake_wait_until, 0)

    def test_begun_command_survives_late_transcription(self):
        detector = ae.ListenActivity(NS(is_speech=lambda *args: True))
        gate = agent.WakeGate()
        raw = mic.pcm([1000, -1000] * 320)
        with patch.object(ae.time, 'monotonic', return_value=100) as clock:
            gate.set_music_state(True)
            gate.feed_transcript('Славик')
            clock.return_value = 102.8
            detector.feed(raw, gate)
            clock.return_value = 102.84
            detector.feed(raw, gate)
            self.assertGreater(gate.wake_wait_until, 104)
            self.assertFalse(gate.response_allowed)
            clock.return_value = 103.5
            gate.feed_transcript('Включи Кизару')
            self.assertTrue(gate.response_allowed)
            self.assertGreater(gate.command_music_until, 103.5)
            self.assertFalse(gate.waiting_for_command)

    def test_silence_does_not_extend_three_second_window(self):
        detector = ae.ListenActivity(NS(is_speech=lambda *args: True))
        with patch.object(ae.time, 'monotonic', return_value=100) as clock:
            gate = agent.WakeGate()
            gate.set_music_state(True)
            gate.feed_transcript('Славик')
            for value in (100, 101, 102.8):
                clock.return_value = value
                detector.feed(b'\0' * 1280, gate)
            clock.return_value = 103
            self.assertFalse(gate.waiting_for_command)

    def test_continuous_false_vad_is_bounded(self):
        detector = ae.ListenActivity(NS(is_speech=lambda *args: True))
        with patch.object(ae.time, 'monotonic', return_value=100) as clock:
            gate = agent.WakeGate()
            gate.set_music_state(True)
            gate.feed_transcript('Славик')
            for i in range(380):
                clock.return_value = 100 + i*.04
                detector.feed(mic.pcm([1000, -1000] * 320), gate)
            self.assertLessEqual(gate.wake_wait_until, 115)
            self.assertFalse(gate.response_allowed)


class CommandListeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_transcript_keeps_music_silent_until_reply(self):
        from test_agent import FakePlayer, FakeSession, message
        with patch.object(agent.time, 'monotonic', return_value=100):
            gate = agent.WakeGate()
            gate.set_music_state(True)
            gate.feed_transcript('Славик')
            gate.feed_transcript('Включи')
            player = NS(queue=asyncio.Queue(), audio_until=0)
            writes = []
            proc = NS(returncode=None, stdin=NS(write=writes.append, drain=AsyncMock(), close=lambda: None), wait=AsyncMock())
            with patch.dict(agent.os.environ, {'SLAVIK_AUDIO_HELPER': '/helper', 'SLAVIK_MUSIC_DUCK': '1'}), \
                 patch.object(agent.asyncio, 'create_subprocess_exec', return_value=proc), \
                 patch.object(agent.asyncio, 'sleep', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await agent.conversation_focus(gate, player)
            self.assertEqual(writes, [b'listen\n'])
            gate.feed_transcript('Кизару')
            with self.assertRaises(RuntimeError):
                await agent.receiver(FakeSession([[message(b'answer', turn_complete=True)]]), FakePlayer(), gate)
            self.assertEqual(gate.command_music_until, 0)

    async def test_three_seconds_start_after_pulse_ack_and_heartbeat_does_not_extend_forever(self):
        with patch.object(agent.time, 'monotonic', return_value=100) as clock:
            gate = agent.WakeGate()
            gate.set_music_state(True)
            gate.feed_transcript('Славик')
            reader = asyncio.StreamReader()
            task = asyncio.create_task(agent.music_state_reader(NS(stdout=reader), gate))
            clock.return_value = 104
            self.assertFalse(gate.waiting_for_command)
            reader.feed_data(b'duck:listen\n')
            await asyncio.sleep(0)
            self.assertEqual(gate.wake_wait_until, 107)
            clock.return_value = 105
            reader.feed_data(b'duck:listen\n')
            await asyncio.sleep(0)
            self.assertEqual(gate.wake_wait_until, 107)
            reader.feed_eof()
            await task

    async def test_tool_turn_boundary_keeps_silence_while_waiting_for_result(self):
        with patch.object(agent.time, 'monotonic', return_value=100) as clock:
            gate = agent.WakeGate()
            gate.set_music_state(True)
            gate.feed_transcript('Славик')
            gate.feed_transcript('Включи Кизару')
            gate.complete_turn()  # model handed off to a slow authorized tool
            clock.return_value = 105
            writes = []
            proc = NS(returncode=None, stdin=NS(write=writes.append, drain=AsyncMock(), close=lambda: None), wait=AsyncMock())
            with patch.dict(agent.os.environ, {'SLAVIK_AUDIO_HELPER': '/helper', 'SLAVIK_MUSIC_DUCK': '1'}), \
                 patch.object(agent.asyncio, 'create_subprocess_exec', return_value=proc), \
                 patch.object(agent.asyncio, 'sleep', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await agent.conversation_focus(gate, NS(queue=asyncio.Queue(), audio_until=0))
            self.assertEqual(writes, [b'listen\n'])
            self.assertFalse(gate.response_allowed)  # mute never grants permission

    async def test_microphone_keeps_sending_audio_when_no_request_is_allowed(self):
        capture = NS(read_mono=AsyncMock(side_effect=[b'\0' * 1280, asyncio.CancelledError]))
        session = NS(send_realtime_input=AsyncMock())
        gate = agent.WakeGate()
        with self.assertRaises(asyncio.CancelledError):
            await agent.microphone_sender(session, capture, gate)
        self.assertFalse(gate.response_allowed)
        session.send_realtime_input.assert_awaited_once()


class EnhancementCLITests(unittest.TestCase):
    def test_missing_native_library_has_actionable_error(self):
        with patch.dict(os.environ, {'SLAVIK_MIC_ENHANCEMENT': 'speex'}), patch.object(ae.ctypes, 'CDLL', side_effect=OSError):
            with self.assertRaisesRegex(RuntimeError, 'install-linux.sh'):
                mic.enhance_processor(mic.InputProcessor())

    def test_fixed_point_build_keeps_denoising_when_agc_is_unavailable(self):
        destroyed = []
        def init(*args): return 1
        def destroy(state): destroyed.append(state)
        def ctl(state, request, value): return -1 if request == 2 else 0
        def run(*args): return 1
        lib = NS(speex_preprocess_state_init=init, speex_preprocess_state_destroy=destroy,
                 speex_preprocess_ctl=ctl, speex_preprocess_run=run)
        with patch.object(ae.ctypes, 'CDLL', return_value=lib):
            dsp = ae.SpeexEnhancer()
            self.assertFalse(dsp.agc_enabled)
            dsp.close()
        self.assertEqual(destroyed, [1])

    def test_cli_enhancement_overrides_old_env(self):
        async def entry(args):
            self.assertEqual(agent.os.getenv('SLAVIK_MIC_ENHANCEMENT'), 'speex')
        with patch.dict(agent.os.environ, {'SLAVIK_MIC_ENHANCEMENT': 'off'}), \
             patch.object(agent.sys, 'argv', ['gemini_assistant_dialog.py', '--mic-enhancement', 'speex']), \
             patch.object(agent, 'entry', side_effect=entry):
            agent.main()
