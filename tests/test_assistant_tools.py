import asyncio
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
import assistant_tools as tools
import gemini_assistant_dialog as agent
import spotify_api as spotify
import mic_processing as mic


class OutcomeTests(unittest.TestCase):
    def test_pause_and_skip_acceptance_never_become_failure(self):
        for action in ('pause', 'next', 'previous'):
            outcome = tools.tool_outcome('Spotify принял команду: ' + action + '. Состояние прочитать не удалось; это не доказывает отказ команды.', action)
            self.assertEqual(outcome['status'], 'accepted')
            self.assertNotIn('не удалось', outcome['spoken_response'])
            self.assertFalse(outcome['repeat_action'])
        confirmed = tools.tool_outcome('Ответ на команду потерян; Подтверждено: пауза.', 'pause')
        self.assertEqual(confirmed['spoken_response'], 'Поставил на паузу.')
        self.assertEqual(tools.tool_outcome('Результат не подтверждён: timeout')['status'], 'unknown')
        self.assertEqual(tools.tool_outcome('Не выполнено: Spotify HTTP 403')['status'], 'failed')

    def test_weather_needs_city_and_returns_timed_data(self):
        with patch.dict(os.environ, {'SLAVIK_CITY': ''}, clear=True), patch.object(tools, 'get_json') as fetch:
            self.assertEqual(tools.weather({})['status'], 'needs_input')
            fetch.assert_not_called()
        with patch.object(tools, 'get_json', side_effect=[{'results': [{'name': 'Ташкент', 'latitude': 41, 'longitude': 69}]},
                {'timezone': 'Asia/Tashkent', 'current': {'time': '2026-10-12T13:00', 'temperature_2m': 20},
                 'daily': {'time': ['today', 'tomorrow'], 'temperature_2m_max': [21, 22]}}]):
            result = tools.weather({'city': 'Ташкент', 'day': 1})
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['forecast']['temperature_2m_max'], 22)
        self.assertIsNone(result['current'])
        with patch.object(tools, 'get_json', side_effect=TimeoutError):
            self.assertEqual(tools.weather({'city': 'Ташкент'})['status'], 'failed')

    def test_tashkent_default_and_explicit_city_override(self):
        for args, expected in [({}, 'Ташкент'), ({'city': 'Москва'}, 'Москва')]:
            with patch.dict(os.environ, {}, clear=True), patch.object(tools, 'get_json', return_value={'results': []}) as fetch:
                tools.weather(args)
            self.assertEqual(fetch.call_args.args[1]['name'], expected)

    def test_curated_queue_validates_both_artist_and_title_and_deduplicates(self):
        results = {'tracks': {'items': [
            {'uri': 'wrong', 'name': 'Song', 'artists': [{'name': 'Wrong Artist'}]},
            {'uri': 'correct', 'name': 'Song', 'artists': [{'name': 'kizaru'}]}]}}
        with patch.object(spotify, 'api', return_value=results):
            uris, names, missing = spotify.related_queue([
                {'title': 'Song', 'artist': 'Кизару'}, {'title': 'Song', 'artist': 'Кизару'},
                {'title': 'Nonexistent', 'artist': 'Кизару'}], 'seed')
        self.assertEqual(uris, ['seed', 'correct'])
        self.assertEqual(missing, 1)
        with patch.object(spotify, 'api', side_effect=spotify.SpotifyError('Spotify HTTP 403')):
            self.assertEqual(spotify.related_queue([{'title': 'Song', 'artist': 'X'}], 'seed')[0], ['seed'])

    def test_play_builds_one_queue_and_keeps_seed_when_related_search_fails(self):
        for related, expected in [((["seed", "similar"], ["Similar"], 0), ["seed", "similar"]),
                                  ((["seed"], [], 1), ["seed"])]:
            with patch.object(spotify, 'device_id', return_value='target'), \
                 patch.object(spotify, 'search_uri', return_value=('seed', 'Requested song')), \
                 patch.object(spotify, 'related_queue', return_value=related), \
                 patch.object(spotify, 'api') as api:
                result = spotify.spotify_command({'action': 'play_track', 'query': 'Requested song',
                                                  'related_tracks': [{'title': 'Similar', 'artist': 'X'}]})
            api.assert_called_once_with('PUT', '/me/player/play', {'uris': expected}, {'device_id': 'target'})
            self.assertEqual(tools.tool_outcome(result, 'play_track')['status'], 'accepted')

    def test_new_wake_names_and_close_pronunciation(self):
        for text in ('Слава', 'Славчуха', 'Как дела, Славуха?', 'Слайвик привет', 'Славика включи музыку', 'Свавик поставь паузу'):
            self.assertTrue(agent.contains_wake(text), text)
        for text in ('Слова этой песни', 'Валик привет', 'Слайды покажи', 'славиковский'):
            self.assertFalse(agent.contains_wake(text), text)

    def test_channel_selection_uses_snr_not_louder_music(self):
        def frames(rms):
            return [{'rms': rms, 'peak': rms * 3, 'clipped': 0, 'count': 640}] * 75
        profile = mic.best_calibration([frames(20), frames(500)], [frames(1000), frames(2000)], 'aec')
        self.assertEqual(profile['channel'], 0)
        with patch.object(mic, 'calibration', side_effect=[RuntimeError('clipping'), {'snr_db': 15}]):
            self.assertEqual(mic.best_calibration([[], []], [[], []], 'aec')['channel'], 1)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_end_unducks_without_waiting_for_followup_window(self):
        gate = agent.WakeGate()
        gate.feed_transcript('Слава привет')
        gate.complete_turn()
        player = NS(queue=asyncio.Queue(), audio_until=1e30)
        writes = []
        proc = NS(returncode=None, stdin=NS(write=writes.append, drain=None, close=lambda: None))
        async def drain(): pass
        async def wait(): return 0
        proc.stdin.drain, proc.wait = drain, wait
        count = 0
        async def tick(_):
            nonlocal count
            count += 1
            if count == 1:
                player.audio_until = 0
            else:
                self.assertTrue(gate.awake)
                raise asyncio.CancelledError
        with patch.dict(os.environ, {'SLAVIK_AUDIO_HELPER': '/test', 'SLAVIK_MUSIC_DUCK': '1'}), \
             patch.object(agent.asyncio, 'create_subprocess_exec', return_value=proc), \
             patch.object(agent.asyncio, 'sleep', side_effect=tick):
            with self.assertRaises(asyncio.CancelledError):
                await agent.conversation_focus(gate, player)
        self.assertEqual(writes, [b'on\n', b'off\n'])

    async def test_duplicate_tool_id_does_not_skip_two_tracks(self):
        queue = asyncio.Queue()
        call = NS(id='skip-1', name='spotify_command', args={'action': 'next'})
        queue.put_nowait((call, True))
        queue.put_nowait((call, True))
        calls, responses = [], []
        async def send(**kwargs): responses.append(kwargs)
        task = asyncio.create_task(agent.tool_worker(NS(send_tool_response=send), queue,
            lambda args: calls.append(args) or 'Spotify принял команду: next.'))
        await asyncio.wait_for(queue.join(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]['function_responses'][0].response['status'], 'accepted')

    async def test_grounded_search_requires_sources(self):
        from unittest.mock import AsyncMock
        from google import genai
        grounded = NS(text='Свежий ответ', candidates=[NS(grounding_metadata=NS(
            grounding_chunks=[NS(web=NS(uri='https://example.com/fact', title='Fact'))]))])
        for response, expected in [(grounded, 'confirmed'), (NS(text='Догадка', candidates=[]), 'unknown')]:
            client = NS(aio=NS(models=NS(generate_content=AsyncMock(return_value=response)), aclose=AsyncMock()))
            with patch.object(genai, 'Client', return_value=client):
                result = await tools.web_search({'query': 'свежий факт'})
            self.assertEqual(result['status'], expected)
            client.aio.aclose.assert_awaited_once()

    async def test_capture_honors_calibrated_left_channel(self):
        import struct
        from unittest.mock import AsyncMock
        with patch.object(agent, 'load_processor', return_value=NS(channel=0, process=lambda raw: raw)):
            capture = agent.PulseCapture()
        capture.read_stereo = AsyncMock(return_value=struct.pack('<hhhh', 11, 22, 33, 44))
        self.assertEqual(struct.unpack('<hh', await capture.read_mono()), (11, 33))

    async def test_echo_and_unrelated_foreign_transcript_cannot_start_self_dialogue(self):
        from test_agent import FakePlayer, FakeSession, message
        gate = agent.WakeGate(wake_enabled=True)
        player = FakePlayer()
        session = FakeSession([
            [message(b'first', input_transcription=NS(text='Славик привет'),
                     output_transcription=NS(text='Может что-нибудь обсудим?'), turn_complete=True)],
            [message(input_transcription=NS(text='Может что-нибудь'), interrupted=True),
             message(b'echo_reply', output_transcription=NS(text='Давай поговорим!'), turn_complete=True)],
            [message(input_transcription=NS(text='Will I switch it?')),
             message(b'foreign_reply', turn_complete=True)],
        ])
        with self.assertRaises(RuntimeError):
            await agent.receiver(session, player, gate)
        self.assertEqual(player.finished, 1)
        self.assertEqual(player.audio, [(b'first', 24000)])  # preserve audio; no echo reply played
        self.assertFalse(gate.command_authorized)

    async def test_followup_requires_fresh_input_and_echo_guard_allows_addressed_barge_in(self):
        guard = agent.EchoGuard()
        player = NS(queue=asyncio.Queue(), audio_until=150)
        with patch.object(agent.time, 'monotonic', return_value=100):
            guard.remember('Может что-нибудь обсудим?')
            self.assertIsNone(guard.accept('Может что-нибудь', player))
            self.assertIsNone(guard.accept('Will I switch it?', player))
            self.assertEqual(guard.accept('Славка, поставь паузу', player), 'Славка, поставь паузу')
            gate = agent.WakeGate(wake_enabled=True, followup_enabled=True)
            gate.feed_transcript('Славик привет')
            gate.complete_turn()
            self.assertTrue(gate.awake)
            self.assertFalse(gate.response_allowed)
        player.audio_until = 0
        with patch.object(agent.time, 'monotonic', return_value=103):
            self.assertEqual(guard.accept('Как дела?', player), 'Как дела?')
            gate.feed_transcript('Как дела?')
            self.assertTrue(gate.response_allowed)

    async def test_experimental_audio_wake_cannot_bypass_rejected_echo(self):
        from test_agent import FakePlayer, FakeSession, message
        gate, player, queue = agent.WakeGate(wake_enabled=True), FakePlayer(), asyncio.Queue()
        echoed = message(input_transcription=NS(text='Может что-нибудь'))
        echoed.tool_call = NS(function_calls=[NS(name='wake_assistant', id='wake', args={'addressed': True}),
                                              NS(name='spotify_command', id='skip', args={'action': 'next'})])
        session = FakeSession([[message(b'reply', input_transcription=NS(text='Славик привет'),
                                        output_transcription=NS(text='Может что-нибудь обсудим?'), turn_complete=True)], [echoed]])
        with patch.dict(os.environ, {'SLAVIK_AUDIO_WAKE': '1'}), self.assertRaises(RuntimeError):
            await agent.receiver(session, player, gate, queue)
        self.assertFalse(queue.get_nowait()[1])
        self.assertFalse(queue.get_nowait()[1])

    async def test_delayed_altered_echo_from_user_log_cannot_authorize_reply(self):
        from test_agent import FakePlayer, FakeSession, message
        gate, player = agent.WakeGate(wake_enabled=True, followup_enabled=False), FakePlayer()
        class DelayedSession(FakeSession):
            async def receive(self):
                self.calls += 1
                if self.calls == 1:
                    yield message(b'hello', input_transcription=NS(text='Славик привет'),
                                  output_transcription=NS(text='Привет! Что хотел?'), turn_complete=True)
                elif self.calls == 2:
                    clock.return_value = 105  # beyond the old 1.2-second guard
                    yield message(input_transcription=NS(text='Привет. Что с'), interrupted=True)
                    yield message(b'self_reply', turn_complete=True)
                elif self.calls == 3:
                    clock.return_value = 110
                    yield message(input_transcription=NS(text='Ja fein feine'))
                    yield message(b'foreign_reply', turn_complete=True)
        with patch.object(agent.time, 'monotonic') as clock:
            clock.return_value = 100
            with self.assertRaises(RuntimeError):
                await agent.receiver(DelayedSession([]), player, gate)
        self.assertEqual(player.finished, 1)
        self.assertFalse(gate.response_allowed)

    async def test_strict_request_fragments_end_when_reply_begins(self):
        from test_agent import FakePlayer, FakeSession, message
        gate, player = agent.WakeGate(wake_enabled=True, followup_enabled=False), FakePlayer()
        gate.feed_transcript('Славик')
        gate.feed_transcript('включи Кизару')
        self.assertTrue(gate.response_allowed)
        with self.assertRaises(RuntimeError):
            await agent.receiver(FakeSession([[message(b'reply', turn_complete=True)]]), player, gate)
        gate.feed_transcript('Могу найти')
        self.assertFalse(gate.response_allowed)
        gate.feed_transcript('Славка поставь паузу')
        self.assertTrue(gate.response_allowed)
