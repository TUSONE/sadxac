import asyncio
import io
import struct
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import gemini_assistant_dialog as agent
import assistant_tools as tools


class InformationTests(unittest.TestCase):
    def test_tashkent_is_uzbekistan_without_ambiguous_geocoding(self):
        forecast = {'timezone': 'Asia/Tashkent', 'current': {'temperature_2m': 20},
                    'daily': {'time': ['2026-10-19'], 'temperature_2m_max': [21]}}
        with patch.object(tools, 'get_json', return_value=forecast) as fetch:
            result = tools.weather({'city': 'Ташкент'})
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['country'], 'Узбекистан')
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], 'https://api.open-meteo.com/v1/forecast')

    def test_explicit_country_is_respected_for_a_namesake(self):
        forecast = {'timezone': 'Europe/Moscow', 'current': {}, 'daily': {'time': ['2026-10-19']}}
        places = {'results': [{'name': 'Ташкент', 'country': 'Узбекистан', 'country_code': 'UZ'},
                              {'name': 'Ташкент', 'country': 'Россия', 'country_code': 'RU', 'latitude': 55, 'longitude': 37}]}
        with patch.object(tools, 'get_json', side_effect=[places, forecast]) as fetch:
            result = tools.weather({'city': 'Ташкент', 'country': 'Россия'})
        self.assertEqual(result['country'], 'Россия')
        self.assertEqual(fetch.call_args.args[1]['latitude'], 55)

    def test_news_uses_real_titles_dates_and_sources(self):
        rss = b'''<rss><channel><item><title>Tesla update</title><link>https://news.example/story</link><pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate><source>Publisher</source></item></channel></rss>'''
        with patch.object(tools, 'urlopen', return_value=io.BytesIO(rss)) as fetch:
            result = tools.news_headlines('последние новости о Tesla')
        self.assertEqual(result['headlines'][0]['publisher'], 'Publisher')
        self.assertIn('2026', result['headlines'][0]['published'])
        self.assertEqual(result['sources'][0]['url'], 'https://news.example/story')
        self.assertIn('Tesla', fetch.call_args.args[0])
        self.assertNotIn('последние', fetch.call_args.args[0])


class DialogueTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_three_second_window_starts_at_audible_end(self):
        gate = agent.WakeGate(followup_enabled=True)
        player = NS(queue=asyncio.Queue(), audio_until=110)
        count = 0
        with patch.object(agent.time, 'monotonic') as clock:
            clock.return_value = 100
            gate.feed_transcript('Славик привет')
            gate.complete_turn()
            async def tick(_):
                nonlocal count
                count += 1
                if count == 1:
                    clock.return_value = 111
                else:
                    self.assertEqual(gate.awake_until, 114)
                    clock.return_value = 113.9
                    gate.feed_transcript('Расскажи подробнее')
                    self.assertTrue(gate.response_allowed)
                    gate.complete_turn()
                    clock.return_value = 117
                    gate.feed_transcript('Посторонняя речь')
                    self.assertFalse(gate.response_allowed)
                    raise asyncio.CancelledError
            with patch.dict(agent.os.environ, {'SLAVIK_MUSIC_DUCK': '0'}), patch.object(agent.asyncio, 'sleep', side_effect=tick):
                with self.assertRaises(asyncio.CancelledError):
                    await agent.conversation_focus(gate, player)

    async def test_expected_short_answer_is_not_mistaken_for_own_question(self):
        guard = agent.EchoGuard()
        player = NS(queue=asyncio.Queue(), audio_until=0)
        guard.remember('В Узбекистане или России?')
        self.assertEqual(guard.accept('В Узбекистане', player, allow_short_answer=True), 'В Узбекистане')
        self.assertIsNone(guard.accept('В Узбекистане', player))

    async def test_longer_than_old_ten_second_queue_does_not_overflow(self):
        player = agent.PulsePlayer()
        await player.enqueue(b'\0' * (3840 * 700))
        self.assertEqual(player.queue.qsize(), 700)

    async def test_full_queue_applies_backpressure_without_dropping_audio(self):
        player = agent.PulsePlayer()
        player.queue = asyncio.Queue(maxsize=2)
        pcm = b'\0' * (3840 * 5)
        producer = asyncio.create_task(player.enqueue(pcm))
        await asyncio.sleep(0)
        self.assertFalse(producer.done())
        collected = []
        for _ in range(5):
            collected.append((await asyncio.wait_for(player.queue.get(), 1))[1])
            player.queue.task_done()
        await producer
        self.assertEqual(b''.join(collected), pcm)

    async def test_addressed_barge_in_discards_old_reply_and_preserves_new_command(self):
        from test_agent import FakePlayer, FakeSession, message
        class Player(FakePlayer):
            def __init__(self):
                super().__init__()
                self.queue = asyncio.Queue()
                self.audio_until = 0
                self.history = []
            async def put(self, data, rate):
                await super().put(data, rate)
                self.history.append(data)
                self.audio_until = 105
            async def flush(self):
                await super().flush()
                self.audio_until = 0
        player, gate, queue = Player(), agent.WakeGate(), asyncio.Queue()
        tool_event = message()
        tool_event.tool_call = NS(function_calls=[NS(name='spotify_command', id='pause', args={'action': 'pause'})])
        session = FakeSession([
            [message(b'old', input_transcription=NS(text='Славик расскажи теорему'), output_transcription=NS(text='В треугольнике')),
             message(input_transcription=NS(text='Слайк поставь паузу')),
             message(b'old_tail', turn_complete=True)],
            [tool_event, message(b'new', turn_complete=True)]])
        with patch.object(agent.time, 'monotonic', return_value=100), self.assertRaises(RuntimeError):
            await agent.receiver(session, player, gate, queue)
        self.assertEqual(player.flushed, 1)
        self.assertEqual(player.history, [b'old', b'new'])
        self.assertTrue(queue.get_nowait()[1])

    async def test_barge_in_does_not_reuse_previous_volume_number(self):
        gate = agent.WakeGate()
        gate.feed_transcript('Славик сделай громкость20')
        gate.reply_started = True
        gate.input_authorized = False
        gate.feed_transcript('Славик говори громче')
        self.assertEqual(gate.last_utterance, 'Славик говори громче')
        player = agent.PulsePlayer()
        called = []
        await agent.route_volume({'action': 'up', 'target': 'voice'}, lambda args: called.append(args), player, gate.last_utterance)
        self.assertEqual(player.voice_percent, 50)
        self.assertEqual(called, [])

    async def test_news_does_not_need_gemini_search_api_when_rss_works(self):
        data = {'status': 'confirmed', 'sources': [{'url': 'https://example.com'}]}
        with patch.object(tools, 'news_headlines', return_value=data), patch.dict(agent.os.environ, {}, clear=True):
            self.assertEqual(await tools.web_search({'query': 'Tesla', 'kind': 'news'}), data)

    async def test_news_rss_failure_uses_grounded_search_and_closes_client(self):
        from google import genai
        client = NS(aio=NS(aclose=AsyncMock()))
        verified = {'status': 'confirmed', 'sources': [{'url': 'https://example.com/news'}]}
        with patch.object(tools, 'news_headlines', side_effect=OSError), \
             patch.object(genai, 'Client', return_value=client), \
             patch.object(tools, 'grounded_search', return_value=verified) as search:
            result = await tools.web_search({'query': 'Tesla', 'kind': 'news'})
        self.assertEqual(result, verified)
        search.assert_awaited_once_with(client, 'Tesla')
        client.aio.aclose.assert_awaited_once()

    async def test_search_404_uses_catalog_model_and_retains_sources(self):
        from google import genai
        error = Exception()
        error.code = 404
        response = NS(text='Актуальный ответ', candidates=[NS(grounding_metadata=NS(
            grounding_chunks=[NS(web=NS(uri='https://example.com', title='Source'))]))])
        generate = AsyncMock(side_effect=[error, response])
        client = NS(aio=NS(models=NS(generate_content=generate), aclose=AsyncMock()))
        with patch.object(genai, 'Client', return_value=client), \
             patch('speech_recognition.model_catalog', return_value=['models/gemini-catalog-flash']), \
             patch.dict(agent.os.environ, {'SLAVIK_SEARCH_MODEL': 'gemini-old-flash'}):
            result = await tools.web_search({'query': 'актуальные факты'})
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['model'], 'models/gemini-catalog-flash')
        self.assertEqual(generate.await_count, 2)


class AliasTests(unittest.TestCase):
    def test_expanded_soundalikes_from_logs_and_latin_spellings(self):
        for text in ('Слайк', 'Флаек привет', 'Свавик расскажи новости', 'Флаг какая погода',
                     'Славян включи музыку', 'Slavick play music', 'Сварик поставь паузу'):
            self.assertTrue(agent.contains_wake(text), text)
        for text in ('слова этой песни', 'Слайды покажи', 'обычная беседа о музыке'):
            self.assertFalse(agent.contains_wake(text), text)
