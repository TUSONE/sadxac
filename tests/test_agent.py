import asyncio
import importlib.util
import math
import struct
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace as NS

spec = importlib.util.spec_from_file_location('agent', Path(__file__).resolve().parents[1] / 'gemini_assistant_dialog.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

class PCMTests(unittest.TestCase):
    def test_right_channel(self):
        raw = struct.pack('<hhhhhh', 10, 20, -10, -20, 7, 8)
        self.assertEqual(struct.unpack('<hhh', a.stereo_to_mono_channel(raw, 1)), (20, -20, 8))

    def test_resampling_chunk_boundaries_duration_and_pitch(self):
        raw = struct.pack('<' + 'h' * 24000, *[int(10000 * math.sin(2*math.pi*440*i/24000)) for i in range(24000)])
        reference = a.Resampler()
        expected = reference.convert(raw) + reference.finish()
        stream = a.Resampler()
        actual = b''.join(stream.convert(raw[i:i+137]) for i in range(0, len(raw), 137)) + stream.finish()
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 48000 * 4)
        samples = struct.unpack('<' + 'h' * 96000, actual)
        self.assertEqual(samples[::2], samples[1::2])
        mono = samples[::2]
        crossings = sum(x <= 0 < y for x, y in zip(mono, mono[1:]))
        self.assertAlmostEqual(crossings, 440, delta=1)

    def test_interruption_resets_partial_bytes(self):
        resampler = a.Resampler()
        resampler.convert(b'\xff')
        resampler.reset()
        actual = resampler.convert(struct.pack('<h', 100)) + resampler.finish()
        self.assertEqual(struct.unpack('<hhhh', actual), (100,) * 4)

    def test_bad_format_rejected(self):
        with self.assertRaises(RuntimeError): a.Resampler().convert(b'xx', 16000)
        r = a.Resampler(); r.convert(b'x')
        with self.assertRaises(RuntimeError): r.finish()

class WakeTests(unittest.TestCase):
    def test_wakes_and_background(self):
        for text in ['Эй Славка!', 'Славик', 'Ебаные славики', 'Ёбаные славики', 'Славуха']:
            self.assertTrue(a.contains_wake(text))
        self.assertFalse(a.contains_wake('обычный разговор о музыке'))
        self.assertFalse(a.contains_wake('славиковский'))

    def test_split_transcript_and_timeout(self):
        gate = a.WakeGate(wake_enabled=True)
        self.assertFalse(gate.feed_transcript('Сла'))
        self.assertTrue(gate.feed_transcript('вик'))
        self.assertTrue(gate.awake)
        before = gate.awake_until
        gate.feed_transcript('продолжай')
        self.assertGreaterEqual(gate.awake_until, before)
        gate.awake_until = time.monotonic() - 1
        self.assertFalse(gate.awake)

    def test_old_fragments_do_not_wake(self):
        gate = a.WakeGate(wake_enabled=True); gate.feed_transcript('Эй ')
        gate.last_input -= 3
        self.assertFalse(gate.feed_transcript('Славка'))

    def test_continuous_mode_has_no_timeout_or_wake_requirement(self):
        gate = a.WakeGate(wake_enabled=False)
        gate.awake_until = time.monotonic() - 100
        self.assertTrue(gate.awake)
        self.assertFalse(gate.feed_transcript('Привет, братуха'))
        self.assertTrue(gate.awake)
        self.assertIn('Основной язык разговора — русский', a.system_instruction())

    def test_wake_timer_expires_and_background_cannot_extend_or_reopen(self):
        with patch.object(a.time, 'monotonic') as clock:
            clock.return_value = 100
            gate = a.WakeGate(wake_enabled=True)
            self.assertFalse(gate.awake)
            self.assertFalse(gate.feed_transcript('Привет, братуха'))
            self.assertTrue(gate.feed_transcript('Славик'))
            self.assertEqual(gate.awake_until, 100 + a.AWAKE_SECONDS)
            clock.return_value = 100 + a.AWAKE_SECONDS / 2
            gate.feed_transcript('Как дела?')
            deadline = gate.awake_until
            self.assertEqual(deadline, clock.return_value + a.AWAKE_SECONDS)
            clock.return_value = deadline
            self.assertFalse(gate.awake)
            gate.feed_transcript('Разговор между людьми')
            self.assertFalse(gate.awake)
            self.assertEqual(gate.awake_until, deadline)
            clock.return_value += 10
            self.assertTrue(gate.feed_transcript('Эй Славка'))
            self.assertTrue(gate.awake)

    def test_sdk_config(self):
        config = a.make_config().model_dump(exclude_none=True)
        self.assertNotIn('proactivity', config)
        self.assertEqual(config['response_modalities'], ['AUDIO'])
        self.assertEqual(config['realtime_input_config']['automatic_activity_detection']['start_of_speech_sensitivity'], 'START_SENSITIVITY_LOW')

