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
            music_duck.serve({})
        changes = [c for c in calls if 'set-sink-input-volume' in c]
        self.assertEqual(len(changes), 2)
        self.assertLess(int(changes[0][-1]), 65536)
        self.assertEqual(current[0], 65536)
