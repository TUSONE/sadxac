import asyncio
import io
import json
import struct
import unittest
import wave
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import speech_recognition as speech
import gemini_assistant_dialog as agent


class SegmentTests(unittest.TestCase):
    def test_real_vad_does_not_segment_silence(self):
        segmenter = speech.SpeechSegmenter()
        self.assertEqual(segmenter.feed(b'\0' * speech.FRAME_BYTES * 500), [])
        self.assertLessEqual(len(segmenter.pre), 10)

    def test_chunk_boundaries_and_silence_end_preserve_pcm(self):
        class VAD:
            def is_speech(self, frame, rate):
                return frame != b'\0' * speech.FRAME_BYTES
        raw = struct.pack('<h', 1000) * (speech.FRAME_BYTES // 2) * 20 + b'\0' * speech.FRAME_BYTES * 40
        whole = speech.SpeechSegmenter(VAD()).feed(raw)
        segmenter = speech.SpeechSegmenter(VAD())
        fragmented = []
        for i in range(0, len(raw), 137):
            fragmented.extend(segmenter.feed(raw[i:i+137]))
        self.assertEqual(whole, fragmented)
        self.assertEqual(len(whole), 1)
        self.assertLess(len(whole[0]), len(raw))
        self.assertTrue(whole[0].startswith(struct.pack('<h', 1000)))

    def test_short_click_rejected_and_long_input_bounded(self):
        class VAD:
            def is_speech(self, frame, rate): return frame[0] != 0
        segmenter = speech.SpeechSegmenter(VAD())
        self.assertEqual(segmenter.feed(b'\1' * speech.FRAME_BYTES * 3 + b'\0' * speech.FRAME_BYTES * 40), [])
        result = segmenter.feed(b'\1' * speech.FRAME_BYTES * 1000)
        self.assertTrue(result)
        self.assertTrue(all(len(pcm) <= speech.FRAME_BYTES * 400 for pcm in result))


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_request_is_wav_with_ru_en_schema_and_no_persisted_file(self):
        generate = AsyncMock(return_value=NS(text=json.dumps({'text': 'Славик, громкость 50', 'language': 'ru'})))
        client = NS(_slavik_asr_model='models/gemini-test-flash', aio=NS(models=NS(generate_content=generate)))
        raw = struct.pack('<h', 1000) * 16000
        self.assertEqual(await speech.transcribe(client, raw), 'Славик, громкость 50')
        args = generate.call_args.kwargs
        blob = args['contents'].parts[0].inline_data
        self.assertEqual(blob.mime_type, 'audio/wav')
        with wave.open(io.BytesIO(blob.data), 'rb') as data:
            self.assertEqual((data.getframerate(), data.getnchannels(), data.getnframes()), (16000, 1, 16000))
        self.assertEqual(args['config'].response_schema['properties']['language']['enum'], ['ru', 'en', 'none'])
        for content in ('{"text":"music", "language":"none"}', '{"text":"こんにちは", "language":"ja"}', 'invalid'):
            generate.return_value = NS(text=content)
            if 'none' in content:
                self.assertEqual(await speech.transcribe(client, raw), '')
            else:
                with self.assertRaises(ValueError): await speech.transcribe(client, raw)

    async def test_only_verified_address_starts_manual_activity(self):
        for text, expected in [('Славик поставь паузу', 4), ('Может что-нибудь обсудим?', 0), ('', 0)]:
            queue = asyncio.Queue()
            queue.put_nowait(b'pcm')
            session = NS(send_realtime_input=AsyncMock())
            player = NS(queue=asyncio.Queue(), audio_until=0)
            gate = agent.WakeGate(wake_enabled=True, followup_enabled=False)
            with patch.object(speech, 'transcribe', return_value=text):
                task = asyncio.create_task(speech.recognition_worker(None, session, queue, gate, player, agent.EchoGuard(), asyncio.Lock()))
                await asyncio.wait_for(queue.join(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(session.send_realtime_input.await_count, expected)
            if expected:
                self.assertTrue(gate.response_allowed)
                calls = session.send_realtime_input.call_args_list
                self.assertIn('activity_start', calls[0].kwargs)
                self.assertEqual(calls[1].kwargs['audio'].data, b'pcm')
                self.assertIn('Славик поставь паузу', calls[2].kwargs['text'])
                self.assertIn('activity_end', calls[3].kwargs)
                self.assertTrue(gate.asr_pending)

    async def test_asr_failure_does_not_open_gate_or_fall_back_to_untrusted_live_asr(self):
        queue = asyncio.Queue()
        queue.put_nowait(b'pcm')
        session = NS(send_realtime_input=AsyncMock())
        player = NS(queue=asyncio.Queue(), audio_until=0)
        gate = agent.WakeGate()
        with patch.object(speech, 'transcribe', side_effect=RuntimeError('private response')):
            task = asyncio.create_task(speech.recognition_worker(None, session, queue, gate, player, agent.EchoGuard(), asyncio.Lock()))
            await asyncio.wait_for(queue.join(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        session.send_realtime_input.assert_not_awaited()
        self.assertFalse(gate.response_allowed)

    async def test_unverified_live_transcription_cannot_override_authorized_request(self):
        from test_agent import FakePlayer, FakeSession, message
        gate, player = agent.WakeGate(), FakePlayer()
        gate.feed_transcript('Славик привет')
        session = FakeSession([[message(b'reply', input_transcription=NS(text='Ja fein feine'), turn_complete=True)]])
        with self.assertRaises(RuntimeError):
            await agent.receiver(session, player, gate, verified_asr=True)
        self.assertEqual(player.audio, [(b'reply', 24000)])

    async def test_server_interrupt_does_not_destroy_buffered_audio(self):
        from test_agent import FakePlayer, FakeSession, message
        gate, player = agent.WakeGate(), FakePlayer()
        session = FakeSession([[message(b'reply', input_transcription=NS(text='Славик привет')),
                               message(interrupted=True), message(b'echo', turn_complete=True)]])
        with self.assertRaises(RuntimeError):
            await agent.receiver(session, player, gate)
        self.assertEqual(player.flushed, 0)
        self.assertEqual(player.audio, [(b'reply', 24000)])
        self.assertEqual(player.finished, 1)

    async def test_manual_live_config_has_no_native_asr_or_automatic_interruptions(self):
        with patch.dict(agent.os.environ, {'SLAVIK_ASR': 'gemini'}):
            config = agent.make_config().model_dump(exclude_none=True)
        self.assertNotIn('input_audio_transcription', config)
        activity = config['realtime_input_config']
        self.assertEqual(activity['automatic_activity_detection'], {'disabled': True})
        self.assertEqual(activity['activity_handling'], 'NO_INTERRUPTION')
        self.assertEqual(activity['turn_coverage'], 'TURN_INCLUDES_ONLY_ACTIVITY')

    async def test_catalog_selection_excludes_live_tts_image_and_unusable_actions(self):
        models = [NS(name=name, supported_actions=actions) for name, actions in [
            ('models/gemini-3.1-flash-live-preview', ['generateContent']),
            ('models/gemini-9-flash-image', ['generateContent']),
            ('models/gemini-4-flash-tts', ['generateContent']),
            ('models/gemini-8-flash', ['bidiGenerateContent']),
            ('models/gemini-4-flash', ['generateContent']),
            ('models/gemini-3-flash', ['generateContent']),
            ('models/gemini-5-flash-preview', ['generateContent'])]]
        self.assertEqual(speech.candidate_models(models), ['models/gemini-4-flash', 'models/gemini-3-flash', 'models/gemini-5-flash-preview'])

    async def test_missing_explicit_model_falls_back_to_catalog_and_probes_audio(self):
        class Missing(Exception): code = 404
        async def pager():
            yield NS(name='models/gemini-4-flash', supported_actions=['generateContent'])
        generate = AsyncMock(side_effect=[Missing('private API body'), NS(text='{"text":"", "language":"none"}')])
        client = NS(aio=NS(models=NS(generate_content=generate, list=AsyncMock(return_value=pager()))))
        with patch.dict(agent.os.environ, {'SLAVIK_ASR_MODEL': 'gemini-2.5-flash'}):
            selected = await speech.prepare_recognizer(client)
        self.assertEqual(selected, 'models/gemini-4-flash')
        self.assertEqual(client._slavik_asr_model, selected)
        self.assertEqual([call.kwargs['model'] for call in generate.call_args_list], ['gemini-2.5-flash', selected])
        self.assertEqual(generate.call_args.kwargs['contents'].parts[0].inline_data.mime_type, 'audio/wav')

    async def test_forbidden_or_quota_error_is_not_hidden_by_switching_models(self):
        for code in (403, 429):
            error = Exception('private response')
            error.code = code
            listing = AsyncMock()
            client = NS(aio=NS(models=NS(generate_content=AsyncMock(side_effect=error), list=listing)))
            with patch.dict(agent.os.environ, {'SLAVIK_ASR_MODEL': 'gemini-4-flash'}):
                with self.assertRaisesRegex(RuntimeError, str(code)):
                    await speech.prepare_recognizer(client)
            listing.assert_not_awaited()

    async def test_all_404_candidates_fail_preflight_instead_of_connecting_silent_agent(self):
        class Missing(Exception): code = 404
        async def pager():
            yield NS(name='models/gemini-4-flash', supported_actions=['generateContent'])
        client = NS(aio=NS(models=NS(generate_content=AsyncMock(side_effect=Missing()), list=AsyncMock(return_value=pager()))))
        with patch.dict(agent.os.environ, {'SLAVIK_ASR_MODEL': ''}), self.assertRaisesRegex(RuntimeError, 'Нет проверенной модели'):
            await speech.prepare_recognizer(client)
        self.assertFalse(hasattr(client, '_slavik_asr_model'))

    async def test_transient_503_retries_same_audio_once(self):
        error = Exception('private error body')
        error.code = 503
        generate = AsyncMock(side_effect=[error, NS(text='{"text":"Славик привет", "language":"ru"}')])
        client = NS(_slavik_asr_model='models/gemini-4-flash', aio=NS(models=NS(generate_content=generate)))
        with patch.object(speech.asyncio, 'sleep', new=AsyncMock()) as delay:
            self.assertEqual(await speech.transcribe(client, b'pcm'), 'Славик привет')
        self.assertEqual(generate.await_count, 2)
        delay.assert_awaited_once_with(1)
        self.assertEqual(generate.call_args_list[0], generate.call_args_list[1])

    async def test_persistently_overloaded_candidate_falls_back_to_next_catalog_model(self):
        error = Exception('private error body')
        error.code = 503
        async def pager():
            for name in ['models/gemini-4-flash', 'models/gemini-3-flash']:
                yield NS(name=name, supported_actions=['generateContent'])
        generate = AsyncMock(side_effect=[error, error, NS(text='{"text":"", "language":"none"}')])
        client = NS(aio=NS(models=NS(generate_content=generate, list=AsyncMock(return_value=pager()))))
        with patch.dict(agent.os.environ, {'SLAVIK_ASR_MODEL': ''}), patch.object(speech.asyncio, 'sleep', new=AsyncMock()):
            selected = await speech.prepare_recognizer(client)
        self.assertEqual(selected, 'models/gemini-3-flash')
        self.assertEqual([c.kwargs['model'] for c in generate.call_args_list],
                         ['models/gemini-4-flash', 'models/gemini-4-flash', 'models/gemini-3-flash'])

    async def test_preflight_has_overall_time_budget(self):
        with patch.object(speech, '_prepare_recognizer', side_effect=TimeoutError):
            with self.assertRaisesRegex(RuntimeError, '60s'):
                await speech.prepare_recognizer(None)

    async def test_real_audio_503_failover_preserves_utterance_and_caches_success(self):
        error = Exception('private response')
        error.code = 503
        generate = AsyncMock(side_effect=[error, error, NS(text='{"text":"Славка включи kizaru", "language":"ru"}')])
        client = NS(_slavik_asr_model='models/gemini-4-flash',
                    _slavik_asr_candidates=['models/gemini-4-flash', 'models/gemini-3-flash'],
                    aio=NS(models=NS(generate_content=generate)))
        pcm = struct.pack('<h', 1234) * 16000
        with patch.object(speech.asyncio, 'sleep', new=AsyncMock()):
            text = await speech.transcribe_with_failover(client, pcm)
        self.assertEqual(text, 'Славка включи kizaru')
        self.assertEqual(client._slavik_asr_model, 'models/gemini-3-flash')
        self.assertEqual([c.kwargs['model'] for c in generate.call_args_list],
                         ['models/gemini-4-flash', 'models/gemini-4-flash', 'models/gemini-3-flash'])
        blobs = [c.kwargs['contents'].parts[0].inline_data.data for c in generate.call_args_list]
        self.assertEqual(blobs, [speech.wav_bytes(pcm)] * 3)

    async def test_real_audio_failover_is_bounded_and_does_not_invent_address(self):
        error = Exception()
        error.code = 503
        client = NS(_slavik_asr_model='models/gemini-4-flash',
                    _slavik_asr_candidates=['models/gemini-4-flash', 'models/gemini-3-flash', 'models/gemini-2-flash'],
                    aio=NS(models=NS(generate_content=AsyncMock(side_effect=error))))
        with patch.object(speech.asyncio, 'sleep', new=AsyncMock()), self.assertRaises(speech.ASRUnavailable):
            await speech.transcribe_with_failover(client, b'pcm')
        self.assertEqual(client.aio.models.generate_content.await_count, 6)
        client.aio.models.generate_content = AsyncMock(return_value=NS(text='{"text":"Привет включи kizaru", "language":"ru"}'))
        self.assertEqual(await speech.transcribe_with_failover(client, b'pcm'), 'Привет включи kizaru')

    async def test_real_audio_forbidden_does_not_use_alternate_models(self):
        error = Exception()
        error.code = 403
        client = NS(_slavik_asr_model='models/gemini-4-flash',
                    _slavik_asr_candidates=['models/gemini-3-flash'],
                    aio=NS(models=NS(generate_content=AsyncMock(side_effect=error))))
        with self.assertRaises(Exception) as raised:
            await speech.transcribe_with_failover(client, b'pcm')
        self.assertIs(raised.exception, error)
        self.assertEqual(client.aio.models.generate_content.await_count, 1)

    async def test_default_uses_direct_flash_live_with_automatic_input_and_no_extra_asr(self):
        with patch.dict(agent.os.environ, {}, clear=True):
            self.assertFalse(agent.independent_asr())
            config = agent.make_config().model_dump(exclude_none=True)
        self.assertFalse(config['realtime_input_config']['automatic_activity_detection']['disabled'])
        self.assertEqual(config['realtime_input_config']['activity_handling'], 'NO_INTERRUPTION')
        self.assertIn('input_audio_transcription', config)


class EngineCLITests(unittest.TestCase):
    def test_cli_live_choice_overrides_env_loaded_by_launcher(self):
        async def capture(args):
            self.assertEqual(args.asr_engine, 'live')
            self.assertFalse(agent.independent_asr())
        with patch.dict(agent.os.environ, {'SLAVIK_ASR': 'gemini'}), \
             patch.object(agent.sys, 'argv', ['gemini_assistant_dialog.py', '--asr-engine', 'live']), \
             patch.object(agent, 'entry', side_effect=capture) as entry:
            agent.main()
            entry.assert_called_once()
