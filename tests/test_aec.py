import unittest
from mic_processing import pcm
from aec_measurement import Levels


class LevelTests(unittest.TestCase):
    def test_channels_and_clipping_remain_separate(self):
        levels = Levels()
        levels.add(pcm(v for i in range(1600) for v in (100 if i % 2 else -100, 32767 if i % 2 else -32768)))
        result = levels.result()
        self.assertEqual(result['left']['clipped_percent'], 0)
        self.assertEqual(result['right']['clipped_percent'], 100)
        self.assertGreater(result['right']['rms'], result['left']['rms'] * 300)

    def test_empty_measurement_and_incomplete_stereo(self):
        levels = Levels()
        self.assertEqual(levels.result()['right']['rms'], 0)
        with self.assertRaises(ValueError):
            levels.add(pcm([1]))


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_streams_discard_warmup_and_close(self):
        import sys
        from aec_measurement import measure
        from gemini_assistant_dialog import stop_process
        sources = []
        stopped = []
        def command(mode, source):
            self.assertEqual(mode, 'capture')
            sources.append(source)
            # Warmup has a different amplitude; both actual OS pipes must drain.
            return [sys.executable, '-c',
                    "import struct,sys; "
                    "quiet=struct.pack('<4h',100,1000,-100,-1000)*160; "
                    "speech=struct.pack('<4h',200,2000,-200,-2000)*160; "
                    "sys.stdout.buffer.write(quiet*100+speech*50)"]
        async def close(proc):
            stopped.append(proc)
            await stop_process(proc)
        result = await measure(command, close, 1)
        self.assertEqual(len(set(sources)), 2)
        self.assertEqual(len(stopped), 2)
        for source in ('physical', 'aec'):
            self.assertGreater(result[source]['right']['rms'], 1900)
            self.assertLess(result[source]['left']['rms'], 210)
            self.assertEqual(result[source]['right']['clipped_percent'], 0)
