#!/usr/bin/env python3
import asyncio
import os
import sys
import base64
import re
import time
import argparse
import math
import signal
from contextlib import AsyncExitStack, asynccontextmanager
from mic_processing import load_processor
from array import array


MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
MIC_SOURCE = os.getenv("GEMINI_MIC_SOURCE", "voice_aec_source")
SPEAKER_SINK = os.getenv("GEMINI_SPEAKER_SINK", "voice_aec_sink")
VOICE = os.getenv("GEMINI_VOICE", "Kore")

INPUT_RATE = 16000
INPUT_CHANNELS = 2
INPUT_SELECTED_CHANNEL = 1
INPUT_CHUNK_MS = 40
INPUT_FRAMES = INPUT_RATE * INPUT_CHUNK_MS // 1000
INPUT_BYTES = INPUT_FRAMES * INPUT_CHANNELS * 2

OUTPUT_RATE = 48000
OUTPUT_CHANNELS = 2

# После обращения можно продолжать разговор без повторения имени.
AWAKE_SECONDS = float(os.getenv("SLAVIK_AWAKE_SECONDS", "5"))
WAKE_ENABLED = os.getenv("SLAVIK_WAKE_ENABLED", "1") == "1"
DEBUG_AUDIO = os.getenv("DEBUG_AUDIO", "0") == "1"
DEBUG_EVENTS = os.getenv("SLAVIK_DEBUG_EVENTS", "0") == "1"

def event_log(name, **fields):
    if DEBUG_EVENTS:
        print(f"[EVENT {time.monotonic():.3f}] {name} " + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)

WAKE_PATTERNS = [
    re.compile(r"\b(?:эй\s+)?славк[аоу]\b", re.I),
    re.compile(r"\bславик\b", re.I),
    re.compile(r"\b(?:hey\s+)?slav(?:ik|ka|uha)\b", re.I),
    re.compile(r"\bебан(?:ые|ый|ая)\s+славик(?:и|ов)?\b", re.I),
    re.compile(r"\bёбан(?:ые|ый|ая)\s+славик(?:и|ов)?\b", re.I),
    re.compile(r"\bславух[аоуе]?\b", re.I),
]

SYSTEM_PROMPT = os.getenv(
    "GEMINI_SYSTEM_PROMPT",
    """Ты голосовой собеседник по имени Славик. Веди естественный непрерывный
живой разговор: слушай пользователя, отвечай и поддерживай контекст беседы.
Правило обращения и ожидания команд задано ниже. Не объявляй режим вопрос-ответ.
Пользователь может говорить поверх твоего ответа: немедленно уступи ему слово.
Не воспринимай собственный голос из динамика как новую реплику пользователя.
Не комментируй техническую работу микрофона, AEC, VAD или аудиопотока.
Отвечай естественно и кратко, если пользователь не попросил подробностей.""",
)


def system_instruction():
    # Language codes bias server transcription; they do not enforce an ASR allowlist.
    language = """Поддерживаемые языки разговора: только русский и английский.
Распознавай русскую и английскую речь, в том числе с акцентом. По умолчанию
отвечай по-русски, на явно английскую реплику отвечай по-английски.
Имена «Славка», «Славик», «Славуха», Slavka, Slavik относятся к тебе;
не принимай их за слова другого языка. Русские приветствия «салом», «салам», сленг «братуха» и английские названия
песен не означают переход на третий язык. Не отвечай на других языках.
Если речь непонятна или на другом языке, попроси повторить по-русски или
по-английски; не выдумывай услышанное."""
    mode = (
        "Режим пробуждения включён. Правило этого режима имеет приоритет: "
        "обычно молчи; отвечай после обращения «Славка», «Эй Славка», «Славик», "
        "«Ебаные славики», «Славуха». После обращения поддерживай диалог "
        f"до {AWAKE_SECONDS:g} секунд без повторения имени. После бездействия "
        "снова молчи до следующего обращения. Имя может быть в начале, середине "
        "или конце фразы: «Славка, как дела?» и «Как дела, Славка?» одинаково "
        "обращены к тебе. Дослушай фразу перед ответом. Не начинай разговор сам."
        if WAKE_ENABLED else
        "Непрерывный диалог включён. Отвечай на обращённую к тебе речь сразу, "
        "без слова пробуждения и без ограничения окна разговора."
    )
    return SYSTEM_PROMPT + "\n" + language + "\n" + mode


