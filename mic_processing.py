"""Low-cost DSP and measured calibration; no local VAD or muting."""
import json
import math
import os
import sys
import tempfile
from array import array
from pathlib import Path

RATE = 16000
CHANNEL = 1
SCHEMA = 1
DEFAULT_PATH = Path.home() / '.config/slavik/mic-calibration.json'


def samples(pcm):
    result = array('h')
    result.frombytes(pcm)
    if sys.byteorder != 'little':
        result.byteswap()
    return result


def pcm(samples_):
    result = array('h', samples_)
    if sys.byteorder != 'little':
        result.byteswap()
    return result.tobytes()


class HighPass:
    def __init__(self):
        self.alpha = math.exp(-2 * math.pi * 60 / RATE)
        self.previous_input = 0.0
        self.previous_output = 0.0

    def filter(self, values):
        out = []
        for x in values:
            y = self.alpha * (self.previous_output + x - self.previous_input)
            self.previous_input = x
            self.previous_output = y
            out.append(y)
        return out


class InputProcessor:
    def __init__(self, gain=1.0):
        if not math.isfinite(gain) or not .25 <= gain <= 4:
            raise ValueError('Mic gain must be between 0.25 and 4')
        self.channel = CHANNEL
        self.gain = gain
        self.highpass = HighPass()
        self.limited_frames = 0
        self.enhancer = None

    def process(self, raw):
        values = self.highpass.filter(samples(raw))
        peak = max((abs(x) for x in values), default=0)
        gain = min(self.gain, 30000 / max(peak, 1))
        if gain < self.gain:
            self.limited_frames += 1
        output = pcm(max(-32768, min(32767, round(x * gain))) for x in values)
        return self.enhancer.process(output) if self.enhancer else output


def frame_stats(raw, filter_):
    values = samples(raw)
    filtered = filter_.filter(values)
    return {
        'rms': math.sqrt(sum(x*x for x in filtered) / max(1, len(filtered))),
        'peak': max((abs(x) for x in filtered), default=0),
        'raw_peak': max((abs(x) for x in values), default=0),
        'clipped': sum(abs(x) >= 32760 for x in values),
        'count': len(values),
        'dc': sum(values) / max(1, len(values)),
    }


def percentile(values, quantile):
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, int((len(ordered)-1)*quantile))]


def calibration(noise, speech, source):
    noise_rms = max(1, percentile([x['rms'] for x in noise], .8))
    active = [x for x in speech if x['rms'] > max(30, noise_rms * 2)]
    if len(active) < 25:  # at least one second of reasonably audible speech
        raise RuntimeError('Недостаточно речи выше шума. Повторите калибровку ближе к микрофону.')
    rms = percentile([x['rms'] for x in active], .6)
    snr = 20 * math.log10(rms / noise_rms)
    clipping = sum(x['clipped'] for x in speech) / max(1, sum(x['count'] for x in speech))
    if clipping > .005:
        raise RuntimeError('Вход перегружен (>0.5% отсчётов). Уменьшите аппаратное усиление/отодвиньтесь и повторите.')
    peak = max(x['peak'] for x in speech)
    gain = max(.25, min(4, 2600 / max(rms, 1), 28000 / max(peak, 1)))
    return {'schema': SCHEMA, 'source': source, 'rate': RATE, 'channel': CHANNEL,
            'gain': round(gain, 4), 'noise_rms': round(noise_rms, 1),
            'speech_rms': round(rms, 1), 'snr_db': round(snr, 1),
            'raw_clipping_fraction': clipping, 'speech_peak': round(peak, 1)}


def best_calibration(noise, speech, source):
    profiles, errors = [], []
    for channel in (0, 1):
        try:
            profile = calibration(noise[channel], speech[channel], source)
            profile['channel'] = channel
            profiles.append(profile)
        except RuntimeError as error:
            errors.append(str(error))
    if not profiles:
        raise RuntimeError('Оба микрофона не прошли калибровку: ' + '; '.join(errors))
    # Prefer right on a near tie to avoid changing channels for measurement noise.
    best = max(profiles, key=lambda p: p['snr_db'])
    right = next((p for p in profiles if p['channel'] == 1), None)
    if right and best['snr_db'] - right['snr_db'] < 3:
        best = right
    return best


def save_profile(profile, path=DEFAULT_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.mic-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def enhance_processor(processor):
    mode = os.getenv('SLAVIK_MIC_ENHANCEMENT', 'off')
    if mode not in ('off', 'speex'):
        raise RuntimeError('SLAVIK_MIC_ENHANCEMENT: off или speex.')
    if mode == 'speex':
        from audio_enhancement import SpeexEnhancer
        try:
            processor.enhancer = SpeexEnhancer()
        except OSError:
            raise RuntimeError('SpeexDSP не установлен. Выполните bash install-linux.sh или выберите --mic-enhancement off.') from None
        agc = 'AGC до +12dB' if processor.enhancer.agc_enabled else 'AGC недоступен в этой сборке'
        print(f'[Mic] SpeexDSP после AEC: шумодав -15dB, {agc}, кадр20ms.', flush=True)
    return processor


def load_processor(source):
    override = os.getenv('SLAVIK_MIC_GAIN')
    path = Path(os.getenv('SLAVIK_MIC_CALIBRATION', str(DEFAULT_PATH)))
    if not path.exists():
        print('[Mic] Нет калибровки: gain=1, high-pass=60Hz. Запустите --calibrate-mic.', flush=True)
        return enhance_processor(InputProcessor(float(override) if override else 1.0))
    try:
        profile = json.loads(path.read_text())
        if (profile['schema'], profile['source'], profile['rate']) != (SCHEMA, source, RATE) or type(profile['channel']) is not int or profile['channel'] not in (0, 1):
            raise ValueError('Calibration device/format mismatch')
        processor = InputProcessor(float(override or profile['gain']))
        processor.channel = profile['channel']
    except (KeyError, ValueError, TypeError, OSError):
        print('[Mic] Профиль не подходит: gain=1. Повторите --calibrate-mic.', flush=True)
        return enhance_processor(InputProcessor(float(override) if override else 1.0))
    print(f"[Mic] channel={processor.channel}, gain={processor.gain:g}, SNR={profile.get('snr_db', '?')}dB, high-pass=60Hz", flush=True)
    return enhance_processor(processor)


class NearSpeech:
    """Energy cue for brief music ducking only; never authorizes a command."""
    def __init__(self):
        self.until = 0.0
        self.active_samples = 0
        self.threshold = 180.0

    def feed(self, raw, now):
        values = samples(raw)
        if not values:
            return
        mean = sum(values) / len(values)
        rms = math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))
        if rms >= self.threshold:
            self.active_samples += len(values)
            if self.active_samples >= RATE * .12:
                self.until = now + 2.0
        else:
            self.active_samples = 0
