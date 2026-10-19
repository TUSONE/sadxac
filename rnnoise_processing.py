"""Classic RNNoise with native streaming rate conversion; input/output PCM16/16k."""
import ctypes as c
import math
import os
from pathlib import Path

from mic_processing import samples, pcm


class RateConverter:
    def __init__(self, library, source_rate, target_rate):
        self.library = library
        library.speex_resampler_init.argtypes = [c.c_uint, c.c_uint, c.c_uint, c.c_int, c.POINTER(c.c_int)]
        library.speex_resampler_init.restype = c.c_void_p
        library.speex_resampler_destroy.argtypes = [c.c_void_p]
        library.speex_resampler_destroy.restype = None
        library.speex_resampler_process_int.argtypes = [c.c_void_p, c.c_uint,
            c.POINTER(c.c_int16), c.POINTER(c.c_uint), c.POINTER(c.c_int16), c.POINTER(c.c_uint)]
        library.speex_resampler_process_int.restype = c.c_int
        error = c.c_int()
        self.state = library.speex_resampler_init(1, source_rate, target_rate, 3, c.byref(error))
        if not self.state or error.value:
            self.close()
            raise RuntimeError('Speex rate converter initialization failed')

    def convert(self, values, output_count):
        if not self.state:
            raise RuntimeError('Rate converter is closed')
        incoming = (c.c_int16 * len(values))(*values)
        outgoing = (c.c_int16 * output_count)()
        used, produced = c.c_uint(len(values)), c.c_uint(output_count)
        error = self.library.speex_resampler_process_int(self.state, 0, incoming,
            c.byref(used), outgoing, c.byref(produced))
        if error or used.value != len(values) or produced.value != output_count:
            raise RuntimeError('Rate converter did not preserve streaming duration')
        return list(outgoing)

    def close(self):
        if self.state:
            self.library.speex_resampler_destroy(self.state)
            self.state = None


class SpeechGain:
    """Bounded software gain driven by RNNoise speech probability, not identity."""
    def __init__(self):
        self.gain = 1.0

    def apply(self, values, probability):
        rms = math.sqrt(sum(v*v for v in values) / max(1, len(values)))
        desired = min(4, max(.25, 2600 / max(1, rms))) if probability >= .6 and rms >= 50 else min(1, self.gain)
        # Each call is 10ms: +6dB/s and -30dB/s, with immediate peak protection.
        self.gain = min(self.gain * 10**(.06/20), max(self.gain * 10**(-.30/20), desired))
        self.gain = min(self.gain, 30000 / max(1, max(abs(v) for v in values)))
        return pcm(max(-30000, min(30000, round(v * self.gain))) for v in values)


class RNNoiseEnhancer:
    """10ms processing frames; classic-v0.1 ABI with bundled model, no downloads."""
    threaded = True  # PulseCapture runs native work outside the asyncio loop.
    frame_bytes = 320  # 160 samples at16k -> 480 native samples at48k

    def __init__(self, library_path=None, speex_path=None):
        self.state = self.up = self.down = None
        self.probability = 0.0
        self.gain = SpeechGain()
        path = library_path or os.getenv('SLAVIK_RNNOISE_LIBRARY',
            str(Path.home() / '.local/lib/slavik/librnnoise-classic.so'))
        self.library = c.CDLL(path)
        lib = self.library
        lib.rnnoise_create.argtypes = []
        lib.rnnoise_create.restype = c.c_void_p
        lib.rnnoise_destroy.argtypes = [c.c_void_p]
        lib.rnnoise_destroy.restype = None
        lib.rnnoise_process_frame.argtypes = [c.c_void_p, c.POINTER(c.c_float), c.POINTER(c.c_float)]
        lib.rnnoise_process_frame.restype = c.c_float
        try:
            self.state = lib.rnnoise_create()
            if not self.state:
                raise RuntimeError('RNNoise allocation failed')
            # Speex is ONLY a resampler here. Its denoiser/AGC are not used.
            resampler = c.CDLL(speex_path or os.getenv('SLAVIK_SPEEX_LIBRARY', 'libspeexdsp.so.1'))
            self.up = RateConverter(resampler, 16000, 48000)
            self.down = RateConverter(resampler, 48000, 16000)
        except BaseException:
            self.close()
            raise

    def process(self, raw):
        if not self.state:
            raise RuntimeError('RNNoise is closed')
        if len(raw) % self.frame_bytes:
            raise ValueError('RNNoise requires whole 10ms input frames')
        result = bytearray()
        for offset in range(0, len(raw), self.frame_bytes):
            high_rate = self.up.convert(samples(raw[offset:offset+self.frame_bytes]), 480)
            incoming = (c.c_float * 480)(*high_rate)  # RNNoise expects PCM units, not -1..1
            outgoing = (c.c_float * 480)()
            probability = float(self.library.rnnoise_process_frame(self.state, outgoing, incoming))
            if not math.isfinite(probability) or not 0 <= probability <= 1 or any(not math.isfinite(v) for v in outgoing):
                raise RuntimeError('RNNoise produced invalid samples')
            self.probability = probability
            values = [max(-32768, min(32767, round(v))) for v in outgoing]
            low_rate = self.down.convert(values, 160)
            result.extend(self.gain.apply(low_rate, probability))
        return bytes(result)

    def close(self):
        for converter in (self.up, self.down):
            if converter:
                converter.close()
        if self.state:
            self.library.rnnoise_destroy(self.state)
            self.state = None