def require_api_key() -> str:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Не задан GEMINI_API_KEY (или GOOGLE_API_KEY)")
    return key


def stereo_to_mono_channel(pcm: bytes, channel: int) -> bytes:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    mono = array("h", samples[channel::INPUT_CHANNELS])
    if sys.byteorder != "little":
        mono.byteswap()
    return mono.tobytes()


def parse_pcm_rate(mime_type: str | None, default: int = 24000) -> int:
    if not mime_type:
        return default
    m = re.search(r"(?:rate|rate_hz)\s*=\s*(\d+)", mime_type, re.I)
    return int(m.group(1)) if m else default


def decode_blob_data(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return base64.b64decode(data)
    return b""


class Resampler:
    """Streaming 2x linear interpolation, including byte/chunk boundaries."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.pending = b""
        self.previous = None
        self.rate = None

    def convert(self, pcm, rate=24000):
        if rate not in (24000, 48000):
            raise RuntimeError(f"Unsupported output PCM rate: {rate}")
        if self.rate is not None and self.rate != rate:
            raise RuntimeError("PCM rate changed within an output turn")
        self.rate = rate
        raw = self.pending + pcm
        self.pending = raw[len(raw) // 2 * 2:]
        src = array("h")
        src.frombytes(raw[:len(raw) // 2 * 2])
        if sys.byteorder != "little":
            src.byteswap()
        out = array("h")
        for x in src:
            if rate == 48000:
                out.extend((x, x))
            else:
                if self.previous is not None:
                    mid = (self.previous + x) // 2
                    out.extend((self.previous, self.previous, mid, mid))
                self.previous = x
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()

    def finish(self):
        if self.pending:
            raise RuntimeError("Incomplete PCM16 sample at turn end")
        out = array("h")
        if self.rate == 24000 and self.previous is not None:
            out.extend((self.previous,) * 4)
        if sys.byteorder != "little":
            out.byteswap()
        self.reset()
        return out.tobytes()


def audio_command(mode):
    helper = os.getenv("SLAVIK_AUDIO_HELPER")
    device = MIC_SOURCE if mode == "capture" else SPEAKER_SINK
    if helper:
        return ["sudo", "-n", "-H", "-u", "voice", helper, mode, device]
    if mode == "capture":
        return ["parec", "--raw", f"--device={device}", "--format=s16le",
                "--rate=16000", "--channels=2"]
    return ["pacat", "--playback", "--raw", f"--device={device}",
            "--format=s16le", "--rate=48000", "--channels=2", "--latency-msec=40"]


async def stop_process(proc):
    if proc and proc.returncode is None:
        try:
            proc.terminate()  # sudo forwards TERM; audio helper kills its pacat child.
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 1)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


def normalize_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9\s]", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def contains_wake(text: str) -> bool:
    norm = normalize_text(text)
    return any(p.search(norm) for p in WAKE_PATTERNS)


class WakeGate:
    def __init__(self, wake_enabled=WAKE_ENABLED):
        self.wake_enabled = wake_enabled
        if AWAKE_SECONDS <= 0:
            raise ValueError("SLAVIK_AWAKE_SECONDS must be positive")
        self.awake_until = 0.0
        self.input_buf = ""
        self.command_authorized = False

    @property
    def awake(self) -> bool:
        return not self.wake_enabled or time.monotonic() < self.awake_until

    @property
    def response_allowed(self):
        return self.awake or self.command_authorized

    def complete_turn(self):
        self.clear_turn_text()
        self.command_authorized = False

    def extend(self):
        self.awake_until = time.monotonic() + AWAKE_SECONDS

    def feed_transcript(self, text: str) -> bool:
        if not self.wake_enabled:
            return False
        if not text:
            return False
        now = time.monotonic()
        if now - getattr(self, "last_input", 0) > 2:
            self.input_buf = ""
        self.last_input = now
        self.input_buf = (self.input_buf + text)[-300:]
        if contains_wake(text) or contains_wake(self.input_buf):
            self.command_authorized = True
            was_asleep = not self.awake
            self.extend()
            self.input_buf = ""
            return was_asleep
        self.command_authorized = self.awake
        if self.awake:
            self.extend()
        return False

    def clear_turn_text(self):
        self.input_buf = ""


class PulseCapture:
    def __init__(self, processed=True):
        self.proc = None
        self.processor = load_processor(MIC_SOURCE) if processed else None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *audio_command("capture"), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.15)
        if self.proc.returncode is not None:
            err = await self.proc.stderr.read()
            raise RuntimeError(f"parec не запустился: {err.decode(errors='replace')}")

    async def read_stereo(self) -> bytes:
        try:
            raw = await self.proc.stdout.readexactly(INPUT_BYTES)
        except asyncio.IncompleteReadError as e:
            err = b""
            if self.proc and self.proc.stderr:
                err = await self.proc.stderr.read()
            raise RuntimeError(f"Поток микрофона завершился: {err.decode(errors='replace')}") from e
        return raw

    async def read_mono(self) -> bytes:
        mono = stereo_to_mono_channel(await self.read_stereo(), INPUT_SELECTED_CHANNEL)
        return self.processor.process(mono) if self.processor else mono

    async def close(self):
        await stop_process(self.proc)


class PulsePlayer:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=512)  # fixed 20ms blocks, <=10.24s
        self.proc = None
        self.generation = 0
        self.resampler = Resampler()

    async def _spawn(self):
        proc = await asyncio.create_subprocess_exec(
            *audio_command("playback"), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self.proc = proc
        await asyncio.sleep(0.08)
        if proc.returncode is not None:
            err = await proc.stderr.read()
            raise RuntimeError(f"pacat failed: {err.decode(errors='replace')}")

    async def start(self):
        await self._spawn()

    def enqueue(self, pcm):
        for offset in range(0, len(pcm), 3840):
            if self.queue.full():
                raise RuntimeError("Playback queue overflow; audio device too slow")
            self.queue.put_nowait((self.generation, pcm[offset:offset + 3840]))

    async def put(self, data, src_rate=24000):
        if DEBUG_AUDIO:
            print(f"[AUDIO] {len(data)} bytes, {src_rate} Hz", flush=True)
        self.enqueue(self.resampler.convert(data, src_rate))

    async def finish_turn(self):
        self.enqueue(self.resampler.finish())

    async def flush(self):
        # No drain lock: kill playback before waiting for a blocked writer.
        self.generation += 1
        self.resampler.reset()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        old, self.proc = self.proc, None
        await stop_process(old)
        # Recreated lazily by the single writer, after its old drain exits.

    async def run(self):
        while True:
            generation, data = await self.queue.get()
            try:
                if generation != self.generation:
                    continue
                if self.proc is None:
                    await self._spawn()
                if generation != self.generation:
                    continue
                proc = self.proc
                if proc is None:
                    continue
                if proc.returncode is not None:
                    err = await proc.stderr.read()
                    raise RuntimeError(f"Playback exited: {err.decode(errors='replace')}")
                try:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    if generation == self.generation:
                        raise RuntimeError("Playback stream failed")
            finally:
                self.queue.task_done()

    async def close(self):
        await stop_process(self.proc)



async def microphone_sender(session, capture: PulseCapture):
    from google.genai import types
    while True:
        mono = await capture.read_mono()
        await session.send_realtime_input(
            audio=types.Blob(data=mono, mime_type=f"audio/pcm;rate={INPUT_RATE}")
        )


async def receiver(session, player, gate, tool_queue=None):
    audible_turn = None
    while True:
        received = False
        async for response in session.receive():
            received = True
            sc = response.server_content
            interrupted = bool(sc and getattr(sc, "interrupted", False))
            if interrupted:
                event_log("interrupted", queued_blocks=player.queue.qsize() if hasattr(player, "queue") else "unknown", audible=audible_turn, awake=gate.awake)
                await player.flush()
                audible_turn = None
                gate.clear_turn_text()
                print("\n[Перебивание: звук очищен]", flush=True)
            if sc:
                inp = getattr(sc, "input_transcription", None)
                if inp and getattr(inp, "text", None):
                    print(f"\nТы: {inp.text}", flush=True)
                    if gate.feed_transcript(inp.text):
                        print("[Славик активирован]", flush=True)
                    if getattr(inp, "finished", False):
                        gate.clear_turn_text()
            if tool_queue is not None and getattr(response, "tool_call", None):
                for call in response.tool_call.function_calls or []:
                    event_log("tool_received", tool=call.name, id=call.id, authorized=gate.response_allowed)
                    tool_queue.put_nowait((call, gate.response_allowed))
            # Freeze the gate for each model turn. Never release old hidden audio
            # after a later wake word, and never truncate an authorized long reply.
            if not interrupted:
                chunks = []
                direct = getattr(response, "data", None)
                audio = decode_blob_data(direct) if direct is not None else b""
                if audio:
                    chunks.append((audio, 24000))
                elif sc and getattr(sc, "model_turn", None):
                    for part in sc.model_turn.parts or []:
                        inline = getattr(part, "inline_data", None)
                        if inline and str(getattr(inline, "mime_type", "")).startswith("audio/pcm"):
                            chunk = decode_blob_data(inline.data)
                            if chunk:
                                chunks.append((chunk, parse_pcm_rate(inline.mime_type)))
                out = getattr(sc, "output_transcription", None) if sc else None
                if chunks or (out and getattr(out, "text", None)):
                    if audible_turn is None:
                        audible_turn = gate.response_allowed
                    if audible_turn:
                        for chunk, rate in chunks:
                            await player.put(chunk, rate)
                        if out and getattr(out, "text", None):
                            print(f"\nСлавик: {out.text}", flush=True)
            if sc and getattr(sc, "turn_complete", False):
                if audible_turn:
                    await player.finish_turn()
                audible_turn = None
                gate.complete_turn()
        if not received:
            raise RuntimeError("Live receive stream ended without a turn")



async def tool_worker(session, queue, spotify):
    from google.genai import types
    while True:
        call, authorized = await queue.get()
        event_log("tool_start", tool=call.name, id=call.id, authorized=authorized)
        try:
            if not authorized:
                result = "Команда отклонена: ассистент спит."
            elif call.name == "audio_volume":
                result = await asyncio.to_thread(audio_volume, dict(call.args or {}))
            elif call.name != "spotify_command" or spotify is None:
                result = "Неизвестный инструмент."
            else:
                try:
                    result = await asyncio.to_thread(spotify, dict(call.args or {}))
                except Exception:
                    result = "Spotify не выполнил команду. Проверьте мост и авторизацию."
            event_log("tool_result", tool=call.name, id=call.id, result=result)
            await session.send_tool_response(function_responses=[types.FunctionResponse(
                id=call.id, name=call.name, response={"result": result})])
        finally:
            queue.task_done()


async def run():
    from google import genai
    api_key = require_api_key()
    spotify = None
    if os.getenv("SPOTIFY_ENABLED", "0") == "1":
        try:
            from spotify_api import spotify_command
            spotify = spotify_command
        except ImportError:
            raise RuntimeError("SPOTIFY_ENABLED=1 but spotify_api.py/dependencies are missing") from None
    config = make_config(spotify is not None)
    tool_queue = asyncio.Queue(maxsize=32)

    capture = PulseCapture()
    player = PulsePlayer()
    gate = WakeGate()
    client = genai.Client(api_key=api_key)
    try:
        await capture.start()
        await player.start()
        async with connect_live(client, config) as session:
            print(f"[Gemini Live подключен] Model={MODEL}", flush=True)
            print(f"Режим: пробуждение, окно {AWAKE_SECONDS:g}s" if WAKE_ENABLED else
                  "Режим: непрерывный диалог, без слова пробуждения", flush=True)
            print(f"Mic={MIC_SOURCE}: 16k stereo -> right mono; Out={SPEAKER_SINK}: 24k mono -> 48k stereo", flush=True)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(microphone_sender(session, capture))
                tg.create_task(receiver(session, player, gate, tool_queue))
                tg.create_task(tool_worker(session, tool_queue, spotify))
                tg.create_task(player.run())
    finally:
        await capture.close()
        await player.close()
        await client.aio.aclose()


def audio_volume(args):
    import subprocess
    action = args.get("action")
    value = args.get("percent")
    if action == "set":
        if type(value) is not int or not 0 <= value <= 100:
            return "Не выполнено: громкость должна быть целым числом от 0 до 100%."
        value = str(value)
    elif action in ("up", "down"):
        value = action
    else:
        return "Не выполнено: неизвестная команда громкости."
    try:
        result = subprocess.run(
            ["sudo", "-n", "-H", "-u", "voice",
             os.getenv("SLAVIK_AUDIO_HELPER", "/usr/local/libexec/slavik-audio"), "volume", value],
            capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "Не выполнено: обновите аудиомост через bash install-linux.sh."


AUDIO_DECLARATIONS = [{"name": "audio_volume",
    "description": "Общая громкость колонки: музыка и голос Славика. Громче/тише: up/down на 10 процентов; set с percent от 0 до 100. Используй для обычных просьб о громкости.",
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "enum": ["up", "down", "set"]},
        "percent": {"type": "INTEGER", "description": "Абсолютная громкость 0..100 для set"}}, "required": ["action"]}}]
SPOTIFY_DECLARATIONS = [{"name": "spotify_command",
    "description": "Spotify: следующий/предыдущий трек next/previous, pause/resume/play_pause/stop; play_track по названию; play_playlist по названию; play_artist; play_mood для настроения (например грустная музыка: query sad songs). play_from_playlist выбирает трек в текущем плейлисте по query или номеру track_number (от 1); playlist_query выбирает другой плейлист. volume_up/down/set_volume меняют только музыку Spotify. Не утверждай успех до результата инструмента.",
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "enum": ["play_track", "play_playlist", "play_artist", "play_mood", "play_from_playlist", "pause", "resume", "play_pause", "next", "previous", "stop", "volume_up", "volume_down", "set_volume", "status"]},
        "query": {"type": "STRING", "description": "Название песни, плейлиста, исполнителя или запрос плейлиста по настроению"},
        "playlist_query": {"type": "STRING", "description": "Название плейлиста; без него используется текущий"},
        "track_number": {"type": "INTEGER", "description": "Номер трека в плейлисте от 1"},
        "percent": {"type": "INTEGER", "description": "Громкость Spotify 0..100 для set_volume"}}, "required": ["action"]}}]

@asynccontextmanager
async def connect_live(client, config):
    async with AsyncExitStack() as stack:
        try:
            session = await stack.enter_async_context(client.aio.live.connect(model=MODEL, config=config))
        except Exception as error:
            reason = str(error).lower()
            if not (config.input_audio_transcription.language_codes and
                    'unknown name' in reason and
                    ('language_codes' in reason or 'languagecodes' in reason)):
                raise
            from google.genai import types
            print('[ASR] Сервер не поддерживает language_codes; остаётся инструкция RU/EN.', flush=True)
            config.input_audio_transcription = types.AudioTranscriptionConfig()
            session = await stack.enter_async_context(client.aio.live.connect(model=MODEL, config=config))
        yield session


def make_config(enable_spotify=False):
    from google.genai import types
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=system_instruction() + " Для управления общей громкостью используй audio_volume; сообщай результат только после инструмента." + (
            " Для музыкальных просьб используй spotify_command; настроение превращай в конкретный запрос плейлиста. Для выбора другой песни из текущего плейлиста используй play_from_playlist, чтобы сохранить очередь. Сообщай об успехе только по результату инструмента."
            if enable_spotify else " Управление Spotify сейчас недоступно; не заявляй, что включил музыку."
        ),
        tools=[types.Tool(function_declarations=AUDIO_DECLARATIONS + (SPOTIFY_DECLARATIONS if enable_spotify else []))],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=["ru-RU", "en-US"]
            if os.getenv("GEMINI_LANGUAGE_HINTS", "1") == "1" else None
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=500,
                silence_duration_ms=700,
            ),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            turn_coverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
        ),
    )


async def diagnostic(mode):
    if mode == "audio":
        proc = await asyncio.create_subprocess_exec(
            *audio_command("playback"), stdin=asyncio.subprocess.PIPE)
        samples = array("h")
        for i in range(OUTPUT_RATE):
            envelope = min(1, i / 960, (OUTPUT_RATE - 1 - i) / 960)
            x = int(2200 * envelope * math.sin(2 * math.pi * 440 * i / OUTPUT_RATE))
            samples.extend((x, x))
        if sys.byteorder != "little":
            samples.byteswap()
        try:
            await asyncio.wait_for(proc.communicate(samples.tobytes()), 5)
            if proc.returncode:
                raise RuntimeError("Audio test failed")
            print("Tone sent: 440 Hz, 1 second, 48k stereo. Confirm hearing it.")
        finally:
            await stop_process(proc)
    else:
        capture = PulseCapture(processed=False)
        processor = load_processor(MIC_SOURCE)
        try:
            await capture.start()
            for _ in range(10):
                raw = b"".join([await asyncio.wait_for(capture.read_mono(), 3) for _ in range(12)])
                from mic_processing import samples
                original = samples(raw)
                processed = samples(processor.process(raw))
                raw_peak = max(abs(x) for x in original)
                raw_clips = sum(abs(x) >= 32760 for x in original)
                rms = math.sqrt(sum(x*x for x in processed) / len(processed))
                print(f"Right mic: raw_peak={raw_peak}, raw_clipped={raw_clips}/{len(original)}, "
                      f"processed_RMS={rms:.1f}, limiter_frames={processor.limited_frames}", flush=True)
        finally:
            await capture.close()



async def wait_enter(prompt):
    print(prompt, end='', flush=True)
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    def readable():
        if not ready.done(): ready.set_result(None)
    loop.add_reader(sys.stdin.fileno(), readable)
    try:
        await ready
        if not sys.stdin.readline():
            raise RuntimeError('Для калибровки нужен интерактивный терминал.')
    finally:
        loop.remove_reader(sys.stdin.fileno())


async def calibrate_mic():
    from mic_processing import HighPass, frame_stats, calibration, save_profile, DEFAULT_PATH
    async def record(label, seconds):
        await wait_enter(label + " Нажмите Enter, когда готовы: ")
        capture = PulseCapture(processed=False)
        filters = [HighPass(), HighPass()]
        frames = [[], []]
        try:
            await capture.start()
            for i in range(seconds * 25):
                raw = await asyncio.wait_for(capture.read_stereo(), 3)
                for channel in (0, 1):
                    frames[channel].append(frame_stats(stereo_to_mono_channel(raw, channel), filters[channel]))
                if i % 25 == 0:
                    print(f"[Калибровка] осталось {seconds - i // 25}s", flush=True)
        finally:
            await capture.close()
        return frames
    print('Калибровка AEC-микрофона без Gemini. Никакая запись речи на диск не сохраняется.')
    noise = await record('Этап 1: 3 секунды тишины, обычный фоновый шум.', 3)
    speech = await record('Этап 2: 10 секунд говорите с обычного расстояния. '
                          'Например: «Славик, включи музыку. Hello Slavik, play some music».', 10)
    profile = calibration(noise[1], speech[1], MIC_SOURCE)
    print(f"Правый канал: SNR={profile['snr_db']}dB, речь RMS={profile['speech_rms']}, "
          f"шум RMS={profile['noise_rms']}, gain={profile['gain']}, "
          f"clipping={100*profile['raw_clipping_fraction']:.3f}%", flush=True)
    from mic_processing import percentile
    left = percentile([x['rms'] for x in speech[0]], .8)
    right = percentile([x['rms'] for x in speech[1]], .8)
    if left > 2 * max(right, 1):
        print('Левый канал заметно громче правого. Канал не переключён; проверьте расположение микрофона.')
    if profile['snr_db'] < 10:
        print('Низкий SNR: усиление увеличивает также шум. Уменьшите шум/расстояние и повторите.')
    path = os.getenv('SLAVIK_MIC_CALIBRATION', str(DEFAULT_PATH))
    save_profile(profile, path)
    print(f'Профиль сохранён: {path}. Он применяется при следующем запуске.', flush=True)


async def entry(args):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        if args.diagnose:
            from audio_diagnostics import print_diagnostics
            await asyncio.to_thread(print_diagnostics)
        elif args.spotify_login or args.spotify_status or args.spotify_next or args.spotify_previous or args.spotify_playlist_track is not None:
            from spotify_api import login, spotify_command
            if args.spotify_login:
                import threading
                stopped = threading.Event()
                try:
                    await asyncio.to_thread(login, stopped)
                finally:
                    stopped.set()
            else:
                action = "play_from_playlist" if args.spotify_playlist_track is not None else "next" if args.spotify_next else "previous" if args.spotify_previous else "status"
                request = {"action": action}
                if args.spotify_playlist_track is not None:
                    request["track_number"] = args.spotify_playlist_track
                result = await asyncio.to_thread(spotify_command, request)
                if result.startswith("Не выполнено:"):
                    raise RuntimeError(result)
                print(result)
        elif args.calibrate_mic:
            await calibrate_mic()
        else:
            await (diagnostic("audio" if args.audio_test else "mic")
                   if args.audio_test or args.mic_test else run())
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--diagnose", action="store_true")
    modes.add_argument("--audio-test", action="store_true")
    modes.add_argument("--mic-test", action="store_true")
    modes.add_argument("--calibrate-mic", action="store_true")
    modes.add_argument("--spotify-login", action="store_true")
    modes.add_argument("--spotify-status", action="store_true")
    modes.add_argument("--spotify-next", action="store_true")
    modes.add_argument("--spotify-previous", action="store_true")
    modes.add_argument("--spotify-playlist-track", type=int, metavar="NUMBER")
    args = parser.parse_args()
    try:
        asyncio.run(entry(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nОстановлено.")
    except Exception as error:
        def report(exc):
            if isinstance(exc, BaseExceptionGroup):
                for child in exc.exceptions:
                    report(child)
            elif type(exc) is RuntimeError or (type(exc).__module__ == "spotify_api" and type(exc).__name__ == "SpotifyError"):
                print(f"Ошибка: {exc}", file=sys.stderr)
            else:
                print(f"Ошибка: {type(exc).__name__}; код={getattr(exc, 'code', '—')}. "
                      "Проверьте сеть, модель и ключ. Ответ API скрыт.", file=sys.stderr)
        report(error)
        raise SystemExit(1)



if __name__ == "__main__":
    main()
