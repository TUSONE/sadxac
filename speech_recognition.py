"""Independent RU/EN recognition: local WebRTC VAD + bounded Gemini audio ASR.

No audio is written to disk. Live's websocket/microphone remain open; only
verified addressed segments start a manual Live activity.
"""
import asyncio
from collections import deque
import io
import json
import os
import time
import wave

RATE = 16000
FRAME_BYTES = 640  # 20ms mono PCM16


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


async def transcribe(client, pcm):
    from google.genai import types
    response = await asyncio.wait_for(client.aio.models.generate_content(
        model=os.getenv('SLAVIK_ASR_MODEL', 'gemini-2.5-flash'),
        contents=types.Content(role='user', parts=[types.Part.from_bytes(
            data=wav_bytes(pcm), mime_type='audio/wav')]),
        config=types.GenerateContentConfig(
            system_instruction=ASR_PROMPT, temperature=0,
            response_mime_type='application/json', response_schema={
                'type': 'OBJECT', 'properties': {
                    'text': {'type': 'STRING'},
                    'language': {'type': 'STRING', 'enum': ['ru', 'en', 'none']}},
                'required': ['text', 'language']})), 15)
    data = json.loads(response.text or '{}')
    if not isinstance(data, dict) or data.get('language') not in ('ru', 'en', 'none'):
        raise ValueError('ASR format')
    text = data.get('text')
    if not isinstance(text, str) or len(text) > 1500:
        raise ValueError('ASR text')
    return '' if data['language'] == 'none' else text.strip()


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
                text = await transcribe(client, pcm)
            except Exception as error:
                print(f'[ASR] Ошибка {type(error).__name__}; код={getattr(error, "code", "—")}. Проверьте SLAVIK_ASR_MODEL и лимиты API.', flush=True)
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
