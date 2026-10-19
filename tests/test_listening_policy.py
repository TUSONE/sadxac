import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import gemini_assistant_dialog as agent
from music_duck import Ducker, music_playing
from test_duck import stream
from test_agent import FakePlayer, FakeSession, message


class MusicStateTests(unittest.TestCase):
    def test_active_paused_muted_zero_and_voice_streams(self):
        playing = {**stream(), 'corked': False, 'mute': False}
        self.assertTrue(music_playing([playing]))
        for changed in ({'corked': True}, {'mute': True},
                        {'volume': {'left': {'value': 0}, 'right': {'value': 0}}},
                        {'properties': {'application.name': 'SlavikVoice'}}):
            self.assertFalse(music_playing([{**playing, **changed}]), changed)
        self.assertFalse(music_playing([stream()]))  # unknown is not playing

    def test_listen_silence_speech_duck_restore_and_state(self):
        calls = []
        ducker = Ducker(lambda *args: calls.append(args))
        playing = {**stream(), 'corked': False}
        ducker.update([playing, stream(2, 'pacat')], True, listening=True)
        self.assertEqual(calls, [(1, [0, 0])])
        silent = {**stream(volume=0), 'corked': False}
        self.assertTrue(music_playing([silent], ducker.saved))
        ducker.update([silent], True, listening=True)
        self.assertEqual(len(calls), 1)
        ducker.update([silent], True)
        reduced = calls[-1][1][0]
        self.assertEqual(reduced, round(65536 * .8))
        ducker.update([stream(volume=reduced)], False)
        self.assertEqual(calls[-1], (1, [65536, 65536]))

    def test_manual_volume_wins_over_listen_to_speech_transition(self):
        calls = []
        ducker = Ducker(lambda *args: calls.append(args))
        ducker.update([stream()], True, listening=True)
        ducker.update([stream(volume=12345)], True)
        ducker.update([stream(volume=12345)], False)
        self.assertEqual(calls, [(1, [0, 0])])


class ListeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_without_music_can_acknowledge_but_music_name_is_silent(self):
        for music in (False, True):
            with self.subTest(music=music), patch.object(agent.time, 'monotonic', return_value=100):
                gate, player = agent.WakeGate(), FakePlayer()
                gate.set_music_state(music)
                with self.assertRaises(RuntimeError):
                    await agent.receiver(FakeSession([[message(b'ack', input_transcription=NS(text='Славик'), turn_complete=True)]]), player, gate)
                self.assertEqual(bool(player.audio), not music)
                self.assertEqual(gate.waiting_for_command, music)

    async def test_stale_music_status_does_not_silently_swallow_name(self):
        with patch.object(agent.time, 'monotonic', return_value=100) as clock:
            gate = agent.WakeGate()
            gate.set_music_state(True)
            clock.return_value = 104
            gate.feed_transcript('Славка')
            self.assertFalse(gate.waiting_for_command)
            self.assertTrue(gate.response_allowed)

    async def test_audio_based_bare_name_waits_without_authorizing_music_action(self):
        with patch.object(agent.time, 'monotonic', return_value=100):
            gate, player, queue = agent.WakeGate(), FakePlayer(), asyncio.Queue()
            gate.set_music_state(True)
            event = message(input_transcription=NS(text='Вайк'))
            event.tool_call = NS(function_calls=[
                NS(name='wake_assistant', id='wake', args={'addressed': True, 'command_present': False}),
                NS(name='spotify_command', id='skip', args={'action': 'next'})])
            with self.assertRaises(RuntimeError):
                await agent.receiver(FakeSession([[event, message(b'hidden', turn_complete=True)]]), player, gate, queue)
            self.assertTrue(gate.waiting_for_command)
            self.assertFalse(queue.get_nowait()[1])
            self.assertFalse(queue.get_nowait()[1])
            self.assertEqual(player.audio, [])

    async def test_full_followup_after_sound_despite_rejected_echo(self):
        gate = agent.WakeGate()
        player = NS(queue=asyncio.Queue(), audio_until=110)
        with patch.object(agent.time, 'monotonic', return_value=100) as clock:
            gate.feed_transcript('Славик расскажи про Фога')
            gate.reply_played = True
            gate.complete_turn()
            # Receiver rejected an echo while the actual authorized voice still played.
            gate.addressed = False
            gate.awake_until = 0
            ticks = 0
            async def tick(_):
                nonlocal ticks
                ticks += 1
                if ticks == 1:
                    clock.return_value = 111
                else:
                    self.assertEqual(gate.awake_until, 114)
                    self.assertTrue(gate.addressed)
                    self.assertFalse(gate.response_allowed)
                    guard = agent.EchoGuard()
                    guard.remember('Это Фог')
                    self.assertEqual(guard.accept('А кто такой Друн?', player, allow_barge_in=gate.addressed and gate.awake), 'А кто такой Друн?')
                    clock.return_value = 113.9
                    gate.feed_transcript('Расскажи про Друна')
                    self.assertTrue(gate.response_allowed)
                    raise asyncio.CancelledError
            with patch.dict(agent.os.environ, {'SLAVIK_MUSIC_DUCK': '0'}), patch.object(agent.asyncio, 'sleep', side_effect=tick):
                with self.assertRaises(asyncio.CancelledError):
                    await agent.conversation_focus(gate, player)

    async def test_audio_command_can_complete_a_partial_transcribed_name(self):
        gate, player, queue = agent.WakeGate(), FakePlayer(), asyncio.Queue()
        with patch.object(agent.time, 'monotonic', return_value=100):
            gate.set_music_state(True)
            event = message(input_transcription=NS(text='Славик'))
            # Audio already contains a full request, although ASR has only emitted the name.
            event.tool_call = NS(function_calls=[
                NS(name='spotify_command', id='pause', args={'action': 'pause'}),
                NS(name='wake_assistant', id='wake', args={'addressed': True, 'command_present': True})])
            with self.assertRaises(RuntimeError):
                await agent.receiver(FakeSession([[event, message(b'confirmation', turn_complete=True)]]), player, gate, queue)
        self.assertTrue(queue.get_nowait()[1])
        self.assertTrue(queue.get_nowait()[1])
        self.assertFalse(gate.waiting_for_command)
        self.assertEqual(player.audio, [(b'confirmation', 24000)])

    async def test_status_reader_ignores_other_lines_and_clears_state_at_eof(self):
        gate = agent.WakeGate()
        reader = asyncio.StreamReader()
        proc = NS(stdout=reader)
        task = asyncio.create_task(agent.music_state_reader(proc, gate))
        reader.feed_data(b'music:on\nnot-a-status\n')
        await asyncio.sleep(0)
        self.assertTrue(gate.music_playing)
        reader.feed_data(b'music:off\n')
        await asyncio.sleep(0)
        self.assertFalse(gate.music_playing)
        reader.feed_data(b'music:on\n')
        await asyncio.sleep(0)
        self.assertTrue(gate.music_playing)
        reader.feed_eof()
        await task
        self.assertFalse(gate.music_playing)

    async def test_listen_helper_restores_on_eof(self):
        import music_duck
        current = [65536]
        statuses = []
        def pulse(command, **kwargs):
            if 'list' in command:
                return NS(stdout=json.dumps([{**stream(volume=current[0]), 'corked': False}]))
            current[0] = int(command[-1])
            return NS(stdout='')
        with patch.object(music_duck.subprocess, 'run', side_effect=pulse), \
             patch.object(music_duck.select, 'select', return_value=([music_duck.sys.stdin], [], [])), \
             patch.object(music_duck.os, 'read', side_effect=[b'listen\n', b'']), \
             patch.object(music_duck.signal, 'signal'), patch('builtins.print', side_effect=lambda *a, **k: statuses.append(a[0])):
            music_duck._serve({}, None)
        self.assertEqual(current[0], 65536)
        self.assertEqual(statuses, ['music:on', 'music:on'])

    async def test_old_generation_does_not_reopen_expired_music_name_wait(self):
        class Player(FakePlayer):
            async def put(self, data, rate):
                await super().put(data, rate)
                self.audio_until = 110
            async def flush(self):
                await super().flush()
                self.audio_until = 0
        player = Player()
        player.queue, player.audio_until = asyncio.Queue(), 0
        clock_value = 100
        gate = agent.WakeGate()
        class Session:
            calls = 0
            async def receive(self):
                nonlocal clock_value
                self.calls += 1
                if self.calls != 1:
                    return
                yield message(b'old', input_transcription=NS(text='Славик расскажи сказку'))
                clock_value = 101
                gate.set_music_state(True)
                yield message(input_transcription=NS(text='Славка'))
                clock_value = 105
                yield message(b'stale', turn_complete=True)
        with patch.object(agent.time, 'monotonic', side_effect=lambda: clock_value), self.assertRaises(RuntimeError):
            await agent.receiver(Session(), player, gate)
        self.assertEqual(player.flushed, 1)
        self.assertFalse(gate.awake)
        self.assertFalse(gate.response_allowed)
        self.assertFalse(gate.reply_played)


class OutputHeadroomTests(unittest.TestCase):
    def test_positive_analogue_gain_is_capped_but_negative_gain_and_mic_preserved(self):
        import tempfile
        import music_duck
        physical = 'alsa_output.platform-sound_i2s.stereo-fallback'
        sinks = [{'name': physical, 'volume': {'left': {'value': 39322}, 'right': {'value': 39322}}, 'mute': False}]
        mixer = ""
        for name, percent, db in [('Headphone', 100, '6.00'), ('Speaker', 82, '-17.00'), ('Playback', 100, '0.00')]:
            mixer += f"Simple mixer control '{name}',0\n Left [{percent}%] [{db}dB]\n Right [{percent}%] [{db}dB]\n"
        for name in ('Left Output Mixer PCM', 'Right Output Mixer PCM'):
            mixer += f"Simple mixer control '{name}',0\n Playback [on]\n"
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return NS(stdout=json.dumps(sinks) if command[-1] == 'sinks' else 'WM8960' if command[-1] == 'info' else mixer)
        with tempfile.TemporaryDirectory() as directory, patch.object(music_duck.subprocess, 'run', side_effect=run):
            music_duck.restore_hardware_output({'XDG_RUNTIME_DIR': directory})
        self.assertIn(['/usr/bin/amixer', '-c', 'WM89602Mic', 'sset', 'Headphone', '0dB'], calls)
        self.assertIn(['/usr/bin/pactl', 'set-sink-volume', physical, '39322'], calls)
        self.assertFalse(any('sset' in c and 'Speaker' in c for c in calls))
        self.assertFalse(any('Capture' in c or 'set-source-volume' in c for c in calls))
