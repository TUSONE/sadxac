"""Regression checks for slow DSP and playback feedback on a small CPU."""
import asyncio
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import gemini_assistant_dialog as agent
from test_agent import FakePlayer, FakeSession, message


class CaptureWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_native_dsp_does_not_block_event_loop_or_reorder_blocks(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def process(raw):
            calls.append(raw)
            entered.set()
            release.wait(1)
            return raw
        processor = NS(channel=1, enhancer=NS(threaded=True, close=lambda: None), process=process)
        with patch.object(agent, 'load_processor', return_value=processor):
            capture = agent.PulseCapture()
        capture.read_stereo = AsyncMock(return_value=b'\x01\0\x02\0' * 640)
        task = asyncio.create_task(capture.read_mono())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            # Receiver/player scheduling must remain possible while C is busy.
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            self.assertEqual(await task, b'\x02\0' * 640)
            self.assertEqual(calls, [b'\x02\0' * 640])
            self.assertIsNone(capture.processing_task)
        finally:
            release.set()
            await task
            await capture.close()

    async def test_cancel_does_not_free_native_state_before_worker_finishes(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def process(raw):
            entered.set()
            release.wait(1)
            finished.set()
            return raw
        def close():
            self.assertTrue(finished.is_set())
        enhancer = NS(threaded=True, close=close)
        with patch.object(agent, 'load_processor', return_value=NS(channel=1, enhancer=enhancer, process=process)):
            capture = agent.PulseCapture()
        capture.read_stereo = AsyncMock(return_value=b'\0' * agent.INPUT_BYTES)
        task = asyncio.create_task(capture.read_mono())
        closing = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            closing = asyncio.create_task(capture.close())
            await asyncio.sleep(.01)
            self.assertFalse(closing.done())
            release.set()
            await closing
            self.assertIsNone(capture.processing_task)
        finally:
            release.set()
            await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    def test_audio_wake_cli_overrides_old_env(self):
        async def entry(args):
            self.assertFalse(agent.audio_wake_enabled())
        with patch.dict(agent.os.environ, {'SLAVIK_AUDIO_WAKE': '1'}), patch.object(agent.sys, 'argv', ['agent', '--audio-wake', 'off']), patch.object(agent, 'entry', side_effect=entry):
            agent.main()

    async def test_raw_audio_only_false_wake_cannot_authorize_dos(self):
        gate, player, queue = agent.WakeGate(), FakePlayer(), asyncio.Queue()
        event = message(input_transcription=NS(text='dos'))
        event.tool_call = NS(function_calls=[NS(name='wake_assistant', id='false-wake',
            args={'addressed': True, 'command_present': True})])
        with patch.dict(agent.os.environ, {}, clear=True), patch.object(agent, 'MIC_SOURCE', agent.PHYSICAL_MIC_SOURCE):
            self.assertFalse(agent.audio_wake_enabled())
            self.assertNotIn('Определяй обращение по самому входному аудио', agent.system_instruction())
            self.assertNotIn('wake_assistant', agent.system_instruction())
            with self.assertRaises(RuntimeError):
                await agent.receiver(FakeSession([[event, message(b'false-reply', turn_complete=True)]]), player, gate, queue)
        self.assertFalse(queue.get_nowait()[1])
        self.assertEqual(player.audio, [])

    async def test_mistranscribed_feedback_does_not_interrupt_aec_reply_by_default(self):
        gate, player = agent.WakeGate(), FakePlayer()
        player.audio_until, player.queue = 110, asyncio.Queue()
        with patch.dict(agent.os.environ, {}, clear=True), patch.object(agent, 'MIC_SOURCE', 'voice_aec_source'), patch.object(agent.time, 'monotonic', return_value=100):
            gate.feed_transcript('Славик привет')
            self.assertFalse(agent.unaddressed_barge_in_enabled())
            with self.assertRaises(RuntimeError):
                await agent.receiver(FakeSession([[message(input_transcription=NS(text='Will I switch it?'))]]), player, gate)
        self.assertEqual(player.flushed, 0)

    async def test_named_interruption_and_nameless_followup_still_work(self):
        player = FakePlayer()
        player.audio_until, player.queue = 110, asyncio.Queue()
        guard = agent.EchoGuard()
        with patch.object(agent.time, 'monotonic', return_value=100) as clock:
            self.assertEqual(guard.accept('Славик стоп', player), 'Славик стоп')
            clock.return_value = 111
            gate = agent.WakeGate()
            gate.extend()
            gate.addressed = True
            accepted = guard.accept('А какая погода?', player)
            self.assertEqual(accepted, 'А какая погода?')
            gate.feed_transcript(accepted)
            self.assertTrue(gate.response_allowed)
