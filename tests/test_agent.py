import asyncio
import importlib.util
import math
import struct
import time
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

class VolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_music_and_voice_are_independent(self):
        player = a.PulsePlayer()
        called = []
        spotify = lambda args: called.append(args) or 'music changed'
        self.assertEqual(player.voice_percent, 40)
        await a.route_volume({'action': 'up'}, spotify, player)
        self.assertEqual(called, [{'action': 'volume_up', 'percent': None}])
        self.assertEqual(player.voice_percent, 40)
        await a.route_volume({'action': 'down', 'target': 'voice'}, spotify, player)
        self.assertEqual(player.voice_percent, 30)
        self.assertEqual(len(called), 1)
        raw = struct.pack('<hh', 10000, -10000)
        self.assertEqual(struct.unpack('<hh', player.scale_voice(raw)), (270, -270))
        for value in (-1, 101, True, '50'):
            self.assertIn('Не выполнено', player.voice_volume({'action': 'set', 'percent': value}))
        self.assertEqual(player.voice_percent, 30)

    async def test_ordinary_volume_overrides_wrong_model_target_without_changing_voice(self):
        player = a.PulsePlayer()
        called = []
        await a.route_volume({'action': 'up', 'target': 'voice'},
                             lambda args: called.append(args) or 'music50', player,
                             'Славик, сделай громкость 50')
        self.assertEqual(called, [{'action': 'set_volume', 'percent': 50}])
        self.assertEqual(player.voice_percent, 40)
        await a.route_volume({'action': 'down', 'target': 'music'},
                             lambda args: called.append(args) or 'music', player,
                             'Славик говори тише')
        self.assertEqual(player.voice_percent, 30)
        self.assertEqual(len(called), 1)

