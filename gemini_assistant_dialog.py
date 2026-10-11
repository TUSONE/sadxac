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
AWAKE_SECONDS = float(os.getenv("SLAVIK_AWAKE_SECONDS", "20"))
WAKE_ENABLED = os.getenv("SLAVIK_WAKE_ENABLED", "1") == "1"
DEBUG_AUDIO = os.getenv("DEBUG_AUDIO", "0") == "1"

WAKE_PATTERNS = [
    re.compile(r"\bэй\s+славк[ао]\b", re.I),
    re.compile(r"\bславик\b", re.I),
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
    # Native-audio Live transcription has no supported forced-language setting.
    # This is a conversational language hint, not a guarantee of ASR accuracy.
    language = """Основной язык разговора — русский. По умолчанию говори по-русски.
Пользователь может говорить с акцентом и употреблять приветствия «салом»,
«салам», английские названия и сленг «братуха». Такие слова сами по себе не
означают переход на португальский или другой язык. Если речь неясна,
коротко переспроси по-русски; не выдумывай услышанное. Переключай язык только
по явной просьбе пользователя."""
    mode = (
        "Режим пробуждения включён. Правило этого режима имеет приоритет: "
        "обычно молчи; отвечай после обращения «Эй Славка», «Славик», "
        "«Ебаные славики», «Славуха». После обращения поддерживай диалог "
        f"до {AWAKE_SECONDS:g} секунд без повторения имени. После бездействия "
        "снова молчи до следующего обращения. Не начинай разговор сам."
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

    @property
    def awake(self) -> bool:
        return not self.wake_enabled or time.monotonic() < self.awake_until

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
            was_asleep = not self.awake
            self.extend()
            self.input_buf = ""
            return was_asleep
        if self.awake:
            self.extend()
        return False

    def clear_turn_text(self):
        self.input_buf = ""


class PulseCapture:
    def __init__(self):
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *audio_command("capture"), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.15)
        if self.proc.returncode is not None:
            err = await self.proc.stderr.read()
            raise RuntimeError(f"parec не запустился: {err.decode(errors='replace')}")

    async def read_mono(self) -> bytes:
        try:
            raw = await self.proc.stdout.readexactly(INPUT_BYTES)
        except asyncio.IncompleteReadError as e:
            err = b""
            if self.proc and self.proc.stderr:
                err = await self.proc.stderr.read()
            raise RuntimeError(f"Поток микрофона завершился: {err.decode(errors='replace')}") from e
        return stereo_to_mono_channel(raw, INPUT_SELECTED_CHANNEL)

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
            if tool_queue is not None and getattr(response, "tool_call", None):
                for call in response.tool_call.function_calls or []:
                    tool_queue.put_nowait((call, gate.awake))
            sc = response.server_content
            interrupted = bool(sc and getattr(sc, "interrupted", False))
            if interrupted:
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
                        audible_turn = gate.awake
                    if audible_turn:
                        for chunk, rate in chunks:
                            await player.put(chunk, rate)
                        if out and getattr(out, "text", None):
                            print(f"\nСлавик: {out.text}", flush=True)
            if sc and getattr(sc, "turn_complete", False):
                if audible_turn:
                    await player.finish_turn()
                audible_turn = None
                gate.clear_turn_text()
        if not received:
            raise RuntimeError("Live receive stream ended without a turn")



async def tool_worker(session, queue, spotify):
    from google.genai import types
    while True:
        call, authorized = await queue.get()
        try:
            if not authorized:
                result = "Команда отклонена: ассистент спит."
            elif call.name != "spotify_command":
                result = "Неизвестный инструмент."
            else:
                try:
                    result = await asyncio.to_thread(spotify, dict(call.args or {}))
                except Exception:
                    result = "Spotify не выполнил команду. Проверьте мост и авторизацию."
            await session.send_tool_response(function_responses=[types.FunctionResponse(
                id=call.id, name=call.name, response={"result": result})])
        finally:
            queue.task_done()


async def run():
    from google import genai
    api_key = require_api_key()
    client = genai.Client(api_key=api_key)
    spotify = None
    if os.getenv("SPOTIFY_ENABLED", "0") == "1":
        try:
            from spotify_api import spotify_command
            spotify = spotify_command
        except ImportError:
            raise RuntimeError("SPOTIFY_ENABLED=1 but spotify_api.py/dependencies are missing") from None
    config = make_config(spotify is not None)
    tool_queue = asyncio.Queue(maxsize=32) if spotify else None

    capture = PulseCapture()
    player = PulsePlayer()
    gate = WakeGate()
    try:
        await capture.start()
        await player.start()
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            print(f"[Gemini Live подключен] Model={MODEL}", flush=True)
            print(f"Режим: пробуждение, окно {AWAKE_SECONDS:g}s" if WAKE_ENABLED else
                  "Режим: непрерывный диалог, без слова пробуждения", flush=True)
            print(f"Mic={MIC_SOURCE}: 16k stereo -> right mono; Out={SPEAKER_SINK}: 24k mono -> 48k stereo", flush=True)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(microphone_sender(session, capture))
                tg.create_task(receiver(session, player, gate, tool_queue))
                if spotify:
                    tg.create_task(tool_worker(session, tool_queue, spotify))
                tg.create_task(player.run())
    finally:
        await capture.close()
        await player.close()
        await client.aio.aclose()


SPOTIFY_DECLARATIONS = [{'name': 'spotify_command', 'description': 'Управление Spotify по голосовой просьбе. Действия: play_track (включить трек), play_playlist (плейлист), play_artist (музыка исполнителя), radio (радио/похожая музыка), pause, resume, play_pause, next, previous, stop, volume_up, volume_down, status. Для трека/плейлиста/исполнителя передай название в query. Не утверждай, что команда выполнена, пока не получен результат инструмента.', 'parameters': {'type': 'OBJECT', 'properties': {'action': {'type': 'STRING', 'enum': ['play_track', 'play_playlist', 'play_artist', 'radio', 'pause', 'resume', 'play_pause', 'next', 'previous', 'stop', 'volume_up', 'volume_down', 'status']}, 'query': {'type': 'STRING', 'description': 'Название трека, плейлиста или исполнителя'}}, 'required': ['action']}}]

def make_config(enable_spotify=False):
    from google.genai import types
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=system_instruction() + (
            " Для музыкальных просьб используй spotify_command; сообщай об успехе только по результату инструмента."
            if enable_spotify else " Управление Spotify сейчас недоступно; не заявляй, что включил музыку."
        ),
        tools=[types.Tool(function_declarations=SPOTIFY_DECLARATIONS)] if enable_spotify else None,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=320,
                silence_duration_ms=500,
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
        capture = PulseCapture()
        try:
            await capture.start()
            for _ in range(10):
                pcm = b"".join([await asyncio.wait_for(capture.read_mono(), 3) for _ in range(12)])
                samples = array("h")
                samples.frombytes(pcm)
                if sys.byteorder != "little":
                    samples.byteswap()
                peak = max(abs(x) for x in samples)
                rms = math.sqrt(sum(x*x for x in samples) / len(samples))
                print(f"Right mic: peak={peak}, RMS={rms:.1f}, clipping={peak >= 32760}", flush=True)
        finally:
            await capture.close()


async def entry(args):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await (diagnostic("audio" if args.audio_test else "mic")
               if args.audio_test or args.mic_test else run())
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--audio-test", action="store_true")
    modes.add_argument("--mic-test", action="store_true")
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
            elif type(exc) is RuntimeError:
                print(f"Ошибка: {exc}", file=sys.stderr)
            else:
                print(f"Ошибка: {type(exc).__name__}; код={getattr(exc, 'code', '—')}. "
                      "Проверьте сеть, модель и ключ. Ответ API скрыт.", file=sys.stderr)
        report(error)
        raise SystemExit(1)



if __name__ == "__main__":
    main()
