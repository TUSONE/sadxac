import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import meme_lore as lore
import gemini_assistant_dialog as agent


class LoreTests(unittest.TestCase):
    def test_provided_cast_settings_and_gaps_are_retrievable(self):
        cases = [('маму-птицу', 'mother_bird', 'птенц'),
                 ('Фог', 'fog', 'преслед'), ('Друн', 'ch_drun', 'спутник'),
                 ('Молочном', 'molochnoe', 'трамвай'), ('Артур', 'arthur', 'рюкзак'),
                 ('Баба Чай', 'baba_chai', 'биограф'), ('Славчуха', 'slavchuha', 'не установ'),
                 ('Куки', 'cat', 'SlivkiShow'), ('Прадод', 'language', 'сын'),
                 ('Элджеевка', 'eldzheevka', 'бел'), ('смены', 'shifts', 'школ'),
                 ('Потеряевка', 'dlc', 'не установлен')]
        for query, key, fragment in cases:
            with self.subTest(query=query):
                result = lore.lookup_lore({'query': query})
                entries = {e['id']: e for e in result['entries']}
                self.assertIn(key, entries)
                self.assertIn(fragment.lower(), str(entries[key]).lower())
                self.assertEqual(result['scope'], 'user_supplied_fan_lore')

    def test_overview_covers_all_twenty_three_topics_without_dumping_full_details(self):
        result = lore.lookup_lore({'query': 'обзор всех сезонов'})
        self.assertEqual(len(result['overview']), 23)
        self.assertTrue(all('detail' not in e for e in result['overview']))
        self.assertIn('не провер', result['instruction'])

    def test_unknown_query_returns_index_not_invented_biography(self):
        result = lore.lookup_lore({'query': 'Несуществующий герой Зюзюбрик'})
        self.assertNotIn('entries', result)
        self.assertIn('overview', result)
        for value in (None, '', 10, 'a' * 501):
            self.assertEqual(lore.lookup_lore({'query': value})['status'], 'needs_input')

    def test_broken_or_missing_local_data_fails_without_network(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(lore, 'PATH', Path(folder) / 'lore.json'):
            try:
                for data in (None, '{}', 'bad', 'a' * 65537):
                    lore.load_lore.cache_clear()
                    if data is not None:
                        lore.PATH.write_text(data)
                    self.assertEqual(lore.lookup_lore({'query': 'Фог'})['status'], 'failed')
            finally:
                lore.load_lore.cache_clear()

    def test_live_config_exposes_local_retrieval_and_rare_contextual_jokes(self):
        with patch.dict(agent.os.environ, {'SLAVIK_ASR': 'live'}):
            config = agent.make_config()
        self.assertIn('meme_lore', [d.name for d in config.tools[0].function_declarations])
        self.assertIn('на шесть содержательных ответов', config.system_instruction)
        self.assertNotIn('гигантские пауки', config.system_instruction)  # details retrieved on demand


class LoreToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_lore_is_authorized_and_never_bypasses_sleep_gate(self):
        for authorized in (False, True):
            queue = asyncio.Queue()
            queue.put_nowait((NS(name='meme_lore', id='lore', args={'query': 'Артур'}), authorized))
            session = NS(send_tool_response=AsyncMock())
            with patch.object(agent, 'lookup_lore', wraps=lore.lookup_lore) as lookup:
                worker = asyncio.create_task(agent.tool_worker(session, queue, None, gate=agent.WakeGate()))
                await asyncio.wait_for(queue.join(), 1)
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            self.assertEqual(lookup.call_count, int(authorized))
            response = session.send_tool_response.call_args.kwargs['function_responses'][0].response
            self.assertEqual(response['status'], 'confirmed' if authorized else 'failed')
