"""Independent RU/EN recognition: local WebRTC VAD + bounded Gemini audio ASR.

No audio is written to disk. Live's websocket/microphone remain open; only
verified addressed segments start a manual Live activity.
"""
import asyncio
from collections import deque
import io
import json
import os
import re
import time
import wave

RATE = 16000
FRAME_BYTES = 640  # 20ms mono PCM16


class ASRUnavailable(RuntimeError):
    """Safe, application-authored error text; never includes API response bodies."""


class SpeechSegmenter:
    def __init__(self, vad=None):
        if vad is None:
            try:
                import webrtcvad
            except ImportError:
                raise RuntimeError('Нужен новый VAD: выполните bash install-linux.sh.') from None
            vad = webrtcvad.Vad(2)
        self.vad = vad
        self.reset()

    def reset(self):
        self.pending = b''
        self.pre = deque(maxlen=10)  # 200ms before start
        self.votes = deque(maxlen=5)
        self.frames = []
        self.voiced = 0
        self.silent = 0

    def feed(self, data):
        self.pending += data
        ready = []
        while len(self.pending) >= FRAME_BYTES:
            frame, self.pending = self.pending[:FRAME_BYTES], self.pending[FRAME_BYTES:]
            speech = self.vad.is_speech(frame, RATE)
            if not self.frames:
                self.pre.append(frame)
                self.votes.append(speech)
                if sum(self.votes) >= 3:
                    self.frames = list(self.pre)
                    self.voiced = sum(self.votes)
                    self.pre.clear()
            else:
                self.frames.append(frame)
                self.voiced += bool(speech)
                self.silent = 0 if speech else self.silent + 1
                if self.silent >= 35 or len(self.frames) >= 400:  # .7s silence / 8s cap
                    if self.voiced >= 8:
                        # Keep a .2s tail; long silence contributes no information.
                        tail = max(0, self.silent - 10)
                        ready.append(b''.join(self.frames[:-tail] if tail else self.frames))
                    self.frames, self.voiced, self.silent = [], 0, 0
                    self.votes.clear()
        return ready