class FakePlayer:
    def __init__(self): self.audio = []; self.flushed = 0; self.finished = 0
    async def put(self, data, rate): self.audio.append((data, rate))
    async def flush(self): self.audio.clear(); self.flushed += 1
    async def finish_turn(self): self.finished += 1

class FakeSession:
    def __init__(self, turns): self.turns = iter(turns); self.calls = 0
    async def receive(self):
        self.calls += 1
        for msg in next(self.turns, []): yield msg

def message(data=None, **fields):
    return NS(data=data, server_content=NS(**fields))

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_mode_plays_without_wake_even_with_wrong_transcription(self):
        gate = a.WakeGate(wake_enabled=False); player = FakePlayer()
        session = FakeSession([
            [message(b'reply', input_transcription=NS(text='Salom, bratuha.'), turn_complete=True)],
            [message(b'next', turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [(b'reply', 24000), (b'next', 24000)])
        self.assertEqual(player.finished, 2)

    async def test_background_dropped_multiple_turns_and_no_duplicate_audio(self):
        gate = a.WakeGate(wake_enabled=True); player = FakePlayer()
        fallback = NS(parts=[NS(inline_data=NS(data=b'fallback', mime_type='audio/pcm;rate=24000'))])
        session = FakeSession([
            [message(b'background', turn_complete=True)],
            [message(b'wake', input_transcription=NS(text='Славик'), model_turn=fallback), message(turn_complete=True)],
            [message(b'followup'), message(turn_complete=True)]])
        with self.assertRaisesRegex(RuntimeError, 'ended'):
            await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [(b'wake', 24000), (b'followup', 24000)])
        self.assertEqual(player.finished, 2)
        self.assertEqual(session.calls, 4)

    async def test_interrupted_flush_always_and_same_event_transcript(self):
        gate = a.WakeGate(wake_enabled=True); player = FakePlayer()
        session = FakeSession([[message(b'stale', interrupted=True, input_transcription=NS(text='Славик')), message(b'new', turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.flushed, 1)
        self.assertTrue(gate.awake)
        self.assertEqual(player.audio, [(b'new', 24000)])

    async def test_hidden_turn_never_released_after_late_wake(self):
        gate = a.WakeGate(wake_enabled=True); player = FakePlayer()
        session = FakeSession([[message(b'background'), message(b'late', input_transcription=NS(text='Славик')), message(turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [])

    async def test_fallback_pcm(self):
        gate = a.WakeGate(wake_enabled=True); gate.extend(); player = FakePlayer()
        part = NS(inline_data=NS(data=b'pcm', mime_type='audio/pcm;rate=48000'))
        session = FakeSession([[message(model_turn=NS(parts=[part]), turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [(b'pcm', 48000)])

    async def test_authorized_reply_survives_wake_timeout(self):
        gate = a.WakeGate(wake_enabled=True); gate.extend(); player = FakePlayer()
        class Session:
            calls = 0
            async def receive(self):
                self.calls += 1
                if self.calls == 1:
                    yield message(b'first')
                    gate.awake_until = time.monotonic() - 1
                    yield message(b'last', turn_complete=True)
        with self.assertRaises(RuntimeError):
            await a.receiver(Session(), player, gate)
        self.assertEqual(player.audio, [(b'first', 24000), (b'last', 24000)])

    async def test_spotify_asleep_is_denied(self):
        from google.genai import types
        queue = asyncio.Queue()
        call = NS(name='spotify_command', id='call1', args={'action': 'pause'})
        queue.put_nowait((call, False))
        called = []
        replies = []
        class Session:
            async def send_tool_response(self, **args): replies.append(args)
        task = asyncio.create_task(a.tool_worker(Session(), queue, lambda args: called.append(args)))
        await asyncio.wait_for(queue.join(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(called)
        self.assertEqual(replies[0]['function_responses'][0].id, 'call1')
        self.assertIn('спит', replies[0]['function_responses'][0].response['result'])


    async def test_flush_does_not_wait_for_blocked_writer(self):
        player = a.PulsePlayer()
        drained = asyncio.Event()
        class Input:
            def write(self, data): pass
            async def drain(self):
                await drained.wait()
                raise BrokenPipeError()
        class Proc:
            returncode = None
            stdin = Input()
            def terminate(self): self.returncode = -15; drained.set()
            async def wait(self): return self.returncode
        player.proc = Proc()
        player.enqueue(b'audio')
        writer = asyncio.create_task(player.run())
        await asyncio.sleep(0)
        await asyncio.wait_for(player.flush(), .2)
        await asyncio.sleep(0)
        self.assertEqual(player.generation, 1)
        self.assertTrue(player.queue.empty())
        writer.cancel()
        with self.assertRaises(asyncio.CancelledError): await writer

if __name__ == '__main__': unittest.main()