class WakeTests(unittest.TestCase):
    def test_wakes_and_background(self):
        for text in ['Эй Славка!', 'Славик', 'Ебаные славики', 'Ёбаные славики', 'Славуха', 'Hey Slavik', 'Славка, как дела?', 'Как дела, Славка?', 'Включи, Славка, музыку', 'How are you, Slavka?']:
            self.assertTrue(a.contains_wake(text))
        self.assertFalse(a.contains_wake('обычный разговор о музыке'))
        self.assertFalse(a.contains_wake('славиковский'))

    def test_split_transcript_and_timeout(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True)
        self.assertFalse(gate.feed_transcript('Сла'))
        self.assertTrue(gate.feed_transcript('вик'))
        self.assertTrue(gate.awake)
        before = gate.awake_until
        gate.feed_transcript('продолжай')
        self.assertGreaterEqual(gate.awake_until, before)
        gate.awake_until = time.monotonic() - 1
        self.assertFalse(gate.awake)

    def test_old_fragments_do_not_wake(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True); gate.feed_transcript('Сла')
        gate.last_input -= 3
        self.assertFalse(gate.feed_transcript('вка'))

    def test_continuous_mode_has_no_timeout_or_wake_requirement(self):
        gate = a.WakeGate(wake_enabled=False)
        gate.awake_until = time.monotonic() - 100
        self.assertTrue(gate.awake)
        self.assertFalse(gate.feed_transcript('Привет, братуха'))
        self.assertTrue(gate.awake)
        self.assertIn('только русский и английский', a.system_instruction())

    def test_wake_timer_expires_and_background_cannot_extend_or_reopen(self):
        with patch.object(a.time, 'monotonic') as clock:
            clock.return_value = 100
            gate = a.WakeGate(wake_enabled=True, followup_enabled=True)
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
        self.assertEqual(config['input_audio_transcription']['language_codes'], ['ru-RU', 'en-US'])
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
    async def test_audio_wake_with_bad_transcription_authorizes_same_packet(self):
        for addressed in (True, False, 'true'):
            gate = a.WakeGate(wake_enabled=True, followup_enabled=True)
            player = FakePlayer()
            queue = asyncio.Queue()
            event = message(input_transcription=NS(text='Валик давай другой трек'))
            event.tool_call = NS(function_calls=[
                NS(name='spotify_command', id='music', args={'action': 'next'}),
                NS(name='wake_assistant', id='wake', args={'addressed': addressed})])
            session = FakeSession([[event, message(b'answer', turn_complete=True)]])
            with patch.dict(a.os.environ, {"SLAVIK_AUDIO_WAKE": "1"}), self.assertRaises(RuntimeError):
                await a.receiver(session, player, gate, queue)
            call, authorized = queue.get_nowait()
            self.assertEqual(call.id, 'music')
            self.assertEqual(authorized, addressed is True)
            self.assertEqual(bool(player.audio), addressed is True)

    async def test_disabled_audio_wake_cannot_authorize_even_if_requested(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True)
        queue = asyncio.Queue()
        event = message(input_transcription=NS(text='Привет зай'))
        event.tool_call = NS(function_calls=[NS(name='wake_assistant', id='wake', args={'addressed': True})])
        with patch.dict(a.os.environ, {'SLAVIK_AUDIO_WAKE': '0'}):
            config = a.make_config()
            self.assertNotIn('wake_assistant', [d.name for d in config.tools[0].function_declarations])
            with self.assertRaises(RuntimeError):
                await a.receiver(FakeSession([[event]]), FakePlayer(), gate, queue)
        self.assertFalse(queue.get_nowait()[1])
        self.assertFalse(gate.awake)

    async def test_local_recording_has_correct_samples_rate_and_permissions(self):
        import tempfile
        import wave
        class Capture:
            def __init__(self, **kwargs): pass
            processor = NS(process=lambda raw: raw)
            async def start(self): pass
            async def close(self): pass
            async def read_stereo(self): return struct.pack('<hh', 11, 22) * a.INPUT_FRAMES
        with tempfile.TemporaryDirectory() as directory, patch.object(a, 'PulseCapture', Capture):
            folder = await a.record_mic(directory, seconds=1)
            for path in folder.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with wave.open(str(path), 'rb') as f:
                    self.assertEqual((f.getframerate(), f.getnchannels(), f.getnframes()), (16000, 2 if path.name == 'physical-stereo.wav' else 1, 16000))
                    self.assertEqual(f.readframes(1), struct.pack('<hh', 11, 22) if path.name == 'physical-stereo.wav' else struct.pack('<h', 22))

    async def test_music_call_alone_cannot_wake_from_bad_transcription(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True)
        player = FakePlayer()
        queue = asyncio.Queue()
        event = message(input_transcription=NS(text='Валик давай другой трек'))
        event.tool_call = NS(function_calls=[NS(name='spotify_command', id='music', args={'action': 'next'})])
        with self.assertRaises(RuntimeError):
            await a.receiver(FakeSession([[event, message(b'answer', turn_complete=True)]]), player, gate, queue)
        self.assertFalse(queue.get_nowait()[1])
        self.assertEqual(player.audio, [])

    async def test_continuous_mode_plays_without_wake_even_with_wrong_transcription(self):
        gate = a.WakeGate(wake_enabled=False); player = FakePlayer()
        session = FakeSession([
            [message(b'reply', input_transcription=NS(text='Salom, bratuha.'), turn_complete=True)],
            [message(b'next', turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [(b'reply', 24000), (b'next', 24000)])
        self.assertEqual(player.finished, 2)

    async def test_background_dropped_multiple_turns_and_no_duplicate_audio(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True); player = FakePlayer()
        fallback = NS(parts=[NS(inline_data=NS(data=b'fallback', mime_type='audio/pcm;rate=24000'))])
        session = FakeSession([
            [message(b'background', turn_complete=True)],
            [message(b'wake', input_transcription=NS(text='Славик'), model_turn=fallback), message(turn_complete=True)],
            [message(b'followup'), message(turn_complete=True)]])
        with self.assertRaisesRegex(RuntimeError, 'ended'):
            await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [(b'wake', 24000)])
        self.assertEqual(player.finished, 1)
        self.assertEqual(session.calls, 4)

    async def test_interrupted_flush_always_and_same_event_transcript(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True); player = FakePlayer()
        session = FakeSession([[message(b'stale', interrupted=True, input_transcription=NS(text='Славик')), message(b'new', turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.flushed, 1)
        self.assertTrue(gate.awake)
        self.assertEqual(player.audio, [(b'new', 24000)])

    async def test_hidden_turn_never_released_after_late_wake(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True); player = FakePlayer()
        session = FakeSession([[message(b'background'), message(b'late', input_transcription=NS(text='Славик')), message(turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [])

    async def test_fallback_pcm(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True); gate.feed_transcript('Славик'); player = FakePlayer()
        part = NS(inline_data=NS(data=b'pcm', mime_type='audio/pcm;rate=48000'))
        session = FakeSession([[message(model_turn=NS(parts=[part]), turn_complete=True)]])
        with self.assertRaises(RuntimeError): await a.receiver(session, player, gate)
        self.assertEqual(player.audio, [(b'pcm', 48000)])

    async def test_authorized_reply_survives_wake_timeout(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True); gate.feed_transcript('Славик'); player = FakePlayer()
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


    async def test_command_started_in_window_can_finish_after_timeout(self):
        gate = a.WakeGate(wake_enabled=True, followup_enabled=True); player = FakePlayer()
        class Session:
            calls = 0
            async def receive(self):
                self.calls += 1
                if self.calls == 1:
                    yield message(input_transcription=NS(text='Славик включи музыку'))
                    gate.awake_until = time.monotonic() - 1
                    yield message(b'reply', turn_complete=True)
                elif self.calls == 2:
                    yield message(b'background', input_transcription=NS(text='неадресованная речь'), turn_complete=True)
        with self.assertRaises(RuntimeError): await a.receiver(Session(), player, gate)
        self.assertEqual(player.audio, [(b'reply', 24000)])
        self.assertFalse(gate.response_allowed)

    async def test_language_hint_rejection_retries_only_initial_setup(self):
        config = a.make_config()
        calls = []
        class Context:
            async def __aenter__(self):
                if len(calls) == 1:
                    raise RuntimeError('1007 Unknown name "language_codes" at setup')
                return 'session'
            async def __aexit__(self, *args): pass
        class Live:
            def connect(self, **args):
                calls.append(args['config'].input_audio_transcription.language_codes)
                return Context()
        client = NS(aio=NS(live=Live()))
        async with a.connect_live(client, config) as session:
            self.assertEqual(session, 'session')
        self.assertEqual(calls, [['ru-RU', 'en-US'], None])

    async def test_idle_interrupt_preserves_pulse_stream(self):
        player = a.PulsePlayer()
        proc = NS(returncode=None)
        player.proc = proc
        with patch.object(a, 'stop_process') as stop:
            await player.flush()
        self.assertIs(player.proc, proc)
        stop.assert_not_called()
        self.assertEqual(player.generation, 1)

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
        player.enqueue(struct.pack('<hh', 1000, -1000))
        writer = asyncio.create_task(player.run())
        await asyncio.sleep(0)
        await asyncio.wait_for(player.flush(), .2)
        await asyncio.sleep(0)
        self.assertEqual(player.generation, 1)
        self.assertTrue(player.queue.empty())
        writer.cancel()
        with self.assertRaises(asyncio.CancelledError): await writer

if __name__ == '__main__': unittest.main()