def wav_bytes(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(pcm)
    return output.getvalue()


ASR_PROMPT = """Распознай речь человека в аудио. Верни только JSON.
Языки: русский или английский. Не переводи, не отвечай на просьбу, не придумывай
слова по шуму. Если нет понятной речи человека или слышна лишь музыка/песня,
text="", language="none". Собственный голос ассистента не является просьбой.
Ассистента зовут Славик. Славка, Слава, Славуха, Славчуха и похожее произношение
могут быть обращением в любой части фразы. Если такое обращение действительно
слышно, сохрани имя по-русски; не заменяй его иностранным словом. Не добавляй имя,
которого не слышно. Кизару — исполнитель kizaru. Сохраняй названия треков и числа.
"""


def candidate_models(models):
    """Use names returned by this API/key, never guess a new model version."""
    candidates = []
    for model in models:
        name = getattr(model, 'name', '') or ''
        short = name.removeprefix('models/')
        actions = getattr(model, 'supported_actions', None) or []
        if (not re.fullmatch(r'gemini-[a-zA-Z0-9_.-]+', short) or 'flash' not in short or
                not any(a in ('generateContent', 'generate_content') for a in actions) or
                any(term in short for term in ('image', 'tts', 'live', 'native-audio', 'embedding', 'robotics'))):
            continue
        candidates.append(name)
    def rank(name):
        version = re.search(r'gemini-(\d+)(?:\.(\d+))?', name)
        major, minor = (int(version[1]), int(version[2] or 0)) if version else (0, 0)
        return ('preview' in name or 'exp' in name, 'lite' in name, -major, -minor, name)
    return sorted(set(candidates), key=rank)


def temporary_asr_error(error):
    return getattr(error, 'code', None) in (500, 502, 503, 504) or isinstance(error, TimeoutError)


async def model_catalog(client):
    async def collect():
        result = []
        async for model in await client.aio.models.list():
            result.append(model)
            if len(result) >= 200:
                break
        return result
    try:
        models = await asyncio.wait_for(collect(), 15)
    except Exception as error:
        raise RuntimeError(f'Не удалось получить каталог Gemini для ASR; код={getattr(error, "code", "—")}. '                           'Можно указать доступную audio-модель в SLAVIK_ASR_MODEL.') from None
    candidates = candidate_models(models)
    client._slavik_asr_candidates = candidates
    return candidates


async def prepare_recognizer(client):
    try:
        return await asyncio.wait_for(_prepare_recognizer(client), 60)
    except asyncio.TimeoutError:
        raise RuntimeError('Проверка ASR превысила60s: сервис временно недоступен. Повторите запуск позже.') from None


async def _prepare_recognizer(client):
    explicit = os.getenv('SLAVIK_ASR_MODEL', '').strip()
    if explicit and not re.fullmatch(r'(?:models/)?gemini-[a-zA-Z0-9_.-]+', explicit):
        raise RuntimeError('Некорректное имя SLAVIK_ASR_MODEL.')
    async def probe(name):
        await transcribe(client, b'\0' * 8000, model=name)  # .25s silence; no microphone recording
        client._slavik_asr_model = name
        print(f'[ASR] Модель проверена: {name}', flush=True)
        return name
    if explicit:
        try:
            return await probe(explicit)
        except Exception as error:
            if getattr(error, 'code', None) != 404 and not temporary_asr_error(error):
                raise RuntimeError(f'Проверка ASR {explicit} не прошла; код={getattr(error, "code", "—")}.') from None
            print(f'[ASR] {explicit} недоступна; код={getattr(error, "code", "таймаут")}. Ищу другую модель в каталоге API.', flush=True)
    candidates = await model_catalog(client)
    tried = []
    for name in candidates[:5]:
        if name.removeprefix('models/') == explicit.removeprefix('models/'):
            continue
        tried.append(name)
        try:
            return await probe(name)
        except Exception as error:
            code = getattr(error, 'code', None)
            if code == 404 or isinstance(error, ValueError) or temporary_asr_error(error):
                print(f'[ASR] {name}: проверка не прошла, код={code or "таймаут/формат"}; проверяю следующую модель.', flush=True)
                continue
            # Permissions and quota are not transient model overload.
            raise RuntimeError(f'Проверка модели ASR {name} не прошла; код={code or "—"}. '                               'Проверьте доступ и лимиты API.') from None
    raise RuntimeError('Нет проверенной модели для отдельного ASR. Кандидаты: ' +
                       (', '.join(tried) or 'каталог не содержит Flash с generateContent') +
                       '. Можно временно выбрать SLAVIK_ASR=live в .env.')


async def transcribe(client, pcm, model=None):
    from google.genai import types
    model = model or getattr(client, '_slavik_asr_model', None) or os.getenv('SLAVIK_ASR_MODEL')
    if not model:
        raise RuntimeError('ASR модель не проверена перед запуском.')
    for attempt in range(2):
        try:
            response = await asyncio.wait_for(client.aio.models.generate_content(
                model=model,
                contents=types.Content(role='user', parts=[types.Part.from_bytes(
                    data=wav_bytes(pcm), mime_type='audio/wav')]),
                config=types.GenerateContentConfig(
                    system_instruction=ASR_PROMPT, temperature=0,
                    response_mime_type='application/json', response_schema={
                        'type': 'OBJECT', 'properties': {
                            'text': {'type': 'STRING'},
                            'language': {'type': 'STRING', 'enum': ['ru', 'en', 'none']}},
                        'required': ['text', 'language']})), 15)
            break
        except Exception as error:
            if attempt or not temporary_asr_error(error):
                raise
            print(f'[ASR] {model}: временная ошибка {getattr(error, "code", "таймаут")}; повтор через1s.', flush=True)
            await asyncio.sleep(1)
    data = json.loads(response.text or '{}')
    if not isinstance(data, dict) or data.get('language') not in ('ru', 'en', 'none'):
        raise ValueError('ASR format')
    text = data.get('text')
    if not isinstance(text, str) or len(text) > 1500:
        raise ValueError('ASR text')
    return '' if data['language'] == 'none' else text.strip()


async def transcribe_with_failover(client, pcm):
    """Keep the same utterance in memory when a healthy-at-start model fails."""
    try:
        return await asyncio.wait_for(_transcribe_with_failover(client, pcm), 60)
    except asyncio.TimeoutError:
        raise ASRUnavailable('ASR не ответил за60s. Фраза не отправлена на выполнение; повторите её позже.') from None


async def _transcribe_with_failover(client, pcm):
    current = getattr(client, '_slavik_asr_model', None) or os.getenv('SLAVIK_ASR_MODEL')
    try:
        return await transcribe(client, pcm)
    except Exception as error:
        if not temporary_asr_error(error) and getattr(error, 'code', None) != 404:
            raise
    candidates = getattr(client, '_slavik_asr_candidates', None)
    if candidates is None:
        candidates = await model_catalog(client)
    alternatives = [m for m in candidates if m.removeprefix('models/') != (current or '').removeprefix('models/')]
    for model in alternatives[:2]:
        print(f'[ASR] Переношу ту же фразу на {model}; повторять её не нужно.', flush=True)
        try:
            text = await transcribe(client, pcm, model=model)
        except Exception as error:
            if temporary_asr_error(error) or getattr(error, 'code', None) == 404 or isinstance(error, ValueError):
                continue
            raise
        client._slavik_asr_model = model
        print(f'[ASR] Распознавание выполнено: {model}', flush=True)
        return text
    raise ASRUnavailable('Сервис ASR недоступен на выбранной и резервных моделях. '                       'Команда не выполнялась; повторите фразу позже.')


def player_busy(player):
    return not player.queue.empty() or time.monotonic() < player.audio_until + .2


async def microphone_pipeline(session, capture, player, gate, queue, send_lock):
    from google.genai import types
    segmenter = SpeechSegmenter()
    echo_tail = 0
    while True:
        mono = await capture.read_mono()
        # Stream continuously, outside manual activity. It cannot trigger a reply.
        async with send_lock:
            await session.send_realtime_input(audio=types.Blob(data=mono, mime_type='audio/pcm;rate=16000'))
        if player_busy(player):
            echo_tail = time.monotonic() + 1.2
        if gate.asr_pending or time.monotonic() < echo_tail:
            segmenter.reset()
            continue
        for pcm in segmenter.feed(mono):
            if not queue.full():
                queue.put_nowait(pcm)
            else:
                print('[ASR] Распознаватель занят; новая фраза пропущена.', flush=True)


async def recognition_worker(client, session, queue, gate, player, echo, send_lock):
    from google.genai import types
    from gemini_assistant_dialog import contains_wake
    while True:
        pcm = await queue.get()
        try:
            if gate.asr_pending or player_busy(player):
                continue
            try:
                text = await transcribe_with_failover(client, pcm)
            except Exception as error:
                if isinstance(error, ASRUnavailable):
                    print(f'[ASR] {error}', flush=True)
                else:
                    print(f'[ASR] Ошибка {type(error).__name__}; код={getattr(error, "code", "—")}. Проверьте доступ и лимиты ASR API.', flush=True)
                continue
            if not text or player_busy(player):
                continue
            accepted = echo.accept(text, player)
            if accepted is None or (gate.wake_enabled and not contains_wake(accepted)):
                print(f'[ASR фон] {text}', flush=True)
                continue
            print(f'\nТы [ASR]: {accepted}', flush=True)
            if gate.feed_transcript(accepted):
                print('[Славик активирован]', flush=True)
            if not gate.response_allowed:
                continue
            # VAD/ASR complete before Live sees an activity: there is no race with
            # early model replies/tool calls based on a different transcription.
            gate.asr_pending = True
            async with send_lock:
                await session.send_realtime_input(activity_start=types.ActivityStart())
                await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type='audio/pcm;rate=16000'))
                await session.send_realtime_input(text='Проверенная транскрипция текущей реплики: ' + accepted)
                await session.send_realtime_input(activity_end=types.ActivityEnd())
            gate.input_authorized = False
        finally:
            queue.task_done()
