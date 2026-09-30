"""Audio: beeps, device lookup, TEN VAD wrapper, loudness normalisation, block coalescing."""

import math
from datetime import datetime

import numpy as np
import sounddevice as sd
from ten_vad import TenVad

from .config import AppConfig

SAMPLE_RATE = 16000

# Coalescing.  A pause longer than this stops being context the model can use
# and starts being dead air inside the block, so the contiguous span is not
# carried across it: the block ends and the next one starts after the pause.
# 10 s is well past the longest gap seen inside a measured block (6.3 s).
COALESCE_MAX_GAP_SAMPLES = 10 * SAMPLE_RATE

# Loudness normalisation of the audio handed to the model (see
# normalize_for_model).  The cap is what keeps near-silence from being
# amplified into hallucination fuel: +20 dB lifts real but distant speech
# (-37 to -42 dBFS, where this user's natural dictation sits) into the range
# the model was trained on, and stops well short of turning a -70 dBFS noise
# floor into something that decodes.
NORMALIZE_MAX_GAIN_DB = 20.0
NORMALIZE_NOISE_FLOOR_DBFS = -55.0   # below this a block is noise, not speech
NORMALIZE_PEAK_DBFS = -1.0           # gain is scaled back to keep the peak here


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _generate_tone(freq: float, duration: float, volume: float) -> np.ndarray:
    t = np.linspace(0, duration, int(SAMPLE_RATE * duration), dtype=np.float32)
    tone = (volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    fade = min(int(SAMPLE_RATE * 0.01), len(tone) // 2)
    if fade > 0:
        tone[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        tone[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    return tone


def _is_night_mode(config: "AppConfig") -> bool:
    """Check if current time falls within night mode hours."""
    if not config.night_mode:
        return False
    hour = datetime.now().hour
    if config.night_start > config.night_end:
        # Wraps midnight: e.g. 22-9 means 22,23,0,1,...,8
        return hour >= config.night_start or hour < config.night_end
    else:
        return config.night_start <= hour < config.night_end


# Global config ref for beep functions (set in main)
_active_config: "AppConfig | None" = None


def play_beep_start(volume: float = 0.5):
    """Rising tone — dictation started."""
    if _active_config and _is_night_mode(_active_config):
        return
    sd.play(_generate_tone(880, 0.15, volume), samplerate=SAMPLE_RATE)


def play_beep_stop(volume: float = 0.5):
    """Falling tone — dictation stopped."""
    if _active_config and _is_night_mode(_active_config):
        return
    sd.play(_generate_tone(440, 0.15, volume), samplerate=SAMPLE_RATE)


def play_beep_pause(volume: float = 0.5):
    """Double short beep — paused/resumed."""
    if _active_config and _is_night_mode(_active_config):
        return
    t1 = _generate_tone(660, 0.07, volume)
    gap = np.zeros(int(SAMPLE_RATE * 0.05), dtype=np.float32)
    t2 = _generate_tone(660, 0.07, volume)
    sd.play(np.concatenate([t1, gap, t2]), samplerate=SAMPLE_RATE)


# ---------------------------------------------------------------------------
# Audio device helpers
# ---------------------------------------------------------------------------

def list_input_devices() -> list[dict]:
    """Return a list of input-capable audio devices."""
    result = []
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            result.append({"index": i, "name": dev["name"], "channels": dev["max_input_channels"]})
    return result


def resolve_audio_device(config_value: str):
    """Convert config audio_device string to a sounddevice device index or None."""
    if not config_value:
        return None
    try:
        return int(config_value)
    except ValueError:
        for dev in list_input_devices():
            if config_value in dev["name"]:
                return dev["index"]
        return None


# ---------------------------------------------------------------------------
# TEN VAD wrapper — lightweight voice activity detection (~306 KB)
# Provides segment-based interface compatible with the ASR engine.
# ---------------------------------------------------------------------------

class _SpeechSegment:
    """A completed speech segment with audio samples.

    `lead` is the audio the VAD threw away between the previous segment and
    this one — the part of the pause that ran past `min_silence_duration`.  It
    is what lets the coalescer rebuild the CONTIGUOUS span across two segments
    instead of splicing their speech together; `None` means no contiguous span
    is available across this boundary (nothing was retained, or the pause ran
    longer than COALESCE_MAX_GAP_SAMPLES), which the coalescer treats as a hard
    block break.
    """
    __slots__ = ("samples", "lead")

    def __init__(self, samples: np.ndarray, lead=None):
        self.samples = samples
        self.lead = lead


class TenVadDetector:
    """TEN VAD wrapper that accumulates speech and yields segments on silence."""

    def __init__(self, threshold: float = 0.5, min_silence_duration: float = 0.8,
                 min_speech_duration: float = 0.25, max_speech_duration: float = 30.0,
                 sample_rate: int = 16000):
        self._hop_size = 256  # ~16ms at 16kHz — TEN VAD optimal
        self._threshold = threshold
        self._sample_rate = sample_rate
        self._min_silence_samples = int(min_silence_duration * sample_rate)
        self._min_speech_samples = int(min_speech_duration * sample_rate)
        self._max_speech_samples = int(max_speech_duration * sample_rate)

        self._vad = TenVad(hop_size=self._hop_size, threshold=threshold)

        # Internal state
        self._buffer: list = []  # float32 hop-sized chunks for ASR
        self._int16_remainder = np.array([], dtype=np.int16)  # leftover for VAD
        self._float_remainder = np.zeros(0, dtype=np.float32)  # its float twin
        self._in_speech = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._segments: list[_SpeechSegment] = []
        self._is_speech = False
        # Non-speech audio since the last segment closed, kept as float32
        # chunks (4 bytes a sample, not a 32-byte Python float) so retaining
        # seconds of it costs nothing.  `None` = the pause outgrew the carry
        # cap, so there is no contiguous span left to offer.
        self._gap: list | None = []
        self._gap_samples = 0
        self._max_gap_samples = COALESCE_MAX_GAP_SAMPLES
        self._lead = None               # gap that precedes the open segment

    def accept_waveform(self, samples: list[float]):
        """Feed float32 audio samples (matching sounddevice output)."""
        # Convert to int16 for TEN VAD
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        int16_data = (arr * 32767).astype(np.int16)

        # Prepend any leftover from the previous call.  BOTH views, and always
        # together: a 100 ms block is 6.25 hops, so there is a leftover on
        # every call, and indexing the float audio with the int16 loop's `i`
        # (as this did) shifted the two apart and dropped the 64-sample tail of
        # every block — 4 % of the take never reached the model, and what did
        # reach it was spliced.  Keeping one pair of remainders makes the
        # buffered audio exactly the captured audio.
        if len(self._int16_remainder) > 0:
            int16_data = np.concatenate([self._int16_remainder, int16_data])
            arr = np.concatenate([self._float_remainder, arr])

        # Process in hop_size chunks
        i = 0
        while i + self._hop_size <= len(int16_data):
            chunk = int16_data[i:i + self._hop_size]
            prob, _flag = self._vad.process(chunk)
            is_speech = prob >= self._threshold

            float_chunk = arr[i:i + self._hop_size].copy()

            if is_speech:
                self._is_speech = True
                self._silence_samples = 0
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_samples = 0
                    self._lead = self._take_gap()
                self._buffer.append(float_chunk)
                self._speech_samples += self._hop_size

                # Force segment if max duration reached
                if self._speech_samples >= self._max_speech_samples:
                    self._emit_segment()
            else:
                if self._in_speech:
                    self._buffer.append(float_chunk)
                    self._silence_samples += self._hop_size
                    if self._silence_samples >= self._min_silence_samples:
                        self._emit_segment()
                else:
                    self._is_speech = False
                    self._remember_gap(float_chunk)

            i += self._hop_size

        # Save leftover
        self._int16_remainder = int16_data[i:]
        self._float_remainder = arr[i:]

    def _emit_segment(self, force: bool = False):
        """Finalize current speech buffer into a segment.

        `force` is the end-of-take flush: whatever is still buffered is what
        the user just said, so the only floor allowed to drop it is the
        recognizer's own MIN_SEGMENT_SAMPLES one, applied at submit time.

        The floor counts SPEECH hops, not the buffer: on every silence-close
        the buffer already holds min_silence_duration of trailing silence, so
        measured against it the floor was dead and a single VAD blip became
        a ~0.8 s near-silent segment that, normalised by +20 dB, decoded to
        hallucinated words.
        """
        if force or self._speech_samples >= self._min_speech_samples:
            self._segments.append(_SpeechSegment(np.concatenate(self._buffer), self._lead))
        elif self._lead is not None:
            # A blip too short to be speech: what was buffered is part of the
            # pause it interrupted, so it goes back into the gap and the
            # contiguous span across that pause stays whole for the coalescer.
            self._gap = [self._lead, *self._buffer]
            self._gap_samples = len(self._lead) + len(self._buffer) * self._hop_size
            if self._gap_samples > self._max_gap_samples:
                self._gap = None
        else:
            self._gap = None      # the pause was already past the carry cap
        self._lead = None
        self._buffer = []
        self._in_speech = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._is_speech = False

    def _remember_gap(self, chunk):
        """Keep one hop of the pause, for the coalescer's contiguous span."""
        if self._gap is None:
            return
        self._gap.append(chunk)
        self._gap_samples += len(chunk)
        if self._gap_samples > self._max_gap_samples:
            self._gap = None          # too long to belong inside one block

    def _take_gap(self):
        gap, self._gap, self._gap_samples = self._gap, [], 0
        if gap is None:
            return None
        return (np.concatenate(gap) if gap
                else np.zeros(0, dtype=np.float32))

    def open_tail(self, max_samples: int):
        """Tail of the phrase that has NOT closed yet — preview use only.

        A copy, so the caller can decode it while the capture thread keeps
        extending the buffer.  Never feeds an insertion: the document only ever
        gets text decoded from a closed block.
        """
        buf = self._buffer
        if not buf:
            return np.zeros(0, dtype=np.float32)
        if max_samples:
            chunks = -(-max_samples // self._hop_size)        # ceil(max / hop)
            buf = buf[-chunks:]
        audio = np.concatenate(buf)
        return audio[-max_samples:] if max_samples and audio.size > max_samples else audio

    def is_speech_detected(self) -> bool:
        return self._is_speech

    def empty(self) -> bool:
        return len(self._segments) == 0

    @property
    def front(self) -> _SpeechSegment:
        return self._segments[0]

    def pop(self):
        self._segments.pop(0)

    def flush(self) -> bool:
        """Close the pending buffer into a segment.  True if one was emitted.

        A release that lands mid-phrase leaves the whole phrase sitting here
        with no trailing silence to cut it, so this must never be a no-op:
        dropping it drops exactly the words the user was still saying.
        """
        if not self._buffer:
            return False
        self._emit_segment(force=True)
        return True


def normalize_for_model(samples, target_dbfs: float = -18.0):
    """Loudness-normalise ONE decode block.  Returns (audio, info).

    FOR THE MODEL ONLY.  The gain is applied to the float32 copy handed to the
    recognizer and to nothing else: if per-session audio saving is ever added it
    must write the RAW samples, or the saved recording stops being what the
    microphone actually heard.

    Why: the read-aloud measurements were made at -13 dBFS RMS, but this user's
    natural dictation sits at -37 to -42 dBFS — they speak from further away in
    real use — and at that level the model returns half-transliterated word
    salad ("Dennotmer цей прев", "Everyсен It was Open for US Observer").
    Bringing a block to about -18 dBFS fixed those.

    The gain is bounded three ways, because an unbounded one turns silence into
    a hallucination generator:
      * never more than NORMALIZE_MAX_GAIN_DB, and never below 0 dB — a block
        already at or above the target is passed through unchanged,
      * nothing below NORMALIZE_NOISE_FLOOR_DBFS is lifted at all (that is room
        noise, not quiet speech),
      * scaled back if it would push the peak past NORMALIZE_PEAK_DBFS, so a
        block with one loud syllable is never clipped into distortion.
    """
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    info = {"gain_db": 0.0, "rms_in_db": -120.0, "rms_out_db": -120.0,
            "clipped": 0}
    if not audio.size:
        return audio, info
    rms_in = float(np.sqrt(np.mean(audio ** 2)))
    if not (rms_in > 0.0) or not math.isfinite(rms_in):
        return audio, info
    rms_in_db = 20.0 * math.log10(rms_in)
    info["rms_in_db"] = round(rms_in_db, 1)
    info["rms_out_db"] = round(rms_in_db, 1)
    if rms_in_db < NORMALIZE_NOISE_FLOOR_DBFS:
        # Digital noise: leave it exactly as captured.  A block this quiet
        # decodes to nothing, which is the correct answer for it.
        return audio, info
    # Boost only.  Attenuating a loud take cost ~2 errors on the read-aloud
    # benchmark (19.5 % -> 21.2 % WER); every measured win came from lifting
    # quiet speech, so a block already at or above the target is left alone.
    gain_db = max(0.0, min(target_dbfs - rms_in_db, NORMALIZE_MAX_GAIN_DB))
    gain = 10.0 ** (gain_db / 20.0)
    peak = float(np.max(np.abs(audio)))
    ceiling = 10.0 ** (NORMALIZE_PEAK_DBFS / 20.0)
    # The ceiling only matters when we are actually boosting; an unboosted block
    # must reach the model exactly as captured, peaks and all.
    if gain_db > 0.0 and peak * gain > ceiling:
        gain = ceiling / peak
        gain_db = 20.0 * math.log10(gain)
        info["clipped"] = 1
    if abs(gain_db) < 0.1:
        return audio, info
    out = audio * np.float32(gain)
    info["gain_db"] = round(gain_db, 1)
    info["rms_out_db"] = round(rms_in_db + gain_db, 1)
    return out, info


class _BlockCoalescer:
    """Groups consecutive VAD segments into one decode block.

    The VAD keeps deciding where phrases END — this only decides how many of
    those phrases the model reads in one call, which is where the accuracy
    comes from (24.6 % -> 19.5 % WER on the user's read-aloud take, and a
    slightly faster total decode with it).

    A block is the CONTIGUOUS span from the first segment's start to the last
    segment's end: the pauses between the phrases are part of what the model
    reads, so they are carried through (`_SpeechSegment.lead`) rather than
    spliced out.  Concatenating just the speech was measurably not what the
    A/B measured.
    """

    def __init__(self, target_s: float, sample_rate: int = SAMPLE_RATE):
        self._target = int(max(float(target_s), 0.0) * sample_rate)
        self._parts: list = []
        self._samples = 0
        self._segments = 0
        self.closed = 0          # blocks handed out — the preview's staleness seq

    @property
    def enabled(self) -> bool:
        return self._target > 0

    @property
    def pending_samples(self) -> int:
        return self._samples

    def add(self, segment) -> list:
        """Take one closed VAD segment.  Returns the blocks now ready to decode.

        Two at once is possible: a segment whose pause was too long to carry
        closes the block before it AND opens the next one.
        """
        if not self.enabled:
            # Handed straight out, but still a closed block: `closed` is the
            # preview's staleness sequence, and left unbumped here the guard
            # was inert and committed words were doubled on the panel.
            self.closed += 1
            return [(np.asarray(segment.samples, dtype=np.float32), 1)]
        ready = []
        if self._parts:
            if segment.lead is None:
                ready.append(self._close())
            elif len(segment.lead):
                self._parts.append(np.asarray(segment.lead, dtype=np.float32))
                self._samples += len(segment.lead)
        self._parts.append(np.asarray(segment.samples, dtype=np.float32))
        self._samples += len(segment.samples)
        self._segments += 1
        if self._samples >= self._target:
            ready.append(self._close())
        return ready

    def flush(self) -> list:
        """End of take: whatever is pending is what the user just said."""
        return [self._close()] if self._parts else []

    def tail(self, max_samples: int):
        """Copy of the tail of the open block — preview use only."""
        if not self._parts:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self._parts)
        return audio[-max_samples:] if max_samples and audio.size > max_samples else audio

    def _close(self):
        block = (self._parts[0] if len(self._parts) == 1
                 else np.concatenate(self._parts))
        segments = self._segments
        self._parts, self._samples, self._segments = [], 0, 0
        self.closed += 1
        return (block, segments)
