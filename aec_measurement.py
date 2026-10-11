"""Compare both mic channels before/after AEC without storing recordings."""
import asyncio
import json
import math
from mic_processing import HighPass, frame_stats, samples, pcm

PHYSICAL_SOURCE = 'alsa_input.platform-sound_i2s.stereo-fallback'
AEC_SOURCE = 'voice_aec_source'


class Levels:
    def __init__(self):
        self.filters = [HighPass(), HighPass()]
        self.energy = [0., 0.]
        self.clipped = [0, 0]
        self.count = 0
        self.peak = [0, 0]

    def add(self, stereo):
        values = samples(stereo)
        if len(values) % 2:
            raise ValueError('Incomplete stereo frame')
        for channel in range(2):
            stats = frame_stats(pcm(values[channel::2]), self.filters[channel])
            self.energy[channel] += stats['rms'] ** 2 * stats['count']
            self.clipped[channel] += stats['clipped']
            self.peak[channel] = max(self.peak[channel], stats['raw_peak'])
        self.count += len(values) // 2

    def result(self):
        return {name: {'rms': round(math.sqrt(self.energy[ch] / max(1, self.count)), 1),
                       'peak': self.peak[ch],
                       'clipped_percent': round(100 * self.clipped[ch] / max(1, self.count), 4)}
                for ch, name in enumerate(('left', 'right'))}


async def measure(command, stop_process, seconds):
    processes = []
    levels = [Levels(), Levels()]
    try:
        for source in (PHYSICAL_SOURCE, AEC_SOURCE):
            processes.append(await asyncio.create_subprocess_exec(
                *command('capture', source), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
        # Read both pipes throughout; discard two seconds for AEC adaptation.
        for frame in range((seconds + 2) * 50):
            try:
                chunks = await asyncio.wait_for(asyncio.gather(
                    *(proc.stdout.readexactly(1280) for proc in processes)), timeout=5)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError) as error:
                raise RuntimeError('AEC-тест: захват прерван/нет аудио. Проверьте --diagnose и аудиомост.') from error
            if frame >= 100:
                for level, chunk in zip(levels, chunks):
                    level.add(chunk)
        return dict(zip(('physical', 'aec'), (level.result() for level in levels)))
    finally:
        for proc in processes:
            await stop_process(proc)


async def run_test(command, stop_process):
    print('AEC-тест без Gemini: аудио не сохраняется. Славик должен быть остановлен.', flush=True)
    report = {}
    stages = [
        ('quiet', 'Остановите музыку, молчите. Enter: 8 секунд тишины.', 8),
        ('music', 'Включите Spotify на Slavik с обычной громкостью, молчите. Enter: 8 секунд музыки.', 8),
        ('speech_music', 'Музыку оставьте. Enter: 10 секунд говорите с обычного расстояния: «Славик, громкость десять; Славка, включи Кизару».', 10),
    ]
    for name, prompt, seconds in stages:
        await asyncio.to_thread(input, prompt + '\n')
        print('Измерение...', flush=True)
        report[name] = await measure(command, stop_process, seconds)
        print(json.dumps({name: report[name]}, ensure_ascii=False), flush=True)
    print('AEC_REPORT=' + json.dumps(report, ensure_ascii=False), flush=True)
    print('Для распознавания используется right. Сравните quiet/music/speech_music: '
          'AEC должен уменьшать музыку и сохранять рост уровня при вашей речи. '
          'Разница physical/aec сама по себе не доказывает эффективность: у узлов может отличаться усиление. '
          'Пришлите AEC_REPORT; по нему выберем дальнейшую настройку.', flush=True)
