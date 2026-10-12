import asyncio
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from music_duck import Ducker
import gemini_assistant_dialog as agent


def stream(index=1, binary='librespot', volume=65536, pid='100'):
    return {'index': index, 'client': 10, 'properties': {
        'application.process.binary': binary, 'application.process.id': pid},
        'volume': {'left': {'value': volume}, 'right': {'value': volume}}}


class DuckTests(unittest.TestCase):
    def test_only_music_is_attenuated_once_and_restored(self):
        calls = []
        d = Ducker(lambda *args: calls.append(args))
        d.update([stream(), stream(2, 'pacat')], True)
        reduced = calls[0][1][0]
        self.assertLess(reduced, 65536)
        d.update([stream(volume=reduced), stream(2, 'pacat')], True)
        self.assertEqual(len(calls), 1)
        d.update([stream(volume=reduced)], False)
        self.assertEqual(calls[-1], (1, [65536, 65536]))

    def test_crash_recovery_prevents_repeated_attenuation(self):
        import tempfile
        from pathlib import Path
        from music_duck import StateStore
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.json')
            calls = []
            first = Ducker(lambda *args: calls.append(args), store)
            first.update([stream()], True)
            reduced = calls[-1][1][0]
            # Simulate process loss: no cleanup; a new process reloads its journal.
            recovered = Ducker(lambda *args: calls.append(args), store)
            recovered.update([stream(volume=reduced)], False)
            self.assertEqual(calls[-1], (1, [65536, 65536]))
            recovered.update([stream()], True)
            self.assertEqual(calls[-1], (1, [reduced, reduced]))

    def test_manual_volume_and_reused_stream_id_are_not_overwritten(self):
        for replacement in (stream(volume=20000), stream(pid='200')):
            calls = []
            d = Ducker(lambda *args: calls.append(args))
            d.update([stream()], True)
            d.update([replacement], False)
            self.assertEqual(len(calls), 1)

    def test_music_started_during_conversation_is_ducked(self):
        calls = []
        d = Ducker(lambda *args: calls.append(args))
        d.update([], True)
        d.update([stream(binary='slavik-librespot')], True)
        self.assertEqual(len(calls), 1)

    def test_observed_wake_variant_requires_command_context(self):
        for text in ('Слайк, привет', 'Слайка, включи Кизару', 'Громкость десять, Слайк'):
            self.assertTrue(agent.contains_wake(text))
        for text in ('Слайды сделай погромче', 'Валик включи', 'Слайк', 'рассказываю про слайка'):
            self.assertFalse(agent.contains_wake(text))


class FocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_audible_reply_keeps_followup_window_open(self):
        gate = agent.WakeGate()
        gate.awake_until = 0
        player = SimpleNamespace(queue=asyncio.Queue(), audio_until=1e30)
        with patch.dict(agent.os.environ, {'SLAVIK_MUSIC_DUCK': '0'}), patch.object(agent.asyncio, 'sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await agent.conversation_focus(gate, player)
        self.assertTrue(gate.awake)


    async def test_music_confirmation_closes_only_after_audio_drains(self):
        gate = agent.WakeGate()
        gate.feed_transcript('Славик включи музыку')
        gate.music_finish_pending = True
        gate.complete_turn()
        player = SimpleNamespace(queue=asyncio.Queue(), audio_until=1e30)
        ticks = []
        async def tick(_):
            ticks.append(gate.awake)
            if len(ticks) == 1:
                player.audio_until = 0
            else:
                raise asyncio.CancelledError
        with patch.dict(agent.os.environ, {'SLAVIK_MUSIC_DUCK': '0'}), patch.object(agent.asyncio, 'sleep', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await agent.conversation_focus(gate, player)
        self.assertEqual(ticks, [True, False])
        self.assertFalse(gate.addressed)
        gate.close_when_drained = True
        gate.feed_transcript('Славик говори тише')
        self.assertFalse(gate.close_when_drained)

    async def test_duck_requires_address_and_restores_after_window(self):
        gate = agent.WakeGate()
        player = SimpleNamespace(queue=asyncio.Queue(), audio_until=0)
        writes = []
        stdin = SimpleNamespace(write=writes.append, drain=AsyncMock(), close=lambda: None)
        proc = SimpleNamespace(stdin=stdin, returncode=None, wait=AsyncMock(return_value=0))
        count = 0
        async def tick(_):
            nonlocal count
            count += 1
            if count == 1:
                gate.feed_transcript('Славик, привет')
            elif count == 2:
                gate.awake_until = 0
                gate.command_authorized = False
            else:
                raise asyncio.CancelledError
        capture = SimpleNamespace(near_speech=SimpleNamespace(until=1e30))
        with patch.dict(agent.os.environ, {'SLAVIK_MUSIC_DUCK': '1', 'SLAVIK_AUDIO_HELPER': '/test', 'SLAVIK_EARLY_DUCK': '1'}), \
             patch.object(agent.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=proc)), \
             patch.object(agent.asyncio, 'sleep', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await agent.conversation_focus(gate, player, capture)
        self.assertEqual(writes, [b'off\n', b'on\n', b'off\n'])
        self.assertFalse(gate.addressed)


class ServiceTests(unittest.TestCase):
    def test_eof_restores_music_after_active_session(self):
        import json
        import music_duck
        calls = []
        current = [65536]
        def pulse(command, **kwargs):
            calls.append(command)
            if 'list' in command:
                return SimpleNamespace(stdout=json.dumps([stream(volume=current[0])]))
            current[0] = int(command[-1])
            return SimpleNamespace(stdout='')
        with patch.object(music_duck.subprocess, 'run', side_effect=pulse), \
             patch.object(music_duck.select, 'select', return_value=([music_duck.sys.stdin], [], [])), \
             patch.object(music_duck.os, 'read', side_effect=[b'on\n', b'']), \
             patch.object(music_duck.signal, 'signal'):
            music_duck._serve({}, None)
        changes = [c for c in calls if 'set-sink-input-volume' in c]
        self.assertEqual(len(changes), 2)
        self.assertLess(int(changes[0][-1]), 65536)
        self.assertEqual(current[0], 65536)


class OutputMigrationTests(unittest.TestCase):
    def test_master_becomes_unity_without_attenuating_music(self):
        import tempfile
        import json
        import music_duck
        sink = {'name': 'voice_aec_sink', 'index': 41, 'volume': {
            'left': {'value': 26214}, 'right': {'value': 26214}}}
        music = stream()
        music['sink'] = 41
        calls = []
        def pulse(command, **kwargs):
            calls.append(command)
            if command[-1] == 'sinks':
                return SimpleNamespace(stdout=json.dumps([sink]))
            if command[-1] == 'sink-inputs':
                return SimpleNamespace(stdout=json.dumps([music]))
            return SimpleNamespace(stdout='')
        with tempfile.TemporaryDirectory() as directory, patch.object(music_duck.subprocess, 'run', side_effect=pulse):
            music_duck.prepare_output({'XDG_RUNTIME_DIR': directory})
        self.assertFalse(any('set-sink-input-volume' in c for c in calls))
        self.assertEqual(calls[-1][-3:], ['set-sink-volume', 'voice_aec_sink', '100%'])

    def test_unity_master_does_not_attenuate_music_on_each_restart(self):
        import tempfile
        import json
        import music_duck
        sink = {'name': 'voice_aec_sink', 'volume': {'left': {'value': 65536}, 'right': {'value': 65536}}}
        with tempfile.TemporaryDirectory() as directory, patch.object(music_duck.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps([sink]))) as pulse:
            music_duck.prepare_output({'XDG_RUNTIME_DIR': directory})
        self.assertEqual(pulse.call_count, 1)


class HardwareOutputTests(unittest.TestCase):
    def test_zero_headphone_and_attenuated_physical_sink_are_repaired_without_capture_change(self):
        import tempfile
        import json
        import music_duck
        sink = {'name': 'alsa_output.platform-sound_i2s.stereo-fallback',
                'base_volume': {'value': 52057}, 'volume': {'left': {'value': 26214}}}
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[-1] == 'sinks':
                return SimpleNamespace(stdout=json.dumps([sink]))
            if command[-1] == 'info':
                return SimpleNamespace(stdout='Card WM8960-2Mic')
            return SimpleNamespace(stdout='Headphone:0')
        with tempfile.TemporaryDirectory() as directory, patch.object(music_duck.subprocess, 'run', side_effect=run):
            music_duck.restore_hardware_output({'XDG_RUNTIME_DIR': directory})
        self.assertIn(['/usr/bin/amixer', '-c', 'WM89602Mic', 'sset', 'Headphone', '100%'], calls)
        self.assertIn(['/usr/bin/pactl', 'set-sink-volume', sink['name'], '65536'], calls)
        self.assertFalse(any('Capture' in c or 'set-source-volume' in c for c in calls))

    def test_missing_card_or_sink_never_changes_levels(self):
        import music_duck
        with patch.object(music_duck.subprocess, 'run', return_value=SimpleNamespace(stdout='[]')) as run:
            with self.assertRaisesRegex(RuntimeError, 'physical sink'):
                music_duck.restore_hardware_output({})
        self.assertEqual(run.call_count, 1)

    def test_correct_output_does_not_rewrite_mixer_or_pulse_levels(self):
        import tempfile
        import json
        import music_duck
        sinks = [{'name': 'alsa_output.platform-sound_i2s.stereo-fallback',
                  'base_volume': {'value': 52057}, 'volume': {'left': {'value': 65536}, 'right': {'value': 65536}}, 'mute': False},
                 {'name': 'voice_aec_sink', 'mute': False}]
        mixer = ''.join("Simple mixer control '%s',0\n Front Left [100%%] [6.00dB]\n Front Right [100%%] [6.00dB]\n" % name for name in ('Headphone', 'Speaker', 'Playback'))
        mixer += ''.join("Simple mixer control '%s',0\n Playback [on]\n" % name for name in ('Left Output Mixer PCM', 'Right Output Mixer PCM'))
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(stdout=json.dumps(sinks) if command[-1] == 'sinks' else 'WM8960' if command[-1] == 'info' else mixer)
        with tempfile.TemporaryDirectory() as directory, patch.object(music_duck.subprocess, 'run', side_effect=run):
            music_duck.restore_hardware_output({'XDG_RUNTIME_DIR': directory})
        self.assertFalse(any('sset' in c or 'set-sink-volume' in c or 'set-sink-mute' in c for c in calls))
