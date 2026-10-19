"""Optional native SpeexDSP after AEC, plus bounded activity hold after a wake."""
import ctypes
import os
import time
from mic_processing import samples, pcm


class SpeexEnhancer:
    """16 kHz mono, 20 ms frames; no resampling, network or speaker identity."""
    frame_samples = 320
    frame_bytes = 640

    def __init__(self, library_path=None, agc=True):
        self.state = None
        self.library = ctypes.CDLL(library_path or os.getenv('SLAVIK_SPEEX_LIBRARY', 'libspeexdsp.so.1'))
        lib = self.library
        lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_preprocess_state_init.restype = ctypes.c_void_p
        lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]
        lib.speex_preprocess_state_destroy.restype = None
        lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_preprocess_ctl.restype = ctypes.c_int
        lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16)]
        lib.speex_preprocess_run.restype = ctypes.c_int
        self.state = lib.speex_preprocess_state_init(self.frame_samples, 16000)
        if not self.state:
            raise RuntimeError('SpeexDSP allocation failed')
        try:
            # Public speex_preprocess.h ABI. Keep suppression moderate; do not
            # run a second echo canceller without an aligned playback reference.
            for request, value in ((0, 1), (18, -15)):
                setting = ctypes.c_int(value)
                if lib.speex_preprocess_ctl(self.state, request, ctypes.byref(setting)) != 0:
                    raise RuntimeError('SpeexDSP lacks required preprocessing controls')
            enabled = ctypes.c_int(int(agc))
            self.agc_enabled = agc and lib.speex_preprocess_ctl(self.state, 2, ctypes.byref(enabled)) == 0
            if self.agc_enabled:
                for request, value in ((46, 4000), (26, 6), (28, -30), (30, 12)):
                    setting = ctypes.c_int(value)
                    if lib.speex_preprocess_ctl(self.state, request, ctypes.byref(setting)) != 0:
                        raise RuntimeError('SpeexDSP lacks required AGC controls')
        except BaseException:
            self.close()
            raise

    def process(self, raw):
        if not self.state:
            raise RuntimeError('SpeexDSP is closed')
        if len(raw) % self.frame_bytes:
            raise ValueError('SpeexDSP requires whole 20ms PCM16 frames')
        output = bytearray()
        for offset in range(0, len(raw), self.frame_bytes):
            frame = (ctypes.c_int16 * self.frame_samples)(*samples(raw[offset:offset+self.frame_bytes]))
            self.library.speex_preprocess_run(self.state, frame)
            values = list(frame)
            gain = min(1, 30000 / max(1, max(abs(v) for v in values)))
            output.extend(pcm(round(v * gain) for v in values))
        return bytes(output)

    def close(self):
        if self.state:
            self.library.speex_preprocess_state_destroy(self.state)
            self.state = None


class ListenActivity:
    """VAD extends an already-addressed listening window; never grants authority."""
    def __init__(self, vad=None):
        if vad is None:
            import webrtcvad
            vad = webrtcvad.Vad(2)
        self.vad = vad
        self.started = None
        self.voiced = 0

    def feed(self, raw, gate):
        now = time.monotonic()
        waiting = gate.waiting_for_command
        capturing = gate.addressed and now < gate.command_music_until
        if not waiting and not capturing:
            self.started, self.voiced = None, 0
            return
        if self.started is None:
            self.started = now
        for offset in range(0, len(raw) - 639, 640):
            block = raw[offset:offset+640]
            values = samples(block)
            energy = sum(v*v for v in values) / 320
            speech = energy >= 80**2 and self.vad.is_speech(block, 16000)
            self.voiced = self.voiced + 1 if speech else 0
            if self.voiced >= 3:
                # Three seconds to START a command. Preserve a begun utterance
                # and allow final ASR to arrive, bounded against noisy false VAD.
                deadline = min(self.started + 15, now + 1.5)
                if waiting:
                    gate.wake_wait_until = max(gate.wake_wait_until, deadline)
                gate.awake_until = max(gate.awake_until, deadline)
