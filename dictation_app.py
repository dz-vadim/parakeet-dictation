#!/usr/bin/env python3
"""Parakeet Dictation — On-device voice typing with punctuation via sherpa-onnx.

Supports multiple ASR model profiles (Parakeet, Canary, Nemotron) with
configurable hotkeys and VAD-segmented or true streaming transcription.
"""

import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import cairo
import gi
import numpy as np
import sounddevice as sd
from ten_vad import TenVad

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, Gdk, Gio, GLib, Gtk

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_NAME = "Parakeet Dictation"
APP_ID = "parakeet-dictation"
CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_ID
MODELS_DIR = DATA_DIR / "models"
MODELS_JSON = APP_DIR / "models.json"
SAMPLE_RATE = 16000

# Offline (VAD-segmented) path sizing.
MIN_SEGMENT_SAMPLES = 4800   # 0.3 s — shorter segments decode to garbage
TAIL_PAD_SAMPLES = 8000      # 0.5 s of real zeros appended before decoding

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

DIAG_LOG_FILE = DATA_DIR / "diagnostics.log"
DIAG_LOG_MAX_BYTES = 5 * 1024 * 1024

# Push-to-talk timing.  The tail and the UI run on two independent clocks from
# one key release: capture keeps going a moment longer so the last word is not
# clipped, while the pill flips to "processing" immediately so the release
# still feels instant.
RELEASE_TAIL_MS = 200
PREPARING_GRACE_MS = 600

# The overlay claims the microphone is live; that claim has to be cheap to
# falsify.  The watchdog re-checks it against the engine, and the meter stops
# animating once the capture loop has stopped feeding it.
OVERLAY_WATCHDOG_MS = 2000
OVERLAY_LEVEL_STALE_S = 2.0

# Gesture states for one take (press -> hold -> release).
GESTURE_IDLE = "idle"
GESTURE_STARTING = "starting"
GESTURE_RECORDING = "recording"
GESTURE_STOPPING = "stopping"

# A stop is a three-valued decision, never a bare boolean.  A release that
# lands while the engine is still opening the stream must be HELD and applied
# when capture comes up; dropping it is what leaves the microphone on forever.
STOP_PROCEED = "proceed"
STOP_DEFER = "defer"
STOP_REJECT = "reject"


def _migrate_legacy_models():
    """Move models from APP_DIR/models to the XDG data directory if needed."""
    import shutil

    legacy = APP_DIR / "models"
    if not legacy.is_dir() or legacy == MODELS_DIR:
        return
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for item in legacy.iterdir():
        dest = MODELS_DIR / item.name
        if dest.exists():
            continue
        try:
            shutil.move(str(item), str(dest))
        except OSError:
            # Installed read-only — copy instead
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))


_migrate_legacy_models()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    # Model
    model_profile: str = "desktop"
    num_threads: int = min(os.cpu_count() or 4, 8)
    vad_threshold: float = 0.5

    # How long a silence has to run before the VAD calls a phrase finished.
    # 0.25 s cut people off mid-thought: a gap that short is not even perceived
    # as a pause (published endpointing guidance puts that at ~0.5 s, and
    # advises dictation to be more conservative still), so every stop to think
    # fragmented the utterance.  0.8 s also hands the model longer segments,
    # which is where its accuracy comes from.
    vad_min_silence: float = 0.8

    # How much audio to accumulate before calling the model.  The VAD still
    # decides WHERE phrases end — this only decides how many of those phrases
    # go to the recognizer in one call.  Measured on 120 s of the user's read
    # aloud speech (118 reference words, same audio, same engine, only the
    # blocking differing): 18 VAD segments as-is scored 24.6 % WER in 10.0 s of
    # decode, the same segments coalesced to >= 4 s blocks scored 19.5 % in
    # 8.8 s.  The longer context is what stops the multilingual model drifting
    # into the wrong orthography mid-phrase.  0 disables coalescing and decodes
    # every VAD segment on its own.
    coalesce_target_s: float = 4.0

    # Loudness-normalise each block before handing it to the model.  The read
    # aloud test sat at -13 dBFS RMS, but natural dictation from a normal
    # distance sits at -37 to -42 dBFS, and at that level the model returns
    # half-English word salad.  MODEL INPUT ONLY — see normalize_for_model.
    normalize: bool = True
    normalize_target_dbfs: float = -18.0

    # Audio
    beep_volume: float = 0.5
    audio_device: str = ""  # Empty = system default; otherwise device name or index

    # Typing method: "clipboard" (wl-copy+Ctrl+V, works on all compositors),
    #   "wtype" (needs virtual-keyboard protocol), "ydotool" (needs daemon+uinput)
    typer: str = "clipboard"

    # Hotkey mode: "toggle" (one key), "start_stop" (separate keys) or
    # "hold" (true push-to-talk: dictate only while the key is down)
    hotkey_mode: str = "toggle"

    # Night mode — suppress beeps between these hours (24h format)
    night_mode: bool = True
    night_start: int = 22  # 10 PM
    night_end: int = 9     # 9 AM

    # Streaming partial-overwrite: type partials into active window and
    # backspace-retype when the model revises.  When False, partials are
    # shown only in the status bar and text is typed on final endpoint.
    partial_overwrite: bool = True

    # Strip filler words ("um", "uh", "ehm" …) before injecting text
    filter_fillers: bool = True

    # Language (for models that support it, e.g. Canary)
    language: str = "en"

    # Text insertion.  `keep_on_clipboard` leaves the dictated text in the
    # clipboard instead of restoring what was there before the paste.
    # `paste_chord` is one of "ctrl+v", "ctrl+shift+v", "shift+insert":
    # terminals ignore Ctrl+V and need Ctrl+Shift+V, and KDE Wayland gives no
    # way to read the focused window unless kdotool is installed, so set this
    # to "ctrl+shift+v" if you dictate mainly into a terminal.
    keep_on_clipboard: bool = False
    paste_chord: str = "ctrl+v"
    terminal_paste_chord: str = "ctrl+shift+v"
    terminal_window_classes: list = field(default_factory=lambda: [
        "kitty", "konsole", "org.kde.konsole", "alacritty", "Alacritty",
        "foot", "footclient", "xterm", "wezterm", "org.wezfurlong.wezterm",
        "com.mitchellh.ghostty", "gnome-terminal-server", "terminator",
    ])

    # A take usually breaks into several VAD segments.  "per_segment" inserts
    # each one as it decodes, so text keeps flowing while the key is held;
    # "end_of_take" holds them all and inserts once when the take ends, which
    # costs one clipboard write, one paste and one undo step for the whole take
    # but shows nothing until you let go.  Per-segment is only tolerable
    # because `vad_min_silence` no longer cuts on a thinking pause.
    insert_mode: str = "per_segment"

    # Structured diagnostics log (never contains transcripts or device names)
    diagnostics: bool = True

    # Hotkey bindings (pynput format, e.g. "<ctrl>+0", "<alt>+d")
    hotkey_toggle: str = "<ctrl>+0"
    hotkey_start: str = "<ctrl>+9"
    hotkey_stop: str = "<ctrl>+8"
    hotkey_pause: str = "<ctrl>+<alt>+0"

    # Push-to-talk binding, in KDE/Qt shortcut syntax rather than pynput's.
    # Hold mode registers this with kglobalaccel directly (see
    # KGlobalAccelHotkey) because that is the only route that reports the key
    # coming back UP.
    hotkey_hold: str = "Meta+Alt+D"

    # Pill overlay — compact always-visible-while-active status capsule
    overlay: bool = True
    overlay_position: str = "bottom"  # "bottom" or "top"

    # Live transcript preview — a floating, display-only panel above the pill
    # showing the segments of the current take that have already decoded.
    # It exists because end-of-take insertion shows the user nothing until they
    # let the key go, and a partial insertion is impossible: any keystroke
    # injected while the push-to-talk key is held is reported by kglobalaccel
    # as a RELEASE of that key, which would end the take.  So the text is drawn,
    # never typed — see TranscriptPreview.
    preview: bool = True

    # Live preview decode pass.  A segment only closes on 0.8 s of silence and
    # a block only on `coalesce_target_s` of speech, so speaking continuously
    # used to leave the panel empty indefinitely (the user waited 15 s and saw
    # nothing).  On this timer the still-open audio is decoded separately and
    # shown as a running hypothesis after the committed text.  DISPLAY ONLY —
    # it never reaches the typer, the clipboard or the take's text.
    # `preview_interval_s = 0` disables the pass; `preview_window_s` bounds how
    # much of a long open phrase is re-decoded, so the cost stays constant.
    preview_interval_s: float = 1.0
    preview_window_s: float = 15.0

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

    @staticmethod
    def load() -> "AppConfig":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                return AppConfig(**{
                    k: v for k, v in data.items()
                    if k in AppConfig.__dataclass_fields__
                })
            except Exception:
                pass
        return AppConfig()


def load_model_profiles() -> dict:
    with open(MODELS_JSON) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Diagnostics — one `key=value` line per event.  Never logs transcript text or
# audio device names; segment sizes and timings only.
# ---------------------------------------------------------------------------

def _diag_scrub(value) -> str:
    """Values must stay single tokens so the line can be split on whitespace."""
    return str(value).replace(" ", "_").replace("\n", "_").replace("=", "-")


class DiagnosticLog:
    """Append-only key=value log, capped by keeping the newest complete lines."""

    def __init__(self, path: Path = DIAG_LOG_FILE, max_bytes: int = DIAG_LOG_MAX_BYTES):
        self._path = path
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._enabled = True

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)

    def log(self, event: str, **fields):
        if not self._enabled:
            return
        parts = [f"ts={datetime.now().isoformat(timespec='milliseconds')}",
                 f"event={_diag_scrub(event)}"]
        for key, value in fields.items():
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, float):
                value = f"{value:.1f}"
            parts.append(f"{key}={_diag_scrub(value)}")
        line = " ".join(parts) + "\n"
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                if self._path.stat().st_size > self._max_bytes:
                    self._trim()
        except OSError:
            pass  # diagnostics must never break dictation

    def _trim(self):
        """Rewrite the file with the newest complete lines (half the cap)."""
        keep = max(self._max_bytes // 2, 1)
        with open(self._path, "rb") as fh:
            fh.seek(-keep, os.SEEK_END)
            data = fh.read()
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1:]  # drop the partial line the seek landed in
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(self._path)


DIAG = DiagnosticLog()


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
# Filler word filter
# ---------------------------------------------------------------------------

_FILLER_RE = re.compile(
    r"\b(?:um|uh|uhm|ehm|hmm|er|ah|erm|hm)\b",
    re.IGNORECASE,
)
_MULTI_SPACE_RE = re.compile(r"  +")
_WS_RE = re.compile(r"\s+")


def filter_fillers(text: str) -> str:
    """Remove common filler words, collapse resulting double-spaces."""
    text = _FILLER_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Text typer
# ---------------------------------------------------------------------------

# Linux input event keycodes (what `ydotool key` expects):
#   29 = LEFTCTRL, 42 = LEFTSHIFT, 47 = V, 110 = INSERT
_CHORD_KEYCODES = {
    "ctrl+v": ("29:1", "47:1", "47:0", "29:0"),
    "ctrl+shift+v": ("29:1", "42:1", "47:1", "47:0", "42:0", "29:0"),
    "shift+insert": ("42:1", "110:1", "110:0", "42:0"),
}

_kdotool_path = False  # False = not probed yet, None = not installed


def _focused_window_class():
    """Class of the focused window, or None when it cannot be determined.

    KDE Wayland deliberately gives clients no way to ask which window has
    focus: KWin's D-Bus API only offers getWindowInfo(uuid) — whose reply has
    no `active` field — and queryWindowInfo(), which makes the user click a
    window.  kdotool answers it by loading a KWin script, so we use kdotool
    when it is installed and otherwise return None and let the configured
    chord decide.  Guessing from process lists would be worse than a setting.
    """
    global _kdotool_path
    if _kdotool_path is False:
        _kdotool_path = shutil.which("kdotool")
    if not _kdotool_path:
        return None
    try:
        win = subprocess.run([_kdotool_path, "getactivewindow"],
                             capture_output=True, timeout=2)
        wid = win.stdout.decode("utf-8", "replace").strip()
        if win.returncode != 0 or not wid:
            return None
        res = subprocess.run([_kdotool_path, "getwindowclassname", wid],
                             capture_output=True, timeout=2)
        if res.returncode != 0:
            return None
        return res.stdout.decode("utf-8", "replace").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


class TextTyper:
    def __init__(self, method: str = "clipboard", keep_on_clipboard: bool = False,
                 paste_chord: str = "ctrl+v",
                 terminal_paste_chord: str = "ctrl+shift+v",
                 terminal_window_classes=None):
        self._method = method
        self._keep_on_clipboard = keep_on_clipboard
        self._paste_chord = paste_chord if paste_chord in _CHORD_KEYCODES else "ctrl+v"
        self._terminal_chord = (terminal_paste_chord
                                if terminal_paste_chord in _CHORD_KEYCODES
                                else "ctrl+shift+v")
        self._terminal_classes = {c.lower() for c in (terminal_window_classes or [])}
        # Tracks how many characters of the current partial are on-screen
        # so we can backspace them before typing a revision.
        self._partial_len = 0
        # Clipboard save/restore state.  Per-segment insertion fires several
        # pastes a few hundred ms apart, which the old fire-and-forget restore
        # timer got wrong two ways: the second paste read OUR OWN dictated text
        # as "the user's clipboard" and later wrote it back (the Klipper leak
        # this code exists to prevent), and a restore landing between the next
        # wl-copy and its paste pasted the stale value instead of the segment.
        # One lock serialises the pastes, one saved value survives a whole run
        # of them, and a generation counter means only the newest restore runs.
        self._clip_lock = threading.RLock()
        self._clip_saved = None
        self._clip_restore_pending = False
        self._clip_gen = 0

    # -- low-level helpers --------------------------------------------------

    def _type_raw(self, text: str):
        """Type a string into the active window (no newline safety)."""
        try:
            if self._method == "wtype":
                subprocess.run(["wtype", "--", text], timeout=5)
            elif self._method == "ydotool":
                subprocess.run(["ydotool", "type", "--", text], timeout=5)
            else:
                self._clipboard_paste(text)
        except FileNotFoundError:
            print(f"ERROR: {self._method} not found.", file=sys.stderr)
        except subprocess.TimeoutExpired:
            pass

    def _clipboard_paste(self, text: str):
        """Put *text* on the clipboard, paste it, then restore the clipboard."""
        saved, gen = None, 0
        with self._clip_lock:
            if not self._keep_on_clipboard:
                if not self._clip_restore_pending:
                    self._clip_saved = self._read_clipboard()
                # A restore still pending means the clipboard holds the
                # PREVIOUS segment's text, not the user's, so re-reading it
                # here would save our own output and hand it back later.
                self._clip_restore_pending = True
                self._clip_gen += 1   # invalidates any restore already waiting
                gen = self._clip_gen
                saved = self._clip_saved
            subprocess.run(["wl-copy", "--", text], timeout=5)
            time.sleep(0.05)
            self._paste()
            if not self._keep_on_clipboard:
                self._restore_clipboard_later(saved, gen)

    @staticmethod
    def _read_clipboard():
        """Current clipboard text, or None if empty or not plain text."""
        try:
            # No --type: wl-paste then picks whichever text flavour the owner
            # offers.  Pinning "text/plain" fails against apps that only offer
            # "text/plain;charset=utf-8", which would clear the clipboard
            # instead of restoring it.
            res = subprocess.run(["wl-paste", "--no-newline"],
                                 capture_output=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0:
            return None
        try:
            return res.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _restore_clipboard_later(self, saved, gen: int):
        """Put the user's clipboard back ~0.5 s after the paste.

        Delayed and on a background thread: the target window reads the
        selection asynchronously, so restoring at once can race the paste, and
        blocking here would stall text delivery.  Only text survives the
        round-trip — an image that was on the clipboard is already lost by the
        time wl-copy ran, so an unreadable clipboard is cleared rather than
        left holding the dictated phrase (that is the Klipper-history leak
        this exists to prevent).
        """
        def _worker():
            time.sleep(0.5)
            with self._clip_lock:
                if gen != self._clip_gen:
                    return   # a later paste owns the clipboard; it will restore
                try:
                    if saved:
                        subprocess.run(["wl-copy", "--", saved], timeout=5)
                    else:
                        subprocess.run(["wl-copy", "--clear"], timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self._clip_restore_pending = False
                self._clip_saved = None

        threading.Thread(target=_worker, daemon=True).start()

    def _choose_chord(self):
        """Return (chord, how_it_was_chosen) for the focused window."""
        cls = _focused_window_class()
        if cls is None:
            return self._paste_chord, "config"
        if cls.lower() in self._terminal_classes:
            return self._terminal_chord, "detected"
        return self._paste_chord, "detected"

    def _paste(self):
        """Press the paste chord. ydotool reaches native Wayland windows;
        xdotool only reaches XWayland ones, so it is a fallback."""
        chord, how = self._choose_chord()
        DIAG.log("paste", chord=chord, detect=how)
        if shutil.which("ydotool"):
            subprocess.run(["ydotool", "key", *_CHORD_KEYCODES[chord]], timeout=5)
        else:
            subprocess.run(["xdotool", "key", chord], timeout=5)

    def _send_backspaces(self, count: int):
        """Erase *count* characters via repeated BackSpace key presses."""
        if count <= 0:
            return
        try:
            if self._method in ("ydotool",):
                # ydotool key accepts X11 keycodes; BackSpace = 14
                for _ in range(count):
                    subprocess.run(["ydotool", "key", "14:1", "14:0"], timeout=5)
            elif self._method == "wtype":
                for _ in range(count):
                    subprocess.run(["wtype", "-k", "BackSpace"], timeout=5)
            elif shutil.which("ydotool"):
                for _ in range(count):
                    subprocess.run(["ydotool", "key", "14:1", "14:0"], timeout=5)
            else:
                # last resort — xdotool only reaches XWayland windows
                for _ in range(count):
                    subprocess.run(["xdotool", "key", "BackSpace"], timeout=5)
        except FileNotFoundError:
            print(f"ERROR: backspace helper not found for {self._method}.", file=sys.stderr)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _sanitize(text: str) -> str:
        """Strip newlines/carriage-returns — never inject Enter."""
        return text.replace("\n", " ").replace("\r", " ").strip()

    # -- public API ---------------------------------------------------------

    def type_text(self, text: str):
        """Type final (committed) text — adds trailing space."""
        text = self._sanitize(text)
        if not text:
            return
        self._type_raw(text + " ")

    def type_partial(self, text: str):
        """Type a streaming partial, erasing the previous partial first."""
        text = self._sanitize(text)
        if not text:
            return
        # Erase whatever we typed last time
        self._send_backspaces(self._partial_len)
        self._type_raw(text)
        self._partial_len = len(text)

    def commit_partial(self, text: str):
        """Commit (finalize) a partial: erase old partial, type final + space."""
        text = self._sanitize(text)
        self._send_backspaces(self._partial_len)
        self._partial_len = 0
        if text:
            self._type_raw(text + " ")

    def reset_partial(self):
        """Discard partial tracking without erasing anything on screen."""
        self._partial_len = 0


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

    def __init__(self, samples: list[float], lead=None):
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
        self._buffer: list[float] = []  # float32 samples for ASR
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

            float_chunk = arr[i:i + self._hop_size].tolist()

            if is_speech:
                self._is_speech = True
                self._silence_samples = 0
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_samples = 0
                    self._lead = self._take_gap()
                self._buffer.extend(float_chunk)
                self._speech_samples += self._hop_size

                # Force segment if max duration reached
                if self._speech_samples >= self._max_speech_samples:
                    self._emit_segment()
            else:
                if self._in_speech:
                    self._buffer.extend(float_chunk)
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
        """
        if force or len(self._buffer) >= self._min_speech_samples:
            self._segments.append(_SpeechSegment(list(self._buffer), self._lead))
        self._lead = None
        self._buffer.clear()
        self._in_speech = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._is_speech = False

    def _remember_gap(self, chunk):
        """Keep one hop of the pause, for the coalescer's contiguous span."""
        if self._gap is None:
            return
        self._gap.append(np.asarray(chunk, dtype=np.float32))
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
        if max_samples and len(buf) > max_samples:
            buf = buf[-max_samples:]
        return np.asarray(buf, dtype=np.float32)

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


# ---------------------------------------------------------------------------
# Recognizer cache
#
# Loading the 640 MB Parakeet model costs ~1.6 s.  Doing that on every
# dictation start meant the microphone was not open yet while it loaded, so the
# first words of a take were physically lost.  One recognizer is kept alive for
# the current (kind, profile, threads) triple and rebuilt only when that triple
# changes; a single slot bounds RAM (~2 GB per loaded model).
#
# A sherpa-onnx recognizer is not safe for concurrent decoding, so *every*
# inference call site in this file holds INFERENCE_LOCK.
# ---------------------------------------------------------------------------

INFERENCE_LOCK = threading.Lock()

_cache_lock = threading.Lock()
_cached_key = None
_cached_recognizer = None


def get_recognizer(kind: str, profile_name: str, num_threads: int, build):
    """Return the cached recognizer for this key, building it at most once.

    `build` is called with the cache lock held so two starts in quick
    succession cannot load the model twice.  Returns (recognizer, load_ms);
    load_ms is 0.0 on a cache hit.
    """
    global _cached_key, _cached_recognizer
    key = (kind, profile_name, num_threads)
    with _cache_lock:
        if _cached_key == key and _cached_recognizer is not None:
            return _cached_recognizer, 0.0
        # Drop the previous model before loading the next one.
        _cached_key = None
        _cached_recognizer = None
        t0 = time.perf_counter()
        recognizer = build()
        load_ms = (time.perf_counter() - t0) * 1000
        warmup_ms = _warm_up(kind, recognizer)
        _cached_recognizer = recognizer
        _cached_key = key
    DIAG.log("recognizer_loaded", kind=kind, profile=profile_name,
             threads=num_threads, load_ms=load_ms, warmup_ms=warmup_ms)
    return recognizer, load_ms


def segment_too_short(n_samples: int) -> bool:
    """Whether a segment is below the length floor the model can handle.

    Under 0.3 s the recognizer returns garbage or raises.  Length only — no
    loudness test, because quiet-but-real speech must still be decoded.
    """
    return n_samples < MIN_SEGMENT_SAMPLES


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
    rms_in = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
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
    out = (audio * np.float32(gain)).astype(np.float32)
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


def _warm_up(kind: str, recognizer) -> float:
    """Decode half a second of silence so the first real take is not the slow one.

    The first decode after a load pays ONNX Runtime's lazy graph and arena
    initialisation; spending it here keeps it off the user's first segment.
    """
    t0 = time.perf_counter()
    try:
        silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
        with INFERENCE_LOCK:
            stream = recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, silence)
            if kind == "online":
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
            else:
                recognizer.decode_stream(stream)
    except Exception:
        return -1.0  # a failed warm-up must never block dictation
    return (time.perf_counter() - t0) * 1000


# ---------------------------------------------------------------------------
# ASR Engine — supports offline (VAD-segmented) and streaming modes
# ---------------------------------------------------------------------------

class ASREngine:
    def __init__(self, config: AppConfig, profile: dict, on_text, on_partial, on_error,
                 on_partial_type=None, on_commit_partial=None,
                 on_capture_start=None, on_level=None, on_preview=None):
        self._config = config
        self._profile = profile
        self._on_text = on_text
        self._on_partial = on_partial
        self._on_error = on_error
        self._on_partial_type = on_partial_type or (lambda t: None)
        self._on_commit_partial = on_commit_partial or (lambda t: None)
        self._on_capture_start = on_capture_start or (lambda: None)
        self._on_level = on_level or (lambda rms, speech: None)
        # Running hypothesis of the phrase still being spoken.  DRAWN, never
        # inserted — the preview pass is the only text source in the engine
        # whose output is not allowed anywhere near the typer.
        self._on_preview = on_preview
        self._running = False
        self._paused = False
        self._thread = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = NOT paused
        self._pause_event.set()
        # Set while no session has decodes outstanding.  stop() only joins the
        # capture thread, so on its own it says nothing about the segment the
        # end-of-take flush just queued; the take's insertion waits on this.
        self._drained = threading.Event()
        self._drained.set()

    def _get_model_dir(self) -> Path:
        return MODELS_DIR / self._config.model_profile

    def _ensure_models(self):
        model_dir = self._get_model_dir()
        profile_files = self._profile.get("files", {})
        missing = []
        for key, info in profile_files.items():
            fp = model_dir / info["filename"]
            if not fp.exists():
                missing.append(info["filename"])
        if missing:
            raise FileNotFoundError(
                f"Missing model files: {', '.join(missing)}\n"
                f"Run: python download_models.py {self._config.model_profile}"
            )

    def _build_offline_recognizer(self):
        import sherpa_onnx
        model_dir = self._get_model_dir()
        files = self._profile["files"]
        decoder_type = self._profile.get("decoder_type", "transducer")

        if decoder_type == "transducer":
            return sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(model_dir / files["encoder"]["filename"]),
                decoder=str(model_dir / files["decoder"]["filename"]),
                joiner=str(model_dir / files["joiner"]["filename"]),
                tokens=str(model_dir / files["tokens"]["filename"]),
                num_threads=self._config.num_threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=self._profile.get("feature_dim", 128),
                provider="cpu",
                model_type=self._profile.get("model_type", "nemo_transducer"),
                decoding_method="greedy_search",
            )
        elif decoder_type == "canary":
            return sherpa_onnx.OfflineRecognizer.from_nemo_canary(
                encoder=str(model_dir / files["encoder"]["filename"]),
                decoder=str(model_dir / files["decoder"]["filename"]),
                tokens=str(model_dir / files["tokens"]["filename"]),
                src_lang=self._config.language,
                tgt_lang=self._config.language,
                num_threads=self._config.num_threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=self._profile.get("feature_dim", 128),
                provider="cpu",
                decoding_method="greedy_search",
            )
        else:
            return sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                model=str(model_dir / files["model"]["filename"]),
                tokens=str(model_dir / files["tokens"]["filename"]),
                num_threads=self._config.num_threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=self._profile.get("feature_dim", 128),
                provider="cpu",
                decoding_method="greedy_search",
            )

    def _build_online_recognizer(self):
        import sherpa_onnx
        model_dir = self._get_model_dir()
        files = self._profile["files"]
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(model_dir / files["encoder"]["filename"]),
            decoder=str(model_dir / files["decoder"]["filename"]),
            joiner=str(model_dir / files["joiner"]["filename"]),
            tokens=str(model_dir / files["tokens"]["filename"]),
            num_threads=self._config.num_threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=self._profile.get("feature_dim", 128),
            provider="cpu",
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=1.2,
            rule3_min_utterance_length=300,
        )

    def _acquire_offline_recognizer(self):
        return get_recognizer("offline", self._config.model_profile,
                              self._config.num_threads,
                              self._build_offline_recognizer)

    def _acquire_online_recognizer(self):
        return get_recognizer("online", self._config.model_profile,
                              self._config.num_threads,
                              self._build_online_recognizer)

    def preload(self):
        """Build and warm the recognizer ahead of the first dictation start."""
        self._ensure_models()
        if self._profile.get("streaming", False):
            self._acquire_online_recognizer()
        else:
            self._acquire_offline_recognizer()

    def _build_vad(self):
        return TenVadDetector(
            threshold=self._config.vad_threshold,
            # Clamped: a config edit to 0 would cut a segment every hop.
            min_silence_duration=min(max(self._config.vad_min_silence, 0.1), 5.0),
            min_speech_duration=0.25,
            max_speech_duration=30.0,
            sample_rate=SAMPLE_RATE,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self):
        if self._running:
            return
        self._stop_event.clear()
        self._pause_event.set()
        self._paused = False
        self._drained.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        # The flag is set unconditionally: `_running` only goes true once the
        # run thread is past the model check, so gating on it dropped stops
        # that arrived during start-up and left the stream open.
        self._stop_event.set()
        self._pause_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False
        self._paused = False

    def wait_drained(self, timeout: float = 60.0) -> bool:
        """Block until the last session's decode queue has emptied."""
        return self._drained.wait(timeout)

    def pause(self):
        if not self._running:
            return
        if self._paused:
            self._paused = False
            self._pause_event.set()
            play_beep_pause(self._config.beep_volume)
            GLib.idle_add(self._on_partial, "Resumed")
        else:
            self._paused = True
            self._pause_event.clear()
            play_beep_pause(self._config.beep_volume)
            GLib.idle_add(self._on_partial, "Paused")

    def _publish_level(self, audio, speech: bool):
        """Feed the overlay meter from the samples already being captured.

        Two sub-windows per 100 ms block is the ~20 Hz the meter redraws at.
        Deliberately reuses this stream: a second InputStream on the same
        device would fight the capture loop for it.
        """
        flat = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not flat.size:
            return
        half = max(flat.size // 2, 1)
        for start in range(0, flat.size, half):
            window = flat[start:start + half]
            if not window.size:
                continue
            rms = float(np.sqrt(np.mean(window * window)))
            if not math.isfinite(rms):
                rms = 0.0
            GLib.idle_add(self._on_level, rms, speech)

    def _run(self):
        try:
            self._ensure_models()
        except Exception as e:
            self._drained.set()   # nothing will ever drain — free the waiters
            GLib.idle_add(self._on_error, str(e))
            return

        is_streaming = self._profile.get("streaming", False)
        self._running = True

        try:
            if is_streaming:
                self._run_streaming()
            else:
                self._run_offline()
        except Exception as e:
            GLib.idle_add(self._on_error, str(e))
        finally:
            # A stop() that timed out while the decoder drained can let this
            # thread outlive the start of the next session — only clear the
            # flag if no newer session thread has taken over.
            if self._thread is threading.current_thread():
                self._running = False
            # After _run_offline's own finally, so the decode worker has been
            # joined and every on_text callback is already queued.
            self._drained.set()
            play_beep_stop(self._config.beep_volume)

    def _run_offline(self):
        vad = self._build_vad()
        coalescer = _BlockCoalescer(self._config.coalesce_target_s)
        # Held around every mutation of the VAD and the coalescer, so the
        # preview thread can snapshot the still-open audio without racing the
        # capture thread that is extending it.
        live_lock = threading.Lock()
        chunk_duration = 0.1
        samples_per_chunk = int(SAMPLE_RATE * chunk_duration)

        # Bounded so a stalled decoder cannot grow the backlog without limit.
        # 16 segments is minutes of speech — far more than the model can fall
        # behind in practice — and a full queue blocks rather than drops audio.
        pending: queue.Queue = queue.Queue(maxsize=16)
        stats = {"segments": 0, "too_short": 0, "overflow": 0,
                 "queue_full": 0, "decode_ms": 0.0, "flushed": 0,
                 "preview_passes": 0, "preview_skipped": 0, "preview_ms": 0.0}
        worker = threading.Thread(target=self._decode_worker,
                                  args=(pending, stats), daemon=True)
        worker.start()

        previewer = None
        if self._on_preview and self._config.preview \
                and self._config.preview_interval_s > 0:
            previewer = threading.Thread(
                target=self._preview_worker,
                args=(vad, coalescer, live_lock, stats), daemon=True)
            previewer.start()

        def submit(samples, reason="silence", parts=1):
            """Queue a finished block for the decoder thread.

            `reason` says whether the VAD cut this on silence or the
            end-of-take flush closed it, so a swallowed tail is visible in the
            log instead of being indistinguishable from "nothing was said".
            `parts` is how many VAD segments the block coalesced.
            """
            if segment_too_short(len(samples)):
                stats["too_short"] += 1
                DIAG.log("segment_discarded", reason="too_short", source=reason,
                         dur_ms=len(samples) / SAMPLE_RATE * 1000)
                return
            stats["segments"] += 1
            if pending.full():
                stats["queue_full"] += 1
                DIAG.log("decode_queue_full", depth=pending.qsize())
            pending.put((samples, reason, parts))

        def drain(reason: str):
            """Move closed segments into blocks and queue whatever is ready.

            Under the lock only for the bookkeeping: submit() can block on a
            full queue, and blocking there with the lock held would stall the
            preview thread behind the decoder.
            """
            with live_lock:
                ready = []
                while not vad.empty():
                    ready.extend(coalescer.add(vad.front))
                    vad.pop()
            for block, parts in ready:
                submit(block, reason, parts)

        device = resolve_audio_device(self._config.audio_device)
        t_open = time.perf_counter()
        try:
            with sd.InputStream(
                device=device, channels=1, dtype="float32", samplerate=SAMPLE_RATE,
                blocksize=samples_per_chunk,
            ) as stream:
                DIAG.log("session_start", mode="offline",
                         profile=self._config.model_profile,
                         threads=self._config.num_threads,
                         stream_open_ms=(time.perf_counter() - t_open) * 1000)
                # Capture is live from here — this, not start(), is the moment
                # a deferred stop becomes applicable.
                GLib.idle_add(self._on_capture_start)
                play_beep_start(self._config.beep_volume)
                GLib.idle_add(self._on_partial, "")

                last_overflow_log = 0.0
                while not self._stop_event.is_set():
                    self._pause_event.wait(timeout=0.1)
                    if self._stop_event.is_set():
                        break
                    if self._paused:
                        continue

                    audio, overflowed = stream.read(samples_per_chunk)
                    if overflowed:
                        # `overflowed` reports samples PortAudio dropped
                        # *before* this read; the block just read is valid
                        # speech, so keep it and only count the event.
                        stats["overflow"] += 1
                        now = time.monotonic()
                        if now - last_overflow_log > 5.0:
                            last_overflow_log = now
                            DIAG.log("audio_overflow", count=stats["overflow"])
                    samples = audio.reshape(-1).tolist()
                    with live_lock:
                        vad.accept_waveform(samples)
                        speech = vad.is_speech_detected()
                    self._publish_level(audio, speech)
                    if speech:
                        GLib.idle_add(self._on_partial, "Listening...")

                    drain("silence")

                # Close whatever the VAD still holds.  A release mid-phrase
                # leaves the entire phrase pending with no silence to cut it,
                # so this is the only thing that saves the user's last words;
                # the decoder is drained in the finally block below, so they
                # still reach the document.
                with live_lock:
                    stats["flushed"] = int(bool(vad.flush()))
                    ready = []
                    while not vad.empty():
                        ready.extend(coalescer.add(vad.front))
                        vad.pop()
                    # Whatever is still accumulating goes as-is: a block that
                    # never reached the target is still the words the user just
                    # said, and only MIN_SEGMENT_SAMPLES may drop it.
                    ready.extend(coalescer.flush())
                for block, parts in ready:
                    submit(block, "flush", parts)
        finally:
            # The preview is a hypothesis about audio that has now been closed
            # and queued: drop it before the committed text arrives, so the
            # panel cannot end the take showing a guess next to the real thing.
            if previewer is not None:
                self._stop_event.set()      # the pass is timer-driven; wake it
                previewer.join(timeout=2)
                if previewer.is_alive():
                    # Still inside a decode.  It checks the stop flag before it
                    # delivers, so nothing stale can reach the panel; waiting
                    # any longer here would only delay the take's insertion.
                    DIAG.log("preview_join_timeout")
                if self._on_preview:
                    GLib.idle_add(self._on_preview, "")
            # The sentinel must be sent even if the input stream raised, or the
            # decoder thread waits on an empty queue for the life of the process.
            try:
                pending.put(None, timeout=5)
            except queue.Full:
                pass
            worker.join(timeout=60)
            if worker.is_alive():
                DIAG.log("drain_timeout", depth=pending.qsize())
            DIAG.log("session_stop", mode="offline", segments=stats["segments"],
                     discarded_short=stats["too_short"], overflow=stats["overflow"],
                     queue_full=stats["queue_full"], flushed=stats["flushed"],
                     decode_ms=stats["decode_ms"],
                     preview_passes=stats["preview_passes"],
                     preview_skipped=stats["preview_skipped"],
                     preview_ms=stats["preview_ms"])

    def _decode_worker(self, pending: "queue.Queue", stats: dict):
        """Decode finished segments off the capture thread.

        Decoding inline in the capture loop meant nothing read the input stream
        for the 0.2-0.6 s a decode takes, which is what produced the overflows
        in the first place.  One worker over a FIFO queue keeps emitted text in
        segment order, and GLib.idle_add preserves that order on delivery.
        """
        try:
            recognizer, _ = self._acquire_offline_recognizer()
        except Exception as e:
            GLib.idle_add(self._on_error, str(e))
            # Keep draining so the capture thread never blocks on a full queue.
            while pending.get() is not None:
                pass
            return

        while True:
            item = pending.get()
            if item is None:
                return
            samples, reason, parts = item
            try:
                text, decode_ms = self._decode_segment(
                    recognizer, samples,
                    normalize=self._config.normalize,
                    target_dbfs=self._config.normalize_target_dbfs)
            except Exception as e:
                DIAG.log("decode_error", err=type(e).__name__)
                continue
            stats["decode_ms"] += decode_ms
            DIAG.log("segment", dur_ms=len(samples) / SAMPLE_RATE * 1000,
                     decode_ms=decode_ms, chars=len(text), reason=reason,
                     segments=parts)
            if text:
                GLib.idle_add(self._on_text, text)

    @staticmethod
    def _prepare_audio(samples, normalize=True, target_dbfs=-18.0):
        """Build the float32 buffer the recognizer is handed.  (audio, info).

        sherpa-onnx / Parakeet path ONLY.  NVIDIA's TDT transducer needs
        trailing encoder frames before it emits its final token, and the decode
        is bounded to the audio handed in, so without real appended samples the
        last word of a segment is dropped even though it is clearly audible.
        This must NEVER be applied to a Whisper engine: trailing silence there
        makes Whisper hallucinate extra text.

        The normalisation gain lands on this copy and nowhere else — see
        normalize_for_model.
        """
        audio, info = (normalize_for_model(samples, target_dbfs) if normalize
                       else (np.asarray(samples, dtype=np.float32).reshape(-1), None))
        return np.concatenate([
            audio, np.zeros(TAIL_PAD_SAMPLES, dtype=np.float32),
        ]), info

    @staticmethod
    def _decode_prepared(recognizer, audio) -> str:
        """Run one decode.  The caller must already hold INFERENCE_LOCK."""
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()

    @staticmethod
    def _decode_segment(recognizer, samples, normalize=True, target_dbfs=-18.0):
        """Decode one block with sherpa-onnx.  Returns (text, decode_ms)."""
        audio, info = ASREngine._prepare_audio(samples, normalize, target_dbfs)
        t0 = time.perf_counter()
        with INFERENCE_LOCK:
            text = ASREngine._decode_prepared(recognizer, audio)
        decode_ms = (time.perf_counter() - t0) * 1000
        if info is not None:
            DIAG.log("normalize", **info)
        return text, decode_ms

    def _preview_worker(self, vad, coalescer, live_lock, stats):
        """Decode the still-open audio on a timer so the panel shows something.

        DISPLAY ONLY.  What this returns is a hypothesis about audio that has
        not been committed yet; it goes to the preview panel and nowhere else,
        and the document still gets text only from closed blocks.

        Three rules keep it out of the real decode's way:
          * single flight — one thread, one pass at a time, and a tick that
            arrives while a pass is running is dropped, never queued,
          * INFERENCE_LOCK is taken non-blocking and the tick is skipped if the
            decoder holds it, so a guess never queues in front of a real block,
          * the window is bounded, so the cost of a tick does not grow with the
            length of the take.

        It is not free, though: a block that closes while a pass is in flight
        waits for that pass to finish.  Measured on this machine at the 1 s
        default, the final block's decode went from ~0.27 s to ~0.7-0.9 s, and
        the take's CPU rose by several cores' worth (ONNX Runtime keeps its
        thread pool spinning between calls).  A 2-3 s cadence costs a fraction
        of that — see `preview_interval_s`.
        """
        interval = max(float(self._config.preview_interval_s), 0.2)
        window = int(max(float(self._config.preview_window_s), 1.0) * SAMPLE_RATE)
        try:
            # Blocks behind the model load on a cold first take; the take may
            # be over by the time it returns, hence the check straight after.
            recognizer, _ = self._acquire_offline_recognizer()
        except Exception:
            return          # a failed preview must never take the take down
        if self._stop_event.is_set():
            return
        last_len = 0
        showing = False
        while not self._stop_event.wait(interval):
            if self._paused:
                continue
            with live_lock:
                seq = coalescer.closed
                parts = [coalescer.tail(window), vad.open_tail(window)]
            audio = np.concatenate(parts)
            if audio.size > window:
                audio = audio[-window:]
            if audio.size < MIN_SEGMENT_SAMPLES:
                # Nothing open: the last hypothesis (if any) has been committed
                # or was too short to ever be one.
                if showing:
                    showing = False
                    GLib.idle_add(self._on_preview, "")
                last_len = 0
                continue
            if audio.size == last_len:
                continue    # no new audio since the last pass — same answer
            if not INFERENCE_LOCK.acquire(blocking=False):
                stats["preview_skipped"] += 1
                DIAG.log("preview_pass", dur_ms=audio.size / SAMPLE_RATE * 1000,
                         decode_ms=0.0, skipped=1)
                continue
            try:
                prepared, _info = self._prepare_audio(
                    audio, self._config.normalize,
                    self._config.normalize_target_dbfs)
                t0 = time.perf_counter()
                text = self._decode_prepared(recognizer, prepared)
            except Exception as e:
                DIAG.log("preview_error", err=type(e).__name__)
                continue
            finally:
                INFERENCE_LOCK.release()
            if self._stop_event.is_set():
                return      # the take ended mid-pass: this guess is history
            decode_ms = (time.perf_counter() - t0) * 1000
            last_len = audio.size
            stats["preview_passes"] += 1
            stats["preview_ms"] += decode_ms
            stale = int(coalescer.closed != seq)
            DIAG.log("preview_pass", dur_ms=audio.size / SAMPLE_RATE * 1000,
                     decode_ms=decode_ms, skipped=0, stale=stale,
                     chars=len(text))
            if stale:
                # A block closed while this was decoding: its committed text is
                # already on the way, and showing this too would double the
                # words on the panel.
                continue
            if not text:
                # The model returns nothing for some mid-word cuts.  Blanking
                # the panel on that would make the guess flicker in and out, so
                # the previous one stands until a better guess or the real
                # decode replaces it.
                continue
            showing = True
            GLib.idle_add(self._on_preview, text)

    def _run_streaming(self):
        recognizer, _ = self._acquire_online_recognizer()
        with INFERENCE_LOCK:
            stream = recognizer.create_stream()
        chunk_duration = 0.1
        samples_per_chunk = int(SAMPLE_RATE * chunk_duration)
        partial_overwrite = self._config.partial_overwrite
        overflow = 0
        speaking = False  # last decode produced text — the streaming path's
                          # only speech signal, there is no VAD here

        device = resolve_audio_device(self._config.audio_device)
        with sd.InputStream(
            device=device, channels=1, dtype="float32", samplerate=SAMPLE_RATE,
            blocksize=samples_per_chunk,
        ) as mic:
            DIAG.log("session_start", mode="streaming",
                     profile=self._config.model_profile,
                     threads=self._config.num_threads)
            GLib.idle_add(self._on_capture_start)
            play_beep_start(self._config.beep_volume)
            GLib.idle_add(self._on_partial, "")

            last_overflow_log = 0.0
            while not self._stop_event.is_set():
                self._pause_event.wait(timeout=0.1)
                if self._stop_event.is_set():
                    break
                if self._paused:
                    continue

                audio, overflowed = mic.read(samples_per_chunk)
                if overflowed:
                    # Samples were lost before this read — this block is good.
                    overflow += 1
                    now = time.monotonic()
                    if now - last_overflow_log > 5.0:
                        last_overflow_log = now
                        DIAG.log("audio_overflow", count=overflow)
                samples = audio.reshape(-1).tolist()
                self._publish_level(audio, speaking)
                stream.accept_waveform(SAMPLE_RATE, samples)

                with INFERENCE_LOCK:
                    while recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    partial = recognizer.get_result(stream).strip()
                    at_endpoint = recognizer.is_endpoint(stream)

                speaking = bool(partial)
                if partial:
                    GLib.idle_add(self._on_partial, partial)
                    if partial_overwrite:
                        GLib.idle_add(self._on_partial_type, partial)

                if at_endpoint:
                    if partial:
                        if partial_overwrite:
                            GLib.idle_add(self._on_commit_partial, partial)
                        else:
                            GLib.idle_add(self._on_text, partial)
                    with INFERENCE_LOCK:
                        recognizer.reset(stream)

        DIAG.log("session_stop", mode="streaming", overflow=overflow)

# ---------------------------------------------------------------------------
# Dictation controller
# ---------------------------------------------------------------------------

class DictationController:
    def __init__(self, config: AppConfig):
        self._config = config
        self._typer = self._make_typer(config)
        self._profiles_data = load_model_profiles()
        self._engine = None
        self._status_callback = None
        self._overlay = None
        self._gesture = GESTURE_IDLE
        self._stop_pending = False       # a release that landed mid-start
        self._tail_source = 0
        self._tail_scheduled = False
        self._take_seq = 0
        self._take_closed = 0
        self._take_texts = 0
        self._take_chunks: list = []   # decoded, not yet inserted (end_of_take)
        self._take_inserts = 0
        self._take_stop_t0 = 0.0
        self._last_insert_t = 0.0
        self._rebuild_engine()

    @staticmethod
    def _make_typer(config: AppConfig) -> TextTyper:
        return TextTyper(
            config.typer,
            keep_on_clipboard=config.keep_on_clipboard,
            paste_chord=config.paste_chord,
            terminal_paste_chord=config.terminal_paste_chord,
            terminal_window_classes=config.terminal_window_classes,
        )

    def preload(self):
        """Load and warm the model in the background so the first take is fast."""
        def _worker():
            try:
                self._engine.preload()
            except Exception as e:
                DIAG.log("preload_failed", err=type(e).__name__)

        threading.Thread(target=_worker, daemon=True).start()

    def _rebuild_engine(self):
        profile = self._profiles_data["profiles"].get(self._config.model_profile)
        if not profile:
            profile = self._profiles_data["profiles"]["desktop"]
        self._engine = ASREngine(
            self._config, profile,
            on_text=self._on_final_text,
            on_partial=self._on_partial,
            on_error=self._on_error,
            on_partial_type=self._on_partial_type,
            on_commit_partial=self._on_commit_partial,
            on_capture_start=self._on_capture_start,
            on_level=self._on_level,
            on_preview=self._on_preview_hypothesis,
        )

    def set_status_callback(self, cb):
        self._status_callback = cb

    def set_overlay(self, overlay):
        self._overlay = overlay
        # Late-bound on purpose: apply_config() replaces the engine object.
        overlay.set_capture_probe(lambda: self._engine.is_running)

    def notify(self, message: str):
        """Surface a non-fatal problem — a shortcut conflict, say — visibly.

        A binding that silently does nothing is the worst outcome here, so it
        gets the error shape and stderr both."""
        if not message:
            return
        print(f"WARNING: {message}", file=sys.stderr)
        self._overlay_state("error", message)

    def _overlay_state(self, state: str, message: str = ""):
        if self._overlay:
            self._overlay.set_state(state, message)

    def _preview_append(self, text: str):
        """Put one decoded segment on the floating preview panel.

        DISPLAY ONLY.  This call must never reach the typer: a keystroke while
        the push-to-talk key is held is reported as a release of that key, which
        is exactly why the text is held back in the first place.
        """
        if self._overlay:
            self._overlay.preview_append(text)

    def _preview_hypothesis(self, text: str):
        """Put the running guess for the open phrase on the panel.

        DISPLAY ONLY, and the only text in the app allowed to be replaced once
        shown: it is a hypothesis about audio no decoder has committed yet.  It
        never reaches the typer, the clipboard or `_take_chunks` — the document
        still gets only what _on_final_text accepts.
        """
        if self._overlay:
            self._overlay.preview_hypothesis(text)

    def _preview_reset(self):
        if self._overlay:
            self._overlay.preview_reset()

    @property
    def gesture(self) -> str:
        return self._gesture

    @property
    def _insert_at_end(self) -> bool:
        """Whether decoded text is held back until the take is over.

        Hold mode has a natural end, so holding text back is possible there at
        all; a toggle session may run for minutes, which is why per-segment
        stays the default and this stays a setting.
        """
        return self._config.insert_mode == "end_of_take"

    def _enter(self, gesture: str, reason: str):
        if gesture == self._gesture:
            return
        DIAG.log("gesture", frm=self._gesture, to=gesture, reason=reason,
                 take=self._take_seq)
        self._gesture = gesture


    @property
    def is_running(self) -> bool:
        return self._engine.is_running

    @property
    def is_paused(self) -> bool:
        return self._engine.is_paused

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def profiles(self) -> dict:
        return self._profiles_data["profiles"]

    @property
    def profiles_data(self) -> dict:
        return self._profiles_data

    def start(self):
        """Begin a take.  Safe to call from the hotkey, the tray or the window."""
        if self._gesture in (GESTURE_STARTING, GESTURE_STOPPING) or self._engine.is_running:
            DIAG.log("start_ignored", state=self._gesture, take=self._take_seq)
            return
        self._take_seq += 1
        self._enter(GESTURE_STARTING, "start")
        self._preview_reset()
        self._overlay_state("preparing")
        self._engine.start()

    def stop(self):
        """Stop a take from the tray, the window or SIGUSR1 — no release tail.

        Teardown runs off the main loop so the pill keeps animating while the
        decoder drains; `shutdown()` is the blocking form for quit paths.
        """
        if self._gesture == GESTURE_STOPPING:
            DIAG.log("stop_ignored", reason="already_stopping", take=self._take_seq)
            return
        if self._gesture == GESTURE_IDLE and not self._engine.is_running:
            DIAG.log("stop_ignored", reason="not_recording", take=self._take_seq)
            return
        self._cancel_tail()
        self._enter(GESTURE_STOPPING, "explicit")
        self._overlay_state("processing")
        self._stop_engine_async("explicit")

    def toggle(self):
        if self._gesture == GESTURE_STOPPING:
            DIAG.log("toggle_ignored", reason="already_stopping", take=self._take_seq)
            return
        if self._gesture in (GESTURE_STARTING, GESTURE_RECORDING) or self._engine.is_running:
            self.stop()
        else:
            self.start()

    def shutdown(self):
        """Blocking teardown for quit and SIGINT — the process is going away."""
        self._cancel_tail()
        self._engine.stop()
        self._typer.reset_partial()
        if self._gesture != GESTURE_IDLE:
            self._end_take("cancel", "shutdown")
        self._overlay_state("hidden")

    # --- push-to-talk gesture ---------------------------------------------

    def hold_press(self):
        """Key down.  A press while a take is in flight is a repeat, not a start."""
        if self._gesture != GESTURE_IDLE:
            DIAG.log("hold_press_ignored", state=self._gesture, take=self._take_seq)
            return
        DIAG.log("hold_press", take=self._take_seq + 1)
        self.start()

    def hold_release(self):
        """Key up.  Three-valued and idempotent — see STOP_* above."""
        decision = self._decide_stop()
        # A release that lands within a few hundred ms of an insertion is
        # suspicious: the paste chord goes out through the same virtual keyboard
        # the push-to-talk key is held on, and a release has been observed
        # ~66 ms after a mid-take paste (both in the user's own log and in a run
        # where nothing touched the keyboard).  Stamping the gap here makes that
        # coupling one grep away instead of a guess.
        since_insert = ((time.monotonic() - self._last_insert_t) * 1000
                        if self._last_insert_t else -1.0)
        DIAG.log("hold_release", decision=decision, state=self._gesture,
                 take=self._take_seq,
                 after_insert_ms=(since_insert if 0 <= since_insert < 1000 else -1))
        if decision == STOP_PROCEED:
            # UI first, capture second: the pill must not wait out the tail.
            self._overlay_state("processing")
            self._schedule_release_tail("release")
        elif decision == STOP_DEFER:
            self._stop_pending = True
            self._overlay_state("processing")
            DIAG.log("stop_deferred", take=self._take_seq)
        elif self._gesture == GESTURE_STOPPING:
            DIAG.log("stop_rejected", reason="already_stopping", take=self._take_seq)
        else:
            DIAG.log("stop_rejected", reason="not_recording", take=self._take_seq)
            self._overlay_state("error", "Nothing to stop — press and hold to dictate")

    def _decide_stop(self) -> str:
        if self._gesture == GESTURE_RECORDING:
            return STOP_PROCEED
        if self._gesture == GESTURE_STARTING:
            return STOP_DEFER      # held, never dropped; applied when capture opens
        return STOP_REJECT

    def _schedule_release_tail(self, reason: str):
        self._enter(GESTURE_STOPPING, reason)
        self._tail_scheduled = True
        DIAG.log("release_tail", scheduled=True, ms=RELEASE_TAIL_MS,
                 reason=reason, take=self._take_seq)
        self._tail_source = GLib.timeout_add(RELEASE_TAIL_MS, self._on_release_tail)

    def _on_release_tail(self):
        self._tail_source = 0
        DIAG.log("release_tail", fired=True, take=self._take_seq)
        self._stop_engine_async("release_tail")
        return GLib.SOURCE_REMOVE

    def _cancel_tail(self):
        if self._tail_source:
            GLib.source_remove(self._tail_source)
            self._tail_source = 0
            DIAG.log("release_tail", cancelled=True, take=self._take_seq)

    def _stop_engine_async(self, reason: str):
        take = self._take_seq
        self._take_stop_t0 = time.monotonic()

        def _worker():
            self._engine.stop()
            # stop() only joins the capture thread.  The segment the flush just
            # queued may still be decoding, and its text has to be in hand
            # before the take is declared over — in end-of-take mode because it
            # belongs in the one insertion, in per-segment mode because it is
            # the tail the user was still speaking when they let go.
            if not self._engine.wait_drained(60.0):
                DIAG.log("drain_wait_timeout", take=take)
            GLib.idle_add(self._finish_take, take, reason)

        threading.Thread(target=_worker, daemon=True).start()

    # --- engine events ----------------------------------------------------

    def _on_capture_start(self):
        """The input stream is open."""
        if self._overlay:
            self._overlay.capture_started()
        if self._gesture == GESTURE_STARTING:
            self._enter(GESTURE_RECORDING, "capture_open")
            if self._stop_pending:
                # The key is already up: leave the pill on "processing" rather
                # than flashing "listening" for the length of the tail.
                self._stop_pending = False
                DIAG.log("deferred_stop_applied", take=self._take_seq)
                self._schedule_release_tail("deferred_release")
            else:
                self._overlay_state("listening")
        elif self._gesture == GESTURE_IDLE:
            # Started outside the state machine (SIGUSR1 raced us, say):
            # adopt it so the next key press reads as a stop, not a start.
            self._take_seq += 1
            self._enter(GESTURE_RECORDING, "adopted")
            self._overlay_state("listening")

    def _on_level(self, rms: float, speech: bool):
        if self._overlay:
            self._overlay.push_level(rms, speech)

    # --- take termination -------------------------------------------------

    def _finish_take(self, take: int, reason: str):
        """Capture and the decoder have both drained — the take is over."""
        if take != self._take_seq or take <= self._take_closed:
            # The take already ended by another route (cancel, error) — its
            # late finisher must not re-open or re-report it.
            DIAG.log("finish_take_stale", take=take, current=self._take_seq)
            return GLib.SOURCE_REMOVE
        self._typer.reset_partial()
        self._flush_take_text()
        emitted = self._take_texts
        self._end_take("success" if emitted else "no_speech", reason)
        self._overlay_state("success" if emitted else "hidden")
        if self._status_callback:
            self._status_callback("")
        return GLib.SOURCE_REMOVE

    def _flush_take_text(self):
        """Insert everything the take held back, as one paste.

        End-of-take mode only.  This is that mode's single insertion point:
        nothing reaches the document while the key is down, so a pause to think
        cannot drop half a sentence into the middle of the previous one.
        """
        if not self._take_chunks:
            return
        segments = len(self._take_chunks)
        text = _WS_RE.sub(" ", " ".join(self._take_chunks)).strip()
        self._take_chunks = []
        if text:
            self._insert(text, segments)

    def _insert(self, text: str, segments: int):
        """The one place decoded text reaches the document.

        `waited_ms` is how long this insertion waited after the take's stop was
        initiated — 0 for a segment inserted while the key was still down.
        """
        self._take_inserts += 1
        self._last_insert_t = time.monotonic()
        waited_ms = ((time.monotonic() - self._take_stop_t0) * 1000
                     if self._take_stop_t0 else 0.0)
        DIAG.log("take_insert", mode=self._config.insert_mode,
                 index=self._take_inserts, chars=len(text), segments=segments,
                 waited_ms=waited_ms, take=self._take_seq)
        self._typer.type_text(text)
        if self._status_callback:
            self._status_callback("")

    def _end_take(self, outcome: str, reason: str):
        """The ONLY place gesture state is cleared.

        Reset belongs to the end of a take, never the start of the next one:
        clearing on the way in wipes the hold the fresh key press just
        established, and the microphone then never gets its release.
        """
        self._cancel_tail()
        if self._take_chunks:
            # Only reachable on a cancel path (quit, settings saved mid-take):
            # the take never got its insertion, so say so in the log instead of
            # losing the text without a trace.
            DIAG.log("take_discarded", segments=len(self._take_chunks),
                     chars=sum(len(t) for t in self._take_chunks),
                     outcome=outcome, take=self._take_seq)
        DIAG.log("take_end", outcome=outcome, reason=reason, take=self._take_seq,
                 texts=self._take_texts, inserts=self._take_inserts,
                 tail_scheduled=self._tail_scheduled)
        self._take_closed = self._take_seq
        self._stop_pending = False
        self._tail_scheduled = False
        self._take_texts = 0
        self._take_chunks = []
        self._take_inserts = 0
        self._take_stop_t0 = 0.0
        self._last_insert_t = 0.0
        self._enter(GESTURE_IDLE, outcome)

    def pause(self):
        self._engine.pause()
        if self._engine.is_paused:
            self._overlay_state("paused")
        elif self._gesture == GESTURE_RECORDING:
            self._overlay_state("listening")

    def apply_config(self, new_config: AppConfig):
        if self._engine.is_running or self._gesture != GESTURE_IDLE:
            # Saving settings mid-take cancels it; without ending the take here
            # the gesture would stay "recording" against an engine that no
            # longer exists, and every later key press would be ignored.
            self._cancel_tail()
            self._engine.stop()
            if self._gesture != GESTURE_IDLE:
                self._end_take("cancel", "config_change")
            self._overlay_state("hidden")
        old_profile = self._config.model_profile
        old_threads = self._config.num_threads
        self._config = new_config
        self._config.save()
        self._typer = self._make_typer(new_config)
        DIAG.set_enabled(new_config.diagnostics)
        if self._overlay:
            self._overlay.apply_config(new_config)
        # Always rebuild: the engine holds the config object it was created
        # with, so thread-count and VAD changes were silently ignored before.
        self._rebuild_engine()
        if new_config.model_profile != old_profile or new_config.num_threads != old_threads:
            self.preload()

    def _on_final_text(self, text: str):
        if self._config.filter_fillers:
            text = filter_fillers(text)
        if not text:
            return
        self._take_texts += 1
        # Drawn, not typed.  This is the only thing the user sees during an
        # end-of-take hold, and it is the reason the panel exists.
        self._preview_append(text)
        if self._insert_at_end:
            # Held, not typed — see _flush_take_text.  The status line still
            # grows so the take is visibly progressing while nothing is
            # inserted.
            self._take_chunks.append(text)
            if self._status_callback:
                self._status_callback(
                    _WS_RE.sub(" ", " ".join(self._take_chunks)).strip())
            return
        self._insert(text, 1)

    def _on_preview_hypothesis(self, text: str):
        """Engine callback for the live preview pass — see _preview_hypothesis."""
        if self._config.filter_fillers:
            text = filter_fillers(text)
        self._preview_hypothesis(text)

    def _on_partial_type(self, text: str):
        """Type a streaming partial into the active window (overwrite prev)."""
        if self._insert_at_end:
            # A preview must never insert: in end-of-take mode the document is
            # touched exactly once, after the take is over.
            return
        if self._config.filter_fillers:
            text = filter_fillers(text)
        if text:
            self._typer.type_partial(text)

    def _on_commit_partial(self, text: str):
        """Commit the streaming partial as final text."""
        if self._config.filter_fillers:
            text = filter_fillers(text)
        # One rule for the panel in both insert modes: it mirrors every final
        # result the controller accepts, and inserts none of them.
        if text:
            self._preview_append(text)
        if self._insert_at_end:
            if text:
                self._take_texts += 1
                self._take_chunks.append(text)
            return
        self._typer.commit_partial(text)
        if self._status_callback:
            self._status_callback("")

    def _on_partial(self, text: str):
        if self._status_callback:
            self._status_callback(text)

    def _on_error(self, msg: str):
        print(f"ERROR: {msg}", file=sys.stderr)
        if self._status_callback:
            self._status_callback(f"Error: {msg[:60]}")
        if self._gesture != GESTURE_IDLE:
            # Two distinct terminal paths: the engine never came up at all, or
            # it failed once it was already capturing.
            outcome = "start_failed" if self._gesture == GESTURE_STARTING else "error"
            self._end_take(outcome, "engine_error")
        self._overlay_state("error", overlay_error_message(msg))


# ---------------------------------------------------------------------------
# Global shortcuts via kglobalaccel
#
# A `[services]` entry in kglobalshortcutsrc can only ever fire once per press:
# it *launches* a .desktop file, and a launch has no counterpart when the key
# comes back up.  Registering a component of our own with kglobalaccel gives
# both globalShortcutPressed and globalShortcutReleased on the same binding,
# which is what push-to-talk needs.  No relogin is involved — that constraint
# belongs to hand-edited kglobalshortcutsrc entries, not to this path.
# ---------------------------------------------------------------------------

QT_MODIFIER_BITS = {
    "shift": 0x02000000,
    "ctrl": 0x04000000, "control": 0x04000000,
    "alt": 0x08000000,
    "meta": 0x10000000, "super": 0x10000000, "win": 0x10000000,
}

QT_NAMED_KEYS = {
    "escape": 0x01000000, "tab": 0x01000001, "backtab": 0x01000002,
    "backspace": 0x01000003, "return": 0x01000004, "enter": 0x01000005,
    "insert": 0x01000006, "delete": 0x01000007, "pause": 0x01000008,
    "print": 0x01000009, "sysreq": 0x0100000A, "clear": 0x0100000B,
    "home": 0x01000010, "end": 0x01000011, "left": 0x01000012,
    "up": 0x01000013, "right": 0x01000014, "down": 0x01000015,
    "pageup": 0x01000016, "pagedown": 0x01000017, "capslock": 0x01000024,
    "numlock": 0x01000025, "scrolllock": 0x01000026, "menu": 0x01000055,
    "space": 0x20,
}


def qt_key_sequence(binding: str) -> int:
    """Translate a KDE shortcut string ("Meta+Alt+D") into a Qt key int.

    kglobalaccel speaks QKeySequence, not X11 keysyms: modifier bits sit in the
    high bits (Meta 0x10000000, Alt 0x08000000, Ctrl 0x04000000, Shift
    0x02000000) and the key code in the low ones.  Cross-check against a
    shortcut KDE ships: Close Window reads back as 150994995 = 0x09000033 =
    Alt+F4, and Meta+Alt+D is 0x18000044.
    """
    parts = [p.strip() for p in binding.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty shortcut")
    *mods, key = parts
    value = 0
    for mod in mods:
        bit = QT_MODIFIER_BITS.get(mod.lower())
        if bit is None:
            raise ValueError(f"unknown modifier {mod!r}")
        value |= bit
    low = key.lower()
    if low in QT_NAMED_KEYS:
        return value | QT_NAMED_KEYS[low]
    m = re.fullmatch(r"f(\d{1,2})", low)
    if m and 1 <= int(m.group(1)) <= 35:
        return value | (0x01000030 + int(m.group(1)) - 1)
    if len(key) == 1:
        return value | ord(key.upper())
    raise ValueError(f"unknown key {key!r}")


class KGlobalAccelHotkey:
    """One push-to-talk action registered directly with kglobalaccel.

    Gio.DBusConnection rather than dbus-python so the QKeySequence argument —
    `a(ai)`, a set of sequences each holding up to four key ints — is typed
    exactly; a loose binding marshals it as a plain array and the call fails.
    Signals arrive on whatever main context is thread-default at subscribe
    time, so this must be built on the GTK main loop.
    """

    SERVICE = "org.kde.kglobalaccel"
    ROOT_PATH = "/kglobalaccel"
    ROOT_IFACE = "org.kde.KGlobalAccel"
    COMPONENT_IFACE = "org.kde.kglobalaccel.Component"
    COMPONENT = APP_ID
    ACTION = "push-to-talk"

    # SetPresent (2) | NoAutoloading (4): the keys we pass win over whatever
    # kglobalshortcutsrc last cached for this action.
    SET_FLAGS = 6

    def __init__(self, binding: str, on_press, on_release):
        self._binding = binding
        self._on_press = on_press
        self._on_release = on_release
        self._bus = None
        self._action_id = [self.COMPONENT, self.ACTION, APP_NAME,
                           "Push-to-talk dictation"]
        self._subs = []
        self.registered = False

    def _call(self, path, iface, method, params, reply_type=None):
        return self._bus.call_sync(
            self.SERVICE, path, iface, method, params,
            GLib.VariantType.new(reply_type) if reply_type else None,
            Gio.DBusCallFlags.NONE, 5000, None)

    def _owner_of(self, key: int) -> str:
        """Unique name of the component already holding `key`, if any."""
        try:
            infos = self._call(self.ROOT_PATH, self.ROOT_IFACE,
                               "getGlobalShortcutsByKey",
                               GLib.Variant("(i)", (key,)),
                               "(a(ssssssaiai))").unpack()[0]
        except GLib.Error:
            return "unknown"
        for info in infos:
            if info[2] != self.COMPONENT:
                return info[2]
        return ""

    def register(self) -> str:
        """Register, bind the key and subscribe.  Returns "" when clean.

        A non-empty return names the component that already owns the key.
        kglobalaccel accepts the registration either way and the action shows
        up in System Settings, but only the first claimant is actually handed
        the key — verified here with injected key events — so a conflict has
        to be reported rather than assumed harmless.
        """
        key = qt_key_sequence(self._binding)
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        available = self._call(
            self.ROOT_PATH, self.ROOT_IFACE, "globalShortcutAvailable",
            GLib.Variant("((ai)s)", (([key],), self.COMPONENT)), "(b)").unpack()[0]
        owner = "" if available else self._owner_of(key)

        self._call(self.ROOT_PATH, self.ROOT_IFACE, "doRegister",
                   GLib.Variant("(as)", (self._action_id,)))
        self._call(self.ROOT_PATH, self.ROOT_IFACE, "setShortcutKeys",
                   GLib.Variant("(asa(ai)u)",
                                (self._action_id, [([key],)], self.SET_FLAGS)),
                   "(a(ai))")
        path = self._call(self.ROOT_PATH, self.ROOT_IFACE, "getComponent",
                          GLib.Variant("(s)", (self.COMPONENT,)), "(o)").unpack()[0]

        for signal, handler in (("globalShortcutPressed", self._handle_press),
                                ("globalShortcutReleased", self._handle_release)):
            self._subs.append(self._bus.signal_subscribe(
                self.SERVICE, self.COMPONENT_IFACE, signal, path, None,
                Gio.DBusSignalFlags.NONE, handler))
        self.registered = True
        DIAG.log("hold_hotkey_registered", key=hex(key), path=path,
                 conflict=owner or "none")
        return owner

    def shortcut_infos(self) -> list:
        """What System Settings will list for this component."""
        if not self._bus:
            return []
        path = self._call(self.ROOT_PATH, self.ROOT_IFACE, "getComponent",
                          GLib.Variant("(s)", (self.COMPONENT,)), "(o)").unpack()[0]
        return list(self._call(path, self.COMPONENT_IFACE, "allShortcutInfos",
                               None, "(a(ssssssaiai))").unpack()[0])

    def _handle_press(self, _conn, _sender, _path, _iface, _signal, params):
        if params.unpack()[1] == self.ACTION:
            self._on_press()

    def _handle_release(self, _conn, _sender, _path, _iface, _signal, params):
        if params.unpack()[1] == self.ACTION:
            self._on_release()

    def unregister(self):
        for sub in self._subs:
            try:
                self._bus.unsubscribe(sub)
            except Exception:
                pass
        self._subs = []
        if self.registered:
            try:
                self._call(self.ROOT_PATH, self.ROOT_IFACE, "unRegister",
                           GLib.Variant("(as)", (self._action_id,)))
            except GLib.Error:
                pass
            self.registered = False


# ---------------------------------------------------------------------------
# Hotkey manager
# ---------------------------------------------------------------------------

class HotkeyManager:
    def __init__(self, config: AppConfig, on_toggle, on_start, on_stop, on_pause,
                 on_hold_press=None, on_hold_release=None):
        self._config = config
        self._on_toggle = on_toggle
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_pause = on_pause
        self._on_hold_press = on_hold_press or (lambda: None)
        self._on_hold_release = on_hold_release or (lambda: None)
        self._listener = None
        self._hold = None

    @property
    def hold(self) -> "KGlobalAccelHotkey":
        return self._hold

    def start(self) -> str:
        """Bind the configured hotkeys.  Returns "" or a problem to show.

        Only hold mode goes through kglobalaccel; the other modes keep the
        pynput listener plus the SIGUSR1 path they have always used.
        """
        status = ""
        if self._config.hotkey_mode == "hold":
            status = self._start_hold()

        from pynput import keyboard
        bindings = {}
        if self._config.hotkey_mode == "toggle":
            bindings[self._config.hotkey_toggle] = lambda: GLib.idle_add(self._on_toggle)
        elif self._config.hotkey_mode == "start_stop":
            bindings[self._config.hotkey_start] = lambda: GLib.idle_add(self._on_start)
            bindings[self._config.hotkey_stop] = lambda: GLib.idle_add(self._on_stop)

        if self._config.hotkey_pause:
            bindings[self._config.hotkey_pause] = lambda: GLib.idle_add(self._on_pause)

        self._listener = keyboard.GlobalHotKeys(bindings)
        self._listener.daemon = True
        self._listener.start()
        return status

    def _start_hold(self) -> str:
        binding = self._config.hotkey_hold
        self._hold = KGlobalAccelHotkey(binding, self._on_hold_press,
                                        self._on_hold_release)
        try:
            owner = self._hold.register()
        except (GLib.Error, ValueError) as e:
            DIAG.log("hold_hotkey_failed", err=type(e).__name__)
            self._hold = None
            return f"Push-to-talk unavailable: {e}"
        if owner:
            DIAG.log("hold_hotkey_conflict", owner=owner)
            return (f"{binding} is already held by {owner} — "
                    f"free it in System Settings > Shortcuts")
        return ""

    def stop(self):
        if self._hold:
            self._hold.unregister()
            self._hold = None
        if self._listener:
            self._listener.stop()
            self._listener = None

    def rebuild(self, config: AppConfig) -> str:
        self._config = config
        self.stop()
        return self.start()


# ---------------------------------------------------------------------------
# Hotkey capture widget
# ---------------------------------------------------------------------------

class HotkeyCaptureButton(Gtk.Button):
    def __init__(self, current_binding: str):
        super().__init__(label=self._display(current_binding))
        self._binding = current_binding
        self._capturing = False
        self._key_handler = None
        self.connect("clicked", self._on_clicked)

    @property
    def binding(self) -> str:
        return self._binding

    @staticmethod
    def _display(binding: str) -> str:
        return binding.replace("<", "").replace(">", "").replace("+", " + ").title()

    def _on_clicked(self, _btn):
        if self._capturing:
            return
        self._capturing = True
        self.set_label("Press a key combo...")
        self._key_handler = self.get_toplevel().connect("key-press-event", self._on_key)

    def _on_key(self, _widget, event):
        if not self._capturing:
            return False
        self._capturing = False
        self.get_toplevel().disconnect(self._key_handler)

        parts = []
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            parts.append("<ctrl>")
        if event.state & Gdk.ModifierType.MOD1_MASK:
            parts.append("<alt>")
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            parts.append("<shift>")

        keyname = Gdk.keyval_name(event.keyval).lower()
        if keyname in ("control_l", "control_r", "alt_l", "alt_r",
                       "shift_l", "shift_r", "super_l", "super_r",
                       "meta_l", "meta_r"):
            self.set_label(self._display(self._binding))
            return True

        parts.append(keyname)
        self._binding = "+".join(parts)
        self.set_label(self._display(self._binding))
        return True


# ---------------------------------------------------------------------------
# Settings dialog (tabbed: Models, Hotkeys, About)
# ---------------------------------------------------------------------------

def _any_model_downloaded(profiles: dict) -> bool:
    """Check if at least one model is downloaded."""
    for mid in profiles:
        if _is_model_downloaded(mid, profiles):
            return True
    return False


def _is_model_downloaded(model_id: str, profiles: dict) -> bool:
    """Check if all files for a model are present on disk."""
    profile = profiles.get(model_id)
    if not profile:
        return False
    model_dir = MODELS_DIR / model_id
    for key, info in profile.get("files", {}).items():
        if not (model_dir / info["filename"]).exists():
            return False
    return True


def _download_file(url: str, dest: Path, on_progress_bytes=None):
    """Download a single file with progress reporting. Raises on failure."""
    import requests

    resp = requests.get(url, stream=True, timeout=(15, 60))
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress_bytes:
                    on_progress_bytes(downloaded, total)
        tmp.rename(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _ensure_vad(profiles_data: dict, on_progress_bytes=None):
    """Download the VAD model if not present."""
    vad = profiles_data.get("vad")
    if not vad:
        return
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODELS_DIR / vad["filename"]
    if dest.exists() and dest.stat().st_size > 0:
        return
    _download_file(vad["url"], dest, on_progress_bytes)


def _download_model(model_id: str, profiles_data: dict, on_progress, on_done):
    """Download a model in a background thread."""

    def _worker():
        try:
            profile = profiles_data["profiles"][model_id]
            model_dir = MODELS_DIR / model_id
            model_dir.mkdir(parents=True, exist_ok=True)

            # Download VAD first
            def _vad_progress(done, total):
                if total:
                    mb = done / 1024 / 1024
                    total_mb = total / 1024 / 1024
                    GLib.idle_add(on_progress, f"VAD: {mb:.0f}/{total_mb:.0f} MB", -1.0)
            _ensure_vad(profiles_data, _vad_progress)

            # Download model files
            files = profile["files"]
            total_files = len(files)
            for i, (key, info) in enumerate(files.items(), 1):
                dest = model_dir / info["filename"]
                if dest.exists() and dest.stat().st_size > 0:
                    continue

                def _file_progress(done, total, fname=info["filename"], idx=i):
                    if total:
                        frac = done / total
                        mb = done / 1024 / 1024
                        total_mb = total / 1024 / 1024
                        GLib.idle_add(
                            on_progress,
                            f"{fname} ({idx}/{total_files}): {mb:.0f}/{total_mb:.0f} MB",
                            frac,
                        )
                    else:
                        mb = done / 1024 / 1024
                        GLib.idle_add(on_progress, f"{fname}: {mb:.0f} MB", -1.0)

                _download_file(info["url"], dest, _file_progress)

            GLib.idle_add(on_done, True, "")
        except Exception as e:
            GLib.idle_add(on_done, False, str(e))

    threading.Thread(target=_worker, daemon=True).start()


def _download_all_models(profiles_data: dict, on_progress, on_done):
    """Download all models sequentially in a background thread."""

    def _worker():
        try:
            # Download VAD first
            def _vad_progress(done, total):
                if total:
                    mb = done / 1024 / 1024
                    total_mb = total / 1024 / 1024
                    GLib.idle_add(on_progress, f"VAD: {mb:.0f}/{total_mb:.0f} MB", -1.0)
            _ensure_vad(profiles_data, _vad_progress)

            profiles = profiles_data["profiles"]
            for mid, profile in profiles.items():
                model_dir = MODELS_DIR / mid
                model_dir.mkdir(parents=True, exist_ok=True)
                files = profile["files"]
                total_files = len(files)
                for i, (key, info) in enumerate(files.items(), 1):
                    dest = model_dir / info["filename"]
                    if dest.exists() and dest.stat().st_size > 0:
                        continue
                    short_name = profile["name"][:18]

                    def _file_progress(done, total, sn=short_name, fname=info["filename"], idx=i, tf=total_files):
                        if total:
                            frac = done / total
                            mb = done / 1024 / 1024
                            total_mb = total / 1024 / 1024
                            GLib.idle_add(on_progress, f"{sn}: {fname} {mb:.0f}/{total_mb:.0f} MB", frac)
                        else:
                            mb = done / 1024 / 1024
                            GLib.idle_add(on_progress, f"{sn}: {fname} {mb:.0f} MB", -1.0)

                    _download_file(info["url"], dest, _file_progress)
            GLib.idle_add(on_done, True, "")
        except Exception as e:
            GLib.idle_add(on_done, False, str(e))

    threading.Thread(target=_worker, daemon=True).start()


LANG_LABELS = {"en": "English", "es": "Spanish", "de": "German", "fr": "French"}


class WelcomeDialog(Gtk.Dialog):
    """First-run dialog — downloads all models automatically."""

    def __init__(self, profiles_data: dict, config: AppConfig, on_model_ready):
        super().__init__(title=f"Welcome to {APP_NAME}", flags=0)
        self._profiles_data = profiles_data
        self._profiles = profiles_data["profiles"]
        self._config = config
        self._on_model_ready = on_model_ready
        self.set_default_size(440, 280)
        self.set_deletable(False)

        box = self.get_content_area()
        box.set_spacing(12)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)

        header = Gtk.Label()
        header.set_markup(
            f"<span size='x-large' weight='bold'>Welcome to {APP_NAME}</span>"
        )
        header.set_halign(Gtk.Align.START)
        box.pack_start(header, False, False, 0)

        total_mb = sum(m["size_mb"] for m in self._profiles.values())
        subtitle = Gtk.Label()
        subtitle.set_markup(
            f"Three speech recognition models will be downloaded\n"
            f"so you can switch between them freely.\n\n"
            f"Total download: <b>~{total_mb} MB</b>"
        )
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_line_wrap(True)
        box.pack_start(subtitle, False, False, 0)

        # Model summary (read-only)
        for mid, mdata in self._profiles.items():
            lbl = Gtk.Label()
            tag = "Streaming" if mdata.get("streaming") else "VAD-segmented"
            lbl.set_markup(
                f"  \u2022 <b>{mdata['name']}</b>  ({mdata['size_mb']} MB, {tag})"
            )
            lbl.set_halign(Gtk.Align.START)
            lbl.get_style_context().add_class("dim-label")
            box.pack_start(lbl, False, False, 0)

        # Download button
        self._dl_btn = Gtk.Button()
        dl_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dl_hbox.set_halign(Gtk.Align.CENTER)
        dl_hbox.pack_start(
            Gtk.Image.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON),
            False, False, 0,
        )
        dl_hbox.pack_start(Gtk.Label(label="Download All Models"), False, False, 0)
        self._dl_btn.add(dl_hbox)
        self._dl_btn.get_style_context().add_class("suggested-action")
        self._dl_btn.set_margin_top(8)
        self._dl_btn.connect("clicked", self._on_download_all)
        box.pack_start(self._dl_btn, False, False, 0)

        # Progress bar (hidden until download starts)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_no_show_all(True)
        box.pack_start(self._progress_bar, False, False, 0)

        # Error label (hidden until error)
        self._error_label = Gtk.Label()
        self._error_label.set_line_wrap(True)
        self._error_label.set_max_width_chars(60)
        self._error_label.set_halign(Gtk.Align.START)
        self._error_label.set_no_show_all(True)
        box.pack_start(self._error_label, False, False, 0)

        self.show_all()

    def _update_progress(self, msg, fraction):
        self._progress_bar.set_text(msg)
        if fraction >= 0:
            self._progress_bar.set_fraction(min(fraction, 1.0))
        else:
            self._progress_bar.pulse()

    def _on_download_all(self, btn):
        btn.set_sensitive(False)
        self._progress_bar.show()
        self._error_label.hide()

        def on_progress(msg, fraction):
            self._update_progress(msg, fraction)

        def on_done(success, err):
            if success:
                self._config.model_profile = "desktop"
                self._config.save()
                self.destroy()
                if self._on_model_ready:
                    self._on_model_ready(self._config)
            else:
                btn.set_sensitive(True)
                self._progress_bar.hide()
                self._error_label.set_markup(f"<span color='red'>Download failed: {GLib.markup_escape_text(err)}</span>")
                self._error_label.show()
                print(f"Download error: {err}", file=sys.stderr)

        _download_all_models(self._profiles_data, on_progress, on_done)


class SettingsDialog(Gtk.Dialog):
    def __init__(self, config: AppConfig, profiles_data: dict, on_save):
        super().__init__(title=f"{APP_NAME} — Settings", flags=0)
        self._config = config
        self._profiles_data = profiles_data
        self._profiles = profiles_data["profiles"]
        self._on_save = on_save
        self.set_default_size(520, 560)

        notebook = Gtk.Notebook()
        self.get_content_area().pack_start(notebook, True, True, 0)

        notebook.append_page(self._build_models_tab(), Gtk.Label(label="Models"))
        notebook.append_page(self._build_hotkeys_tab(), Gtk.Label(label="Hotkeys"))
        notebook.append_page(self._build_general_tab(), Gtk.Label(label="General"))
        notebook.append_page(self._build_about_tab(), Gtk.Label(label="About"))

        self.show_all()

    # --- Models tab ---

    def _build_models_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        self._model_status_label = Gtk.Label()
        self._model_status_label.set_halign(Gtk.Align.START)
        box.pack_start(self._model_status_label, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._model_list = Gtk.ListBox()
        self._model_list.set_selection_mode(Gtk.SelectionMode.NONE)
        sw.add(self._model_list)
        box.pack_start(sw, True, True, 0)

        # Download All button
        self._dl_all_btn = Gtk.Button()
        dl_all_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dl_all_hbox.set_halign(Gtk.Align.CENTER)
        dl_all_hbox.pack_start(
            Gtk.Image.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON),
            False, False, 0,
        )
        total_mb = sum(m["size_mb"] for m in self._profiles.values())
        self._dl_all_label = Gtk.Label(label=f"Download All Models ({total_mb} MB)")
        dl_all_hbox.pack_start(self._dl_all_label, False, False, 0)
        self._dl_all_btn.add(dl_all_hbox)
        self._dl_all_btn.connect("clicked", self._on_download_all)
        box.pack_start(self._dl_all_btn, False, False, 0)

        # Progress bar (hidden until download starts)
        self._dl_progress = Gtk.ProgressBar()
        self._dl_progress.set_show_text(True)
        self._dl_progress.set_no_show_all(True)
        box.pack_start(self._dl_progress, False, False, 0)

        # Error label (hidden until error)
        self._dl_error = Gtk.Label()
        self._dl_error.set_line_wrap(True)
        self._dl_error.set_max_width_chars(60)
        self._dl_error.set_halign(Gtk.Align.START)
        self._dl_error.set_no_show_all(True)
        box.pack_start(self._dl_error, False, False, 0)

        self._populate_models()
        return box

    def _populate_models(self):
        for child in self._model_list.get_children():
            self._model_list.remove(child)

        active = self._config.model_profile
        self._model_status_label.set_markup(
            f"Active: <b>{self._profiles.get(active, {}).get('name', active)}</b>"
        )

        for mid, mdata in self._profiles.items():
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)

            # Info column
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name_label = Gtk.Label()
            name_label.set_markup(f"<b>{mdata['name']}</b>")
            name_label.set_halign(Gtk.Align.START)
            vbox.pack_start(name_label, False, False, 0)

            desc = mdata.get("description", "")
            desc_label = Gtk.Label(label=desc)
            desc_label.set_halign(Gtk.Align.START)
            desc_label.set_line_wrap(True)
            desc_label.set_max_width_chars(50)
            desc_label.get_style_context().add_class("dim-label")
            vbox.pack_start(desc_label, False, False, 0)

            rec = mdata.get("recommended_for", "")
            hw = mdata.get("hardware_label", "CPU")
            langs = mdata.get("languages")
            tag_parts = [f"{mdata['params']} params", f"{mdata['size_mb']} MB", hw]
            if rec:
                tag_parts.append(rec)
            if langs:
                tag_parts.append("/".join(l.upper() for l in langs))
            if mdata.get("streaming"):
                tag_parts.append("Streaming")
            tag_label = Gtk.Label()
            tag_label.set_markup(f"<small>{' · '.join(tag_parts)}</small>")
            tag_label.set_halign(Gtk.Align.START)
            tag_label.get_style_context().add_class("dim-label")
            vbox.pack_start(tag_label, False, False, 0)

            hbox.pack_start(vbox, True, True, 0)

            # Buttons column
            btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            btn_box.set_valign(Gtk.Align.CENTER)

            downloaded = _is_model_downloaded(mid, self._profiles)
            is_active = (mid == active)

            if downloaded:
                if is_active:
                    active_label = Gtk.Label(label="Active")
                    active_label.get_style_context().add_class("dim-label")
                    btn_box.pack_start(active_label, False, False, 0)
                else:
                    use_btn = Gtk.Button(label="Use")
                    use_btn.connect("clicked", self._on_use_model, mid)
                    btn_box.pack_start(use_btn, False, False, 0)
            else:
                dl_btn = Gtk.Button()
                dl_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                dl_hbox.pack_start(
                    Gtk.Image.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON),
                    False, False, 0,
                )
                dl_hbox.pack_start(Gtk.Label(label="Download"), False, False, 0)
                dl_btn.add(dl_hbox)
                dl_btn.connect("clicked", self._on_download_model, mid, dl_btn)
                btn_box.pack_start(dl_btn, False, False, 0)

            hbox.pack_end(btn_box, False, False, 0)
            row.add(hbox)
            self._model_list.add(row)

        self._model_list.show_all()

    def _on_use_model(self, _btn, model_id):
        self._config.model_profile = model_id
        self._config.save()
        if self._on_save:
            self._on_save(self._config)
        self._populate_models()

    def _update_dl_progress(self, msg, fraction):
        self._dl_progress.set_text(msg)
        if fraction >= 0:
            self._dl_progress.set_fraction(min(fraction, 1.0))
        else:
            self._dl_progress.pulse()

    def _on_download_model(self, _btn, model_id, btn_widget):
        btn_widget.set_sensitive(False)
        self._dl_progress.show()
        self._dl_error.hide()

        def on_progress(msg, fraction):
            self._update_dl_progress(msg, fraction)

        def on_done(success, err):
            self._dl_progress.hide()
            if success:
                self._populate_models()
            else:
                btn_widget.set_sensitive(True)
                self._dl_error.set_markup(f"<span color='red'>Download failed: {GLib.markup_escape_text(err)}</span>")
                self._dl_error.show()
                print(f"Download error: {err}", file=sys.stderr)

        _download_model(model_id, self._profiles_data, on_progress, on_done)

    def _on_download_all(self, _btn):
        self._dl_all_btn.set_sensitive(False)
        self._dl_progress.show()
        self._dl_error.hide()

        def on_progress(msg, fraction):
            self._update_dl_progress(msg, fraction)

        def on_done(success, err):
            self._dl_progress.hide()
            if success:
                self._dl_all_label.set_text("All models downloaded")
                self._populate_models()
            else:
                self._dl_all_btn.set_sensitive(True)
                self._dl_error.set_markup(f"<span color='red'>Download failed: {GLib.markup_escape_text(err)}</span>")
                self._dl_error.show()
                print(f"Download error: {err}", file=sys.stderr)

        _download_all_models(self._profiles_data, on_progress, on_done)

    # --- Hotkeys tab ---

    def _build_hotkeys_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        # Mode
        self._mode_toggle = Gtk.RadioButton.new_with_label(
            None, "Toggle (one key starts and stops)")
        self._mode_startstop = Gtk.RadioButton.new_with_label_from_widget(
            self._mode_toggle, "Start/Stop (separate keys)")
        self._mode_hold = Gtk.RadioButton.new_with_label_from_widget(
            self._mode_toggle, "Push-to-talk (dictate only while the key is held)")
        if self._config.hotkey_mode == "start_stop":
            self._mode_startstop.set_active(True)
        elif self._config.hotkey_mode == "hold":
            self._mode_hold.set_active(True)
        box.pack_start(self._mode_toggle, False, False, 0)
        box.pack_start(self._mode_startstop, False, False, 4)
        box.pack_start(self._mode_hold, False, False, 4)

        hold_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hold_box.set_margin_start(24)
        hold_box.pack_start(Gtk.Label(label="Hold key:", halign=Gtk.Align.END),
                            False, False, 0)
        self._hold_entry = Gtk.Entry()
        self._hold_entry.set_text(self._config.hotkey_hold)
        self._hold_entry.set_width_chars(16)
        self._hold_entry.set_tooltip_text(
            "KDE shortcut syntax, e.g. Meta+Alt+D.  Push-to-talk registers "
            "this with kglobalaccel itself, which is the only route that "
            "reports the key being released.")
        hold_box.pack_start(self._hold_entry, False, False, 0)
        self._hold_hint = Gtk.Label()
        self._hold_hint.get_style_context().add_class("dim-label")
        hold_box.pack_start(self._hold_hint, False, False, 0)
        box.pack_start(hold_box, False, False, 0)

        # Bindings
        hint = Gtk.Label(label="Click a button, then press your desired key combo.")
        hint.set_halign(Gtk.Align.START)
        hint.set_margin_top(8)
        box.pack_start(hint, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_margin_top(4)

        grid.attach(Gtk.Label(label="Toggle:", halign=Gtk.Align.END), 0, 0, 1, 1)
        self._hk_toggle = HotkeyCaptureButton(self._config.hotkey_toggle)
        grid.attach(self._hk_toggle, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Start:", halign=Gtk.Align.END), 0, 1, 1, 1)
        self._hk_start = HotkeyCaptureButton(self._config.hotkey_start)
        grid.attach(self._hk_start, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Stop:", halign=Gtk.Align.END), 0, 2, 1, 1)
        self._hk_stop = HotkeyCaptureButton(self._config.hotkey_stop)
        grid.attach(self._hk_stop, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Pause:", halign=Gtk.Align.END), 0, 3, 1, 1)
        self._hk_pause = HotkeyCaptureButton(self._config.hotkey_pause)
        grid.attach(self._hk_pause, 1, 3, 1, 1)

        box.pack_start(grid, False, False, 0)

        # Save
        save_btn = Gtk.Button(label="Save Hotkeys")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", self._save_hotkeys)
        save_btn.set_margin_top(12)
        box.pack_start(save_btn, False, False, 0)

        return box

    def _save_hotkeys(self, _btn):
        if self._mode_hold.get_active():
            self._config.hotkey_mode = "hold"
        elif self._mode_startstop.get_active():
            self._config.hotkey_mode = "start_stop"
        else:
            self._config.hotkey_mode = "toggle"
        binding = self._hold_entry.get_text().strip()
        try:
            qt_key_sequence(binding)
            self._config.hotkey_hold = binding
            self._hold_hint.set_markup("")
        except ValueError as e:
            # Keep the last working binding rather than registering nothing.
            self._hold_hint.set_markup(
                f"<small>{GLib.markup_escape_text(str(e))}</small>")
            self._hold_entry.set_text(self._config.hotkey_hold)
        self._config.hotkey_toggle = self._hk_toggle.binding
        self._config.hotkey_start = self._hk_start.binding
        self._config.hotkey_stop = self._hk_stop.binding
        self._config.hotkey_pause = self._hk_pause.binding
        self._config.save()
        if self._on_save:
            self._on_save(self._config)

    # --- General tab ---

    def _build_general_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        # Microphone selector
        hbox_mic = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_mic.pack_start(Gtk.Label(label="Microphone:"), False, False, 0)
        self._mic_combo = Gtk.ComboBoxText()
        self._mic_combo.append("", "System Default")
        devices = list_input_devices()
        for dev in devices:
            self._mic_combo.append(str(dev["index"]), dev["name"])
        self._mic_combo.set_active_id(self._config.audio_device or "")
        hbox_mic.pack_start(self._mic_combo, True, True, 0)
        box.pack_start(hbox_mic, False, False, 0)

        # Typing method
        hbox_typer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_typer.pack_start(Gtk.Label(label="Typing method:"), False, False, 0)
        self._typer_combo = Gtk.ComboBoxText()
        self._typer_combo.append("clipboard", "Clipboard paste (recommended)")
        self._typer_combo.append("wtype", "wtype (GNOME/Sway only)")
        self._typer_combo.append("ydotool", "ydotool (needs daemon+uinput)")
        self._typer_combo.set_active_id(self._config.typer)
        hbox_typer.pack_start(self._typer_combo, False, False, 0)
        box.pack_start(hbox_typer, False, False, 0)

        # When the text lands
        hbox_ins = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_ins.pack_start(Gtk.Label(label="Insert text:"), False, False, 0)
        self._insert_mode_combo = Gtk.ComboBoxText()
        self._insert_mode_combo.append("per_segment", "As each phrase decodes")
        self._insert_mode_combo.append("end_of_take", "Once, when I let the key go")
        self._insert_mode_combo.set_active_id(
            self._config.insert_mode
            if self._config.insert_mode in ("per_segment", "end_of_take")
            else "per_segment")
        self._insert_mode_combo.set_tooltip_text(
            "Per phrase keeps the text flowing while you hold the key.  Once "
            "per take gives one paste and one undo step for the whole take, "
            "but you see nothing until you let go.")
        hbox_ins.pack_start(self._insert_mode_combo, False, False, 0)
        box.pack_start(hbox_ins, False, False, 0)

        sep0 = Gtk.Separator()
        sep0.set_margin_top(4)
        box.pack_start(sep0, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.pack_start(Gtk.Label(label="Beep volume:"), False, False, 0)
        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self._vol_scale.set_value(self._config.beep_volume)
        hbox.pack_start(self._vol_scale, True, True, 0)
        box.pack_start(hbox, False, False, 0)

        hbox2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox2.pack_start(Gtk.Label(label="CPU threads:"), False, False, 0)
        self._threads_spin = Gtk.SpinButton.new_with_range(1, 16, 1)
        self._threads_spin.set_value(self._config.num_threads)
        hbox2.pack_start(self._threads_spin, False, False, 0)
        box.pack_start(hbox2, False, False, 0)

        hbox_vad = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_vad.pack_start(Gtk.Label(label="Pause that ends a phrase:"),
                            False, False, 0)
        self._vad_silence_spin = Gtk.SpinButton.new_with_range(0.2, 3.0, 0.1)
        self._vad_silence_spin.set_digits(1)
        self._vad_silence_spin.set_value(self._config.vad_min_silence)
        self._vad_silence_spin.set_tooltip_text(
            "Seconds of silence before the phrase is treated as finished and "
            "sent to the model.  Raise it if pausing to think splits your "
            "sentences; lower it only if you want shorter phrases.")
        hbox_vad.pack_start(self._vad_silence_spin, False, False, 0)
        hbox_vad.pack_start(Gtk.Label(label="seconds"), False, False, 0)
        box.pack_start(hbox_vad, False, False, 0)

        # Language (applies to Canary model)
        hbox_lang = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_lang.pack_start(Gtk.Label(label="Language:"), False, False, 0)
        self._lang_combo = Gtk.ComboBoxText()
        for code, label in LANG_LABELS.items():
            self._lang_combo.append(code, label)
        self._lang_combo.set_active_id(self._config.language)
        hbox_lang.pack_start(self._lang_combo, False, False, 0)
        lang_hint = Gtk.Label()
        lang_hint.set_markup("<small>Used by Canary model. Parakeet auto-detects.</small>")
        lang_hint.get_style_context().add_class("dim-label")
        hbox_lang.pack_start(lang_hint, False, False, 0)
        box.pack_start(hbox_lang, False, False, 0)

        # Streaming options
        sep_stream = Gtk.Separator()
        sep_stream.set_margin_top(4)
        box.pack_start(sep_stream, False, False, 0)

        self._partial_overwrite_check = Gtk.CheckButton(
            label="Streaming partial-overwrite (type text as you speak)")
        self._partial_overwrite_check.set_active(self._config.partial_overwrite)
        self._partial_overwrite_check.set_tooltip_text(
            "When enabled, streaming models type partial results into the active window "
            "and revise them in place.  When disabled, text only appears on final endpoint.")
        box.pack_start(self._partial_overwrite_check, False, False, 4)

        self._filter_fillers_check = Gtk.CheckButton(
            label="Filter filler words (um, uh, ehm …)")
        self._filter_fillers_check.set_active(self._config.filter_fillers)
        box.pack_start(self._filter_fillers_check, False, False, 4)

        # Overlay
        sep_ov = Gtk.Separator()
        sep_ov.set_margin_top(8)
        box.pack_start(sep_ov, False, False, 0)

        self._overlay_check = Gtk.CheckButton(
            label="Show pill overlay while dictating")
        self._overlay_check.set_active(self._config.overlay)
        self._overlay_check.set_tooltip_text(
            "A small capsule with a live microphone level meter, so you can "
            "see the microphone is actually picking you up.")
        box.pack_start(self._overlay_check, False, False, 4)

        self._preview_check = Gtk.CheckButton(
            label="Show the decoded text above the pill while I hold the key")
        self._preview_check.set_active(self._config.preview)
        self._preview_check.set_tooltip_text(
            "A floating, read-only panel with the phrases already recognised in "
            "this take.  It is drawn on screen, never typed: inserting text "
            "while the push-to-talk key is held would end the take.")
        self._preview_check.set_margin_start(20)
        box.pack_start(self._preview_check, False, False, 4)

        hbox_ov = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox_ov.pack_start(Gtk.Label(label="Overlay position:"), False, False, 0)
        self._overlay_pos_combo = Gtk.ComboBoxText()
        self._overlay_pos_combo.append("bottom", "Bottom centre")
        self._overlay_pos_combo.append("top", "Top centre")
        self._overlay_pos_combo.set_active_id(self._config.overlay_position)
        hbox_ov.pack_start(self._overlay_pos_combo, False, False, 0)
        box.pack_start(hbox_ov, False, False, 0)

        # Night mode
        sep = Gtk.Separator()
        sep.set_margin_top(8)
        box.pack_start(sep, False, False, 0)

        self._night_check = Gtk.CheckButton(label="Night mode (suppress beeps)")
        self._night_check.set_active(self._config.night_mode)
        box.pack_start(self._night_check, False, False, 4)

        hbox3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox3.pack_start(Gtk.Label(label="Quiet hours:"), False, False, 0)
        self._night_start_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._night_start_spin.set_value(self._config.night_start)
        hbox3.pack_start(self._night_start_spin, False, False, 0)
        hbox3.pack_start(Gtk.Label(label="to"), False, False, 0)
        self._night_end_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._night_end_spin.set_value(self._config.night_end)
        hbox3.pack_start(self._night_end_spin, False, False, 0)
        box.pack_start(hbox3, False, False, 0)

        save_btn = Gtk.Button(label="Save General")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", self._save_general)
        save_btn.set_margin_top(12)
        box.pack_start(save_btn, False, False, 0)

        return box

    def _save_general(self, _btn):
        global _active_config
        self._config.audio_device = self._mic_combo.get_active_id() or ""
        self._config.typer = self._typer_combo.get_active_id() or "wtype"
        self._config.insert_mode = (self._insert_mode_combo.get_active_id()
                                    or "per_segment")
        self._config.beep_volume = self._vol_scale.get_value()
        self._config.num_threads = int(self._threads_spin.get_value())
        self._config.vad_min_silence = round(self._vad_silence_spin.get_value(), 2)
        self._config.language = self._lang_combo.get_active_id() or "en"
        self._config.partial_overwrite = self._partial_overwrite_check.get_active()
        self._config.filter_fillers = self._filter_fillers_check.get_active()
        self._config.overlay = self._overlay_check.get_active()
        self._config.preview = self._preview_check.get_active()
        self._config.overlay_position = self._overlay_pos_combo.get_active_id() or "bottom"
        self._config.night_mode = self._night_check.get_active()
        self._config.night_start = int(self._night_start_spin.get_value())
        self._config.night_end = int(self._night_end_spin.get_value())
        _active_config = self._config
        self._config.save()
        if self._on_save:
            self._on_save(self._config)

    # --- About tab ---

    def _build_about_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        about_text = Gtk.Label()
        about_text.set_markup(
            f"<b>{APP_NAME}</b>\n\n"
            "On-device voice typing with punctuation.\n"
            "Powered by sherpa-onnx + NVIDIA NeMo models.\n\n"
            "<b>Model recommendations:</b>\n\n"
            "<b>Parakeet TDT 0.6B v3</b> (639 MB)\n"
            "Best overall accuracy. Ideal for desktops and workstations.\n"
            "Works on CPU at ~30x real-time. Even faster with GPU.\n"
            "Supports 25 European languages.\n\n"
            "<b>Canary 180M Flash</b> (198 MB)\n"
            "Lightweight model for laptops and low-RAM machines.\n"
            "Good accuracy for its size. Supports EN/ES/DE/FR.\n"
            "Only 198 MB download — ideal for travel.\n\n"
            "<b>Nemotron Streaming 0.6B</b> (631 MB)\n"
            "True real-time streaming — text appears as you speak\n"
            "with no pause needed. English only.\n"
            "Higher latency tradeoff: slightly less accurate on\n"
            "sentence boundaries vs. VAD-segmented models.\n\n"
            "<b>General tips:</b>\n"
            "• All models include punctuation and capitalization\n"
            "• Non-streaming models wait for a brief pause, then transcribe\n"
            "• Streaming model transcribes continuously but may revise text\n"
            "• More CPU threads = faster transcription (4-8 recommended)\n"
            "• Pause hotkey mutes mic without unloading model (fast resume)"
        )
        about_text.set_halign(Gtk.Align.START)
        about_text.set_valign(Gtk.Align.START)
        about_text.set_line_wrap(True)
        about_text.set_selectable(True)
        about_text.set_max_width_chars(60)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(about_text)
        box.pack_start(sw, True, True, 0)

        return box


# ---------------------------------------------------------------------------
# Pill overlay — a capsule that says what dictation is doing, right now
#
# gtk-layer-shell puts it on the OVERLAY layer with an exclusive zone of -1, so
# panels keep their geometry, and with an EMPTY input region, so every click
# lands in the window underneath.  Shape carries meaning: every normal state is
# the same capsule, and only an error is a wider rounded rectangle — the shape
# says "something is wrong" before the message has been read.
# ---------------------------------------------------------------------------

def overlay_error_message(msg: str) -> str:
    """Short and actionable for the pill; the full text still goes to stderr."""
    low = msg.lower()
    if "missing model" in low or "download_models" in low:
        return "Model files missing — open Settings › Models"
    if "portaudio" in low or "device" in low or "sounddevice" in low:
        return "Microphone unavailable — check Settings › General"
    first = msg.strip().splitlines()[0] if msg.strip() else ""
    return (first[:58] + "…") if len(first) > 58 else (first or "Dictation failed")


class TranscriptPreview:
    """Floating, display-only tail of the segments this take has already decoded.

    Why it exists: with `insert_mode = "end_of_take"` nothing reaches the
    document until the key is released, so a long take shows the user nothing at
    all.  Inserting the text early instead is not an option — injecting ANY
    keystroke while the push-to-talk key is held (Ctrl alone, V alone, Ctrl+V
    and Shift+Insert were each tried) makes kglobalaccel report the hotkey as
    RELEASED, which ends the take; a control hold with no injection survives.
    So the decoded text is *drawn* on its own layer-shell surface instead.

    HARD INVARIANT — this panel never inserts.  It holds no TextTyper, never
    touches the clipboard and never runs ydotool/wtype: the only things it does
    with text are measure it and paint it.  testing/test_hold_take.py asserts
    that behaviourally (zero insertions while the key is down) and structurally
    (no insertion machinery reachable from this class).

    Append-only for COMMITTED text: what `append()` is fed are final decoder
    results, so a committed word is never re-rendered differently — new
    committed text only ever arrives at the end.  `set_hypothesis()` adds one
    trailing, explicitly provisional fragment for the phrase still being
    spoken, which is the only thing here that may be replaced; it is stored
    apart from the committed text and is never part of `text`.
    """

    MAX_WIDTH = 600            # readable line length, well wider than the pill
    MIN_WIDTH = 240
    SCREEN_MARGIN = 40         # keep the panel off the screen edge

    ROWS_FULL = 2              # lines shown at full legibility
    ROWS_FADE = 1              # one more, faded out, so "older" reads as older
    ROWS_MAX = ROWS_FULL + ROWS_FADE

    FONT_SIZE = 12.5
    LINE_H = 18
    PAD_X = 14
    PAD_TOP = 9
    PAD_BOTTOM = 11
    BASELINE_DROP = 4          # baseline sits this far above the row's bottom
    RADIUS = 16                # the pill's soft capsule, in a shape that holds
                               # several lines instead of one

    # Only the tail is ever laid out: a take can run for minutes, and no amount
    # of earlier text changes which three lines end up on screen.
    TAIL_CHARS = 1200

    def __init__(self, config: AppConfig, clearance: int):
        self._config = config
        self._clearance = clearance   # distance from the screen edge, past the pill
        self._text = ""
        self._hypothesis = ""         # provisional tail, never part of _text
        self._segments = 0
        self._rows: list = []
        self._width = 0
        self._measure = None
        self._shell = None
        self.layered = False
        self._win = None

    # --- text model (works with no window, so it is testable headless) -----

    @property
    def text(self) -> str:
        """The committed text only — what the take will actually insert."""
        return self._text

    @property
    def hypothesis(self) -> str:
        return self._hypothesis

    @property
    def display_text(self) -> str:
        """Committed text plus the provisional tail — what is painted."""
        if not self._hypothesis:
            return self._text
        return f"{self._text} {self._hypothesis}".strip()

    @property
    def segments(self) -> int:
        return self._segments

    @property
    def chars(self) -> int:
        return len(self._text)

    @property
    def rows(self) -> list:
        return list(self._rows)

    def append(self, text: str) -> bool:
        """Add one decoded segment.  Returns whether there is anything to show.

        Logged once per segment — the panel repaints at the pill's frame rate,
        and a log line per redraw would say nothing a log line per segment does
        not.
        """
        text = _WS_RE.sub(" ", text or "").strip()
        if not text:
            return bool(self._rows)
        # The block this hypothesis was guessing at has now been decoded for
        # real, so the guess goes with it.
        self._hypothesis = ""
        self._text = f"{self._text} {text}".strip() if self._text else text
        self._segments += 1
        self._reflow()
        DIAG.log("preview_update", chars=len(self._text), segments=self._segments)
        return bool(self._rows)

    def set_hypothesis(self, text: str) -> bool:
        """Replace the provisional tail.  Returns whether there is anything to show.

        Committed text is untouched: only this fragment changes between ticks,
        so no word the decoder has actually returned can flicker or be
        rewritten.  Logged once per change, not per redraw.
        """
        text = _WS_RE.sub(" ", text or "").strip()
        if text == self._hypothesis:
            return bool(self._rows)
        self._hypothesis = text
        self._reflow()
        DIAG.log("preview_hypothesis", chars=len(text),
                 committed=len(self._text), segments=self._segments)
        return bool(self._rows)

    def reset(self):
        self._text = ""
        self._hypothesis = ""
        self._segments = 0
        self._rows = []

    def _measure_cr(self):
        if self._measure is None:
            # 1x1 scratch surface: text extents need a context, not a window.
            self._measure = cairo.Context(
                cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
        self._apply_font(self._measure)
        return self._measure

    def _apply_font(self, cr):
        cr.select_font_face("Sans", cairo.FontSlant.NORMAL,
                            cairo.FontWeight.NORMAL)
        cr.set_font_size(self.FONT_SIZE)

    def panel_width(self) -> int:
        """Wider than the pill, but never wider than the screen allows."""
        if self._width:
            return self._width
        width = self.MAX_WIDTH
        try:
            display = Gdk.Display.get_default()
            monitor = (display.get_primary_monitor() or display.get_monitor(0)
                       if display is not None else None)
            if monitor is not None:
                width = min(width,
                            monitor.get_geometry().width - 2 * self.SCREEN_MARGIN)
        except Exception:
            pass
        self._width = max(self.MIN_WIDTH, int(width))
        return self._width

    def _text_width(self) -> int:
        return self.panel_width() - 2 * self.PAD_X

    def wrap(self, text: str, max_width: float) -> list:
        """Greedy wrap on word boundaries.

        A word is never split.  One longer than a whole line is left intact on a
        line of its own and clipped by the panel edge instead: a clipped glyph
        reads as "there is more", a chopped word reads as a transcription error.
        """
        cr = self._measure_cr()
        lines, current = [], ""
        for word in text.split():
            trial = f"{current} {word}" if current else word
            if current and cr.text_extents(trial)[4] > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return lines

    def _reflow(self):
        text = self.display_text
        if len(text) > self.TAIL_CHARS:
            text = text[-self.TAIL_CHARS:]
            cut = text.find(" ")
            # Never begin the laid-out tail mid-word; those lines are dropped
            # anyway, but a half word must not be able to reach a visible row.
            text = text[cut + 1:] if cut >= 0 else text
        self._rows = self.wrap(text, self._text_width())[-self.ROWS_MAX:]

    def panel_height(self) -> int:
        return (self.PAD_TOP + max(1, len(self._rows)) * self.LINE_H
                + self.PAD_BOTTOM)

    # --- window (built on first show: a disabled preview costs no surface) --

    def _build(self):
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title(f"{APP_NAME} transcript preview")
        win.set_app_paintable(True)
        win.set_decorated(False)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        visual = win.get_screen().get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)
        try:
            gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import GtkLayerShell
            if not GtkLayerShell.is_supported():
                raise RuntimeError("compositor has no wlr-layer-shell")
            GtkLayerShell.init_for_window(win)
            GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
            GtkLayerShell.set_exclusive_zone(win, -1)   # panels must not move
            self._shell = GtkLayerShell
            self.layered = True
            DIAG.log("preview_backend", backend="layer_shell")
        except Exception as e:
            win.set_keep_above(True)
            win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
            DIAG.log("preview_backend", backend="fallback_window",
                     err=type(e).__name__)
        win.connect("draw", self._on_draw)
        win.connect("realize", lambda _w: self._set_click_through())
        self._win = win
        self._apply_anchor()

    def _apply_anchor(self):
        """Sit past the pill, on the same screen edge.  The pill is a separate
        surface and is neither moved nor resized by any of this."""
        if not self.layered or self._win is None:
            return
        top = self._config.overlay_position == "top"
        shell, win = self._shell, self._win
        shell.set_anchor(win, shell.Edge.TOP, top)
        shell.set_anchor(win, shell.Edge.BOTTOM, not top)
        shell.set_anchor(win, shell.Edge.LEFT, False)
        shell.set_anchor(win, shell.Edge.RIGHT, False)
        shell.set_margin(win, shell.Edge.TOP, self._clearance)
        shell.set_margin(win, shell.Edge.BOTTOM, self._clearance)

    def _place_fallback(self, width, height):
        if self.layered or self._win is None:
            return
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        area = monitor.get_workarea()
        x = area.x + (area.width - width) // 2
        if self._config.overlay_position == "top":
            y = area.y + self._clearance
        else:
            y = area.y + area.height - self._clearance - height
        self._win.move(x, y)

    def _set_click_through(self):
        """Empty input region — the panel is scenery, every click falls through."""
        if self._win is None:
            return
        gdk_win = self._win.get_window()
        if gdk_win is not None:
            gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)

    def apply_config(self, config: AppConfig, clearance: int = None):
        self._config = config
        if clearance is not None:
            self._clearance = clearance
        self._width = 0
        self._apply_anchor()
        if self._rows:
            self._reflow()

    def show(self):
        if self._win is None:
            self._build()
        width, height = self.panel_width(), self.panel_height()
        self._win.set_size_request(width, height)
        self._win.resize(width, height)
        if not self._win.get_visible():
            self._win.show_all()
        self._place_fallback(width, height)
        self._set_click_through()
        self._win.queue_draw()

    def hide(self):
        if self._win is not None:
            self._win.hide()

    def visible(self) -> bool:
        return self._win is not None and self._win.get_visible()

    def shutdown(self):
        self.reset()
        if self._win is not None:
            self._win.destroy()
            self._win = None

    # --- drawing ----------------------------------------------------------

    def _on_draw(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()

        cr.set_operator(cairo.Operator.SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.Operator.OVER)

        # Same dark translucent fill and hairline as the pill.
        PillOverlay._rounded_rect(cr, 0.5, 0.5, width - 1, height - 1, self.RADIUS)
        cr.set_source_rgba(0, 0, 0, 0.9)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.1)
        cr.set_line_width(1)
        cr.stroke()

        rows = self._rows
        if not rows:
            return False

        cr.save()
        # Clip to the text column so an unsplittable long word stops at the
        # padding instead of running over the rounded edge.
        cr.rectangle(self.PAD_X, 0, width - 2 * self.PAD_X, height)
        cr.clip()
        cr.push_group()
        self._apply_font(cr)
        cr.set_source_rgba(1, 1, 1, 0.92)
        for i, line in enumerate(reversed(rows)):
            y = height - self.PAD_BOTTOM - self.BASELINE_DROP - i * self.LINE_H
            cr.move_to(self.PAD_X, y)
            cr.show_text(line)
        cr.pop_group_to_source()
        if len(rows) > self.ROWS_FULL:
            # Older content fades out at the top rather than being cut off.
            fade = cairo.LinearGradient(0, self.PAD_TOP - 1,
                                        0, self.PAD_TOP + self.LINE_H * 0.95)
            fade.add_color_stop_rgba(0.0, 1, 1, 1, 0.0)
            fade.add_color_stop_rgba(1.0, 1, 1, 1, 1.0)
            cr.mask(fade)
        else:
            cr.paint()
        cr.restore()
        return False


class PillOverlay:
    """Compact always-visible-while-active status capsule."""

    WIDTH = 170
    HEIGHT = 36
    PREPARING_WIDTH = 214
    SUCCESS_WIDTH = 112
    ERROR_WIDTH = 348
    ERROR_HEIGHT = 44
    ERROR_RADIUS = 10          # not a capsule: the shape is the category

    BARS = 12
    BAR_W = 3
    BAR_PITCH = 5.5
    BAR_MAX = 20
    BAR_MIN = 2

    MARGIN = 40
    GAP = 8                    # pill-to-preview clearance
    FRAME_MS = 50              # ~20 Hz
    SUCCESS_MS = 500
    ERROR_MS = 3000

    RED = (0.94, 0.27, 0.27)
    GREEN = (0.30, 0.82, 0.45)
    AMBER = (0.96, 0.72, 0.25)

    # States that assert "the microphone is open right now".  They are the
    # only ones the capture probe and the watchdog police.
    ACTIVE_STATES = ("listening", "paused")

    # States that mean "a take is in flight or has just landed".  Outside these
    # there is no take whose decoded text the preview could be showing, so the
    # panel is emptied rather than left on screen.
    PREVIEW_STATES = ("listening", "speech", "processing", "success", "paused")

    DEBUG_MAX_MS = 30000

    def __init__(self, config: AppConfig):
        self._config = config
        self._enabled = bool(config.overlay)
        self._state = "hidden"
        self._message = ""
        self._speech = False
        self._levels = [0.0] * self.BARS
        self._t_start = 0.0
        self._frozen = 0.0
        self._spin = 0.0
        self._frame_source = 0
        self._grace_source = 0
        self._dismiss_source = 0
        self._watchdog_source = 0
        self._last_level = 0.0
        self._probe = None
        self._debug = False
        self._shell = None
        self.layered = False
        self._win = None
        # A sibling surface, not a taller pill: the pill's own geometry is
        # untouched by the preview existing, appearing or growing.
        self._preview = TranscriptPreview(config, self.MARGIN + self.HEIGHT + self.GAP)
        self._build()

    # --- window ----------------------------------------------------------

    def _build(self):
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title(f"{APP_NAME} overlay")
        win.set_app_paintable(True)
        win.set_decorated(False)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        visual = win.get_screen().get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)

        try:
            gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import GtkLayerShell
            if not GtkLayerShell.is_supported():
                raise RuntimeError("compositor has no wlr-layer-shell")
            GtkLayerShell.init_for_window(win)
            GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
            GtkLayerShell.set_exclusive_zone(win, -1)  # panels must not move
            self._shell = GtkLayerShell
            self.layered = True
            DIAG.log("overlay_backend", backend="layer_shell",
                     version=GtkLayerShell.get_protocol_version())
        except Exception as e:
            # Undecorated keep-above window instead; it will not sit over
            # fullscreen clients and the compositor may still focus it.
            win.set_keep_above(True)
            win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
            DIAG.log("overlay_backend", backend="fallback_window",
                     err=type(e).__name__)

        win.connect("draw", self._on_draw)
        win.connect("realize", lambda _w: self._set_click_through())
        self._win = win
        self._apply_anchor()

    def _apply_anchor(self):
        if not self.layered:
            return
        top = self._config.overlay_position == "top"
        shell, win = self._shell, self._win
        shell.set_anchor(win, shell.Edge.TOP, top)
        shell.set_anchor(win, shell.Edge.BOTTOM, not top)
        shell.set_anchor(win, shell.Edge.LEFT, False)
        shell.set_anchor(win, shell.Edge.RIGHT, False)
        shell.set_margin(win, shell.Edge.TOP, self.MARGIN)
        shell.set_margin(win, shell.Edge.BOTTOM, self.MARGIN)

    def _place_fallback(self, width, height):
        if self.layered:
            return
        display = Gdk.Display.get_default()
        # A multi-head KDE session often marks no monitor primary at all, so
        # fall back to the first one rather than leaving the pill unplaced.
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        area = monitor.get_workarea()
        x = area.x + (area.width - width) // 2
        if self._config.overlay_position == "top":
            y = area.y + self.MARGIN
        else:
            y = area.y + area.height - height - self.MARGIN
        self._win.move(x, y)

    def _set_click_through(self):
        """Empty input region — every click falls through to the app below."""
        gdk_win = self._win.get_window()
        if gdk_win is not None:
            gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)

    # --- state -----------------------------------------------------------

    def set_capture_probe(self, probe):
        """`probe()` answers: is the engine actually capturing right now?"""
        self._probe = probe

    def _capture_live(self) -> bool:
        try:
            return bool(self._probe()) if self._probe else False
        except Exception:
            return False

    def capture_started(self):
        """Anchor the elapsed clock to real capture, not to a UI transition."""
        self._t_start = time.monotonic()
        self._last_level = self._t_start

    def _start_watchdog(self):
        if not self._watchdog_source:
            self._watchdog_source = GLib.timeout_add(OVERLAY_WATCHDOG_MS,
                                                     self._on_watchdog)

    def _on_watchdog(self):
        """A pill that says "recording" while nothing is recording is worse
        than no pill at all, so the claim is re-checked against the engine
        rather than trusted for the life of the take."""
        if self._state not in self.ACTIVE_STATES:
            self._watchdog_source = 0
            return GLib.SOURCE_REMOVE
        if self._debug:
            return GLib.SOURCE_CONTINUE
        if not self._capture_live():
            DIAG.log("overlay_watchdog", state=self._state, action="hide",
                     reason="engine_not_capturing")
            self._watchdog_source = 0
            self.set_state("hidden")
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _levels_stale(self) -> bool:
        return (time.monotonic() - self._last_level) > OVERLAY_LEVEL_STALE_S

    # --- debug entry point (screenshots, tests) --------------------------

    def debug_force_state(self, state: str, message: str = "", levels=None,
                          speech: bool = False, elapsed: float = 0.0):
        """Force a state, bypassing the capture checks.  Harnesses only.

        This is the one door around the "only claim live capture when the
        engine is capturing" rule, so it arms a hard auto-hide: a forced pill
        cannot outlive the harness that put it there.
        """
        self._debug = True
        now = time.monotonic()
        self._t_start = now - max(elapsed, 0.0)
        self._last_level = now
        self.set_state(state, message)
        if levels:
            for value in levels:
                self.push_level(value, speech)
        self._clear_source("_dismiss_source")
        self._dismiss_source = GLib.timeout_add(self.DEBUG_MAX_MS, self._auto_hide)

    def debug_release(self):
        self._debug = False
        self.set_state("hidden")

    def apply_config(self, config: AppConfig):
        self._config = config
        self._enabled = bool(config.overlay)
        self._apply_anchor()
        self._preview.apply_config(config, self.MARGIN + self.HEIGHT + self.GAP)
        if not self._enabled or not config.preview:
            self.preview_reset()
        if not self._enabled:
            self.set_state("hidden")

    # --- transcript preview (display only — it never inserts) -------------

    @property
    def preview(self) -> "TranscriptPreview":
        return self._preview

    def preview_append(self, text: str):
        """Show one already-decoded segment of this take in the floating panel.

        Display only.  This is the whole reason the panel exists: the text
        cannot be inserted yet without ending the take (see TranscriptPreview),
        so it is painted instead.
        """
        if not (self._enabled and self._config.preview):
            return
        if not self._preview.append(text):
            return
        if self._state in self.PREVIEW_STATES:
            self._preview.show()
        else:
            # Nothing is claiming a take right now; keep the text but do not
            # put a panel on screen that the pill does not back up.
            DIAG.log("preview_refused", state=self._state)

    def preview_hypothesis(self, text: str):
        """Show the running guess for the phrase still being spoken.

        Same display-only path as preview_append, and the same gate: a panel is
        only ever on screen while the pill is claiming a take.
        """
        if not (self._enabled and self._config.preview):
            return
        if not self._preview.set_hypothesis(text):
            return
        if self._state in self.PREVIEW_STATES:
            self._preview.show()

    def preview_reset(self):
        self._preview.reset()
        self._preview.hide()

    def _sync_preview(self, state: str):
        if state in self.PREVIEW_STATES:
            return
        self.preview_reset()

    def set_state(self, state: str, message: str = ""):
        if not self._enabled and state != "hidden":
            return
        if state in self.ACTIVE_STATES and not self._debug and not self._capture_live():
            # Unrepresentable by construction, not merely avoided: nothing can
            # put the pill into a live-microphone state while the engine says
            # there is no session.
            DIAG.log("overlay_state_refused", state=state, reason="no_capture")
            state = "hidden"
        self._clear_source("_grace_source")
        self._clear_source("_dismiss_source")
        # The preview lives and dies with the take, so it follows the pill:
        # it disappears with the success state, not on a timer of its own.
        self._sync_preview(state)

        if state == "preparing":
            # Caption the wait only once it is long enough to be worth saying.
            # A warm start reaches "listening" well inside the grace, so it
            # never flashes.
            self._state = "preparing"
            self._grace_source = GLib.timeout_add(PREPARING_GRACE_MS,
                                                  self._show_preparing)
            DIAG.log("overlay_state", state="preparing", deferred=True)
            return

        self._state = state
        self._message = message
        DIAG.log("overlay_state", state=state)

        if state == "hidden":
            self._hide()
            return
        if state == "listening":
            # The clock belongs to the capture session; capture_started() sets
            # it.  Without a session there is nothing honest to count from.
            if not self._t_start:
                self._t_start = time.monotonic()
            self._start_watchdog()
        elif state == "paused":
            self._start_watchdog()
        elif state == "processing":
            self._frozen = self._elapsed()
        elif state == "success":
            self._frozen = self._elapsed()
            self._dismiss_source = GLib.timeout_add(self.SUCCESS_MS, self._auto_hide)
        elif state == "error":
            self._dismiss_source = GLib.timeout_add(self.ERROR_MS, self._auto_hide)
        self._show()

    def push_level(self, rms: float, speech: bool):
        """One RMS reading from the live capture loop — not a timer animation."""
        self._levels.append(rms)
        del self._levels[:-self.BARS]
        self._speech = bool(speech)
        self._last_level = time.monotonic()

    def shutdown(self):
        self._debug = False
        self.set_state("hidden")
        self._preview.shutdown()
        if self._win is not None:
            self._win.destroy()

    # --- internals -------------------------------------------------------

    def _clear_source(self, attr):
        src = getattr(self, attr)
        if src:
            GLib.source_remove(src)
            setattr(self, attr, 0)

    def _show_preparing(self):
        self._grace_source = 0
        if self._state != "preparing":
            return GLib.SOURCE_REMOVE
        DIAG.log("overlay_state", state="preparing", shown=True)
        self._show()
        return GLib.SOURCE_REMOVE

    def _auto_hide(self):
        self._dismiss_source = 0
        self.set_state("hidden")
        return GLib.SOURCE_REMOVE

    def _size(self):
        if self._state == "error":
            return self.ERROR_WIDTH, self.ERROR_HEIGHT
        if self._state == "preparing":
            return self.PREPARING_WIDTH, self.HEIGHT
        if self._state == "success":
            return self.SUCCESS_WIDTH, self.HEIGHT
        return self.WIDTH, self.HEIGHT

    def _show(self):
        width, height = self._size()
        self._win.set_size_request(width, height)
        self._win.resize(width, height)
        if not self._win.get_visible():
            self._win.show_all()
        self._place_fallback(width, height)
        self._set_click_through()
        if not self._frame_source:
            self._frame_source = GLib.timeout_add(self.FRAME_MS, self._on_frame)
        self._win.queue_draw()

    def _hide(self):
        self._clear_source("_frame_source")
        self._clear_source("_watchdog_source")
        self._t_start = 0.0
        self._last_level = 0.0
        self._levels = [0.0] * self.BARS
        self._speech = False
        if self._win is not None:
            self._win.hide()

    def _on_frame(self):
        self._spin += 0.16
        self._win.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _elapsed(self) -> float:
        return (time.monotonic() - self._t_start) if self._t_start else 0.0

    @staticmethod
    def _clock(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 60}:{total % 60:02d}"

    @staticmethod
    def _bar_level(rms: float) -> float:
        """RMS -> 0..1.  Speech sits around 0.02-0.2 RMS, so a linear meter
        pins to the floor; dBFS over a -60..-6 window keeps quiet speech
        legible without the loud end clipping."""
        if rms <= 1e-5:
            return 0.0
        db = 20.0 * math.log10(min(rms, 1.0))
        return max(0.0, min(1.0, (db + 60.0) / 54.0))

    # --- drawing ---------------------------------------------------------

    @staticmethod
    def _rounded_rect(cr, x, y, width, height, radius):
        cr.new_sub_path()
        cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
        cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
        cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
        cr.arc(x + radius, y + radius, radius, math.pi, 1.5 * math.pi)
        cr.close_path()

    def _on_draw(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()

        cr.set_operator(cairo.Operator.SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.Operator.OVER)

        radius = self.ERROR_RADIUS if self._state == "error" else height / 2
        self._rounded_rect(cr, 0.5, 0.5, width - 1, height - 1, radius)
        cr.set_source_rgba(0, 0, 0, 0.9)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.1)
        cr.set_line_width(1)
        cr.stroke()

        if self._state == "error":
            self._draw_error(cr, width, height)
        elif self._state == "preparing":
            self._draw_preparing(cr, height)
        elif self._state == "success":
            self._draw_success(cr, width, height)
        else:
            self._draw_active(cr, width, height)
        return False

    def _text(self, cr, x, y, text, size=11.5, rgba=(1, 1, 1, 0.85), bold=False):
        cr.select_font_face("Sans", cairo.FontSlant.NORMAL,
                            cairo.FontWeight.BOLD if bold else cairo.FontWeight.NORMAL)
        cr.set_font_size(size)
        cr.set_source_rgba(*rgba)
        cr.move_to(x, y)
        cr.show_text(text)

    def _text_width(self, cr, text, size=11.5):
        cr.select_font_face("Sans", cairo.FontSlant.NORMAL, cairo.FontWeight.NORMAL)
        cr.set_font_size(size)
        return cr.text_extents(text)[4]

    def _dot(self, cr, cx, cy, rgb, alpha=1.0):
        cr.set_source_rgba(*rgb, alpha)
        cr.arc(cx, cy, 4, 0, 2 * math.pi)
        cr.fill()

    def _spinner(self, cr, cx, cy, radius=7.5):
        cr.set_line_width(2.2)
        cr.set_line_cap(cairo.LineCap.ROUND)
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.arc(cx, cy, radius, self._spin, self._spin + 1.9)
        cr.stroke()

    def _draw_active(self, cr, width, height):
        """listening / speech / processing / paused — one capsule, one red dot."""
        cy = height / 2
        paused = self._state == "paused"
        processing = self._state == "processing"
        self._dot(cr, 18, cy, self.AMBER if paused else self.RED,
                  0.9 if paused else 1.0)

        if processing:
            self._spinner(cr, 63, cy)
        else:
            # No buffers for a while means the capture loop is not feeding us.
            # Flatten the meter rather than keep drawing motion the microphone
            # is not producing.
            stale = self._levels_stale()
            if stale:
                alpha = 0.18
            elif paused:
                alpha = 0.3
            elif self._speech:
                alpha = 0.95
            else:
                alpha = 0.45
            cr.set_source_rgba(1, 1, 1, alpha)
            for i, rms in enumerate(self._levels[-self.BARS:]):
                bar_h = self.BAR_MIN if stale else max(
                    self.BAR_MIN, self._bar_level(rms) * self.BAR_MAX)
                x = 32 + i * self.BAR_PITCH
                cr.rectangle(x, cy - bar_h / 2, self.BAR_W, bar_h)
            cr.fill()

        clock = self._clock(self._frozen if processing else self._elapsed())
        self._text(cr, width - 14 - self._text_width(cr, clock), cy + 4, clock)

    def _draw_preparing(self, cr, height):
        cy = height / 2
        self._spinner(cr, 20, cy, 6.5)
        self._text(cr, 36, cy + 4, "Preparing engine…")

    def _draw_success(self, cr, width, height):
        cy = height / 2
        cr.set_line_width(2.6)
        cr.set_line_cap(cairo.LineCap.ROUND)
        cr.set_line_join(cairo.LineJoin.ROUND)
        cr.set_source_rgb(*self.GREEN)
        cr.move_to(18, cy)
        cr.line_to(22, cy + 4.5)
        cr.line_to(29, cy - 5)
        cr.stroke()
        clock = self._clock(self._frozen)
        self._text(cr, width - 14 - self._text_width(cr, clock), cy + 4, clock)

    def _draw_error(self, cr, width, height):
        cy = height / 2
        self._dot(cr, 20, cy, self.RED)
        message = self._message or "Dictation failed"
        self._text(cr, 36, cy + 4, message, rgba=(1, 1, 1, 0.92))


# ---------------------------------------------------------------------------
# Main window (undockable full-size UI)
# ---------------------------------------------------------------------------

class MainWindow(Gtk.Window):
    def __init__(self, controller: DictationController, hotkey_mgr: HotkeyManager):
        super().__init__(title=APP_NAME)
        self._controller = controller
        self._hotkey_mgr = hotkey_mgr
        self.set_default_size(480, -1)
        self.set_resizable(False)
        self.set_icon_name("audio-input-microphone")
        self.connect("delete-event", self._on_delete)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        self.add(vbox)

        # --- Status ---
        self._status_label = Gtk.Label()
        self._status_label.set_markup("<span size='large'>Idle</span>")
        self._status_label.set_halign(Gtk.Align.CENTER)
        vbox.pack_start(self._status_label, False, False, 0)

        # --- Controls ---
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)

        self._toggle_btn = Gtk.Button()
        self._toggle_btn.get_style_context().add_class("suggested-action")
        self._toggle_btn.connect("clicked", self._on_toggle)
        btn_box.pack_start(self._toggle_btn, False, False, 0)

        self._pause_btn = Gtk.Button(label="Pause")
        self._pause_btn.set_sensitive(False)
        self._pause_btn.connect("clicked", lambda _: self._controller.pause())
        btn_box.pack_start(self._pause_btn, False, False, 0)

        vbox.pack_start(btn_box, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 0)

        # --- Microphone selector ---
        mic_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mic_box.pack_start(Gtk.Label(label="Microphone:"), False, False, 0)
        self._mic_combo = Gtk.ComboBoxText()
        self._mic_combo.append("", "System Default")
        for dev in list_input_devices():
            self._mic_combo.append(str(dev["index"]), dev["name"])
        self._mic_combo.set_active_id(self._controller.config.audio_device or "")
        self._mic_combo.connect("changed", self._on_mic_changed)
        mic_box.pack_start(self._mic_combo, True, True, 0)
        vbox.pack_start(mic_box, False, False, 0)

        # --- Model selector ---
        model_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        model_box.pack_start(Gtk.Label(label="Model:"), False, False, 0)
        self._model_combo = Gtk.ComboBoxText()
        for mid, mdata in self._controller.profiles.items():
            downloaded = _is_model_downloaded(mid, self._controller.profiles)
            label = mdata["name"]
            if not downloaded:
                label += " (not downloaded)"
            self._model_combo.append(mid, label)
        self._model_combo.set_active_id(self._controller.config.model_profile)
        self._model_combo.connect("changed", self._on_model_changed)
        model_box.pack_start(self._model_combo, True, True, 0)
        vbox.pack_start(model_box, False, False, 0)

        # --- Streaming toggle ---
        self._streaming_check = Gtk.CheckButton(label="Streaming mode (type as you speak)")
        self._streaming_check.set_tooltip_text(
            "Switch between real-time streaming (Nemotron) and "
            "VAD-segmented transcription (waits for pause, higher accuracy).")
        is_streaming = self._controller.profiles.get(
            self._controller.config.model_profile, {}).get("streaming", False)
        self._streaming_check.set_active(is_streaming)
        # Remember the non-streaming model so we can restore it
        if is_streaming:
            self._non_streaming_model = "desktop"
        else:
            self._non_streaming_model = self._controller.config.model_profile
        self._streaming_check.connect("toggled", self._on_streaming_toggled)
        vbox.pack_start(self._streaming_check, False, False, 0)

        self._update_controls()

    def _on_delete(self, _win, _event):
        self.hide()
        return True

    def _on_toggle(self, _btn):
        self._controller.toggle()
        self._update_controls()

    def _on_mic_changed(self, combo):
        dev_id = combo.get_active_id() or ""
        cfg = self._controller.config
        cfg.audio_device = dev_id
        cfg.save()

    def _on_model_changed(self, combo):
        model_id = combo.get_active_id()
        if not model_id or model_id == self._controller.config.model_profile:
            return
        if not _is_model_downloaded(model_id, self._controller.profiles):
            self._model_combo.set_active_id(self._controller.config.model_profile)
            return
        new_config = self._controller.config
        new_config.model_profile = model_id
        self._controller.apply_config(new_config)
        self._hotkey_mgr.rebuild(new_config)
        # Keep streaming checkbox in sync
        is_streaming = self._controller.profiles.get(model_id, {}).get("streaming", False)
        self._streaming_check.handler_block_by_func(self._on_streaming_toggled)
        self._streaming_check.set_active(is_streaming)
        self._streaming_check.handler_unblock_by_func(self._on_streaming_toggled)
        if not is_streaming:
            self._non_streaming_model = model_id
        if hasattr(self, "_tray") and self._tray:
            self._tray._build_menu()
            self._tray.update_ui()

    def _on_streaming_toggled(self, check):
        if check.get_active():
            # Remember current non-streaming model, switch to streaming
            cur = self._controller.config.model_profile
            cur_profile = self._controller.profiles.get(cur, {})
            if not cur_profile.get("streaming", False):
                self._non_streaming_model = cur
            target = "streaming"
        else:
            # Restore previous non-streaming model
            target = getattr(self, "_non_streaming_model", "desktop")

        if not _is_model_downloaded(target, self._controller.profiles):
            # Can't switch — revert checkbox
            check.handler_block_by_func(self._on_streaming_toggled)
            check.set_active(not check.get_active())
            check.handler_unblock_by_func(self._on_streaming_toggled)
            return

        new_config = self._controller.config
        new_config.model_profile = target
        self._controller.apply_config(new_config)
        self._hotkey_mgr.rebuild(new_config)
        # Sync the model combo
        self._model_combo.set_active_id(target)
        if hasattr(self, "_tray") and self._tray:
            self._tray._build_menu()
            self._tray.update_ui()

    def _update_controls(self):
        running = self._controller.is_running
        paused = self._controller.is_paused
        cfg = self._controller.config

        if running:
            if paused:
                self._toggle_btn.set_label("Resume")
                self._status_label.set_markup("<span size='large'>Paused</span>")
            else:
                self._toggle_btn.set_label("Stop")
                self._status_label.set_markup("<span size='large'>Listening...</span>")
            self._pause_btn.set_sensitive(True)
        else:
            self._toggle_btn.set_label("Start Dictation")
            self._status_label.set_markup("<span size='large'>Idle</span>")
            self._pause_btn.set_sensitive(False)

        # Sync model combo and streaming checkbox if changed externally
        if self._model_combo.get_active_id() != cfg.model_profile:
            self._model_combo.set_active_id(cfg.model_profile)
        is_streaming = self._controller.profiles.get(
            cfg.model_profile, {}).get("streaming", False)
        if self._streaming_check.get_active() != is_streaming:
            self._streaming_check.handler_block_by_func(self._on_streaming_toggled)
            self._streaming_check.set_active(is_streaming)
            self._streaming_check.handler_unblock_by_func(self._on_streaming_toggled)

    def on_status_update(self, text: str):
        self._update_controls()
        if text and text not in ("Listening...", "Ready", "Resumed", "Paused"):
            display = text[:60] + "\u2026" if len(text) > 60 else text
            self._status_label.set_markup(f"<span size='large'>\u25b6 {GLib.markup_escape_text(display)}</span>")


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------

class TrayIcon:
    def __init__(self, controller: DictationController, hotkey_mgr: HotkeyManager,
                 main_window: MainWindow):
        self._controller = controller
        self._hotkey_mgr = hotkey_mgr
        self._main_window = main_window

        self._indicator = AyatanaAppIndicator3.Indicator.new(
            APP_ID,
            "audio-input-microphone-muted",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self._indicator.set_title(APP_NAME)

        self._build_menu()
        controller.set_status_callback(self._on_status_update)

    def _build_menu(self):
        menu = Gtk.Menu()
        cfg = self._controller.config

        show_window_item = Gtk.MenuItem(label="Show Window")
        show_window_item.connect("activate", lambda _: (self._main_window.show_all(), self._main_window.present()))
        menu.append(show_window_item)

        menu.append(Gtk.SeparatorMenuItem())

        self._toggle_item = Gtk.MenuItem(label=f"Start Dictation ({cfg.hotkey_toggle})")
        self._toggle_item.connect("activate", self._on_toggle)
        menu.append(self._toggle_item)

        self._pause_item = Gtk.MenuItem(label=f"Pause ({cfg.hotkey_pause})")
        self._pause_item.connect("activate", lambda _: self._controller.pause())
        self._pause_item.set_sensitive(False)
        menu.append(self._pause_item)

        self._status_item = Gtk.MenuItem(label="Idle")
        self._status_item.set_sensitive(False)
        menu.append(self._status_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Model switcher submenu
        model_menu_item = Gtk.MenuItem(label="Model")
        model_submenu = Gtk.Menu()
        active_profile = cfg.model_profile
        for mid, mdata in self._controller.profiles.items():
            downloaded = _is_model_downloaded(mid, self._controller.profiles)
            label = mdata["name"]
            if mid == active_profile:
                label = f"\u2713 {label}"
            elif not downloaded:
                label = f"  {label} (not downloaded)"
            else:
                label = f"  {label}"
            item = Gtk.MenuItem(label=label)
            if downloaded and mid != active_profile:
                item.connect("activate", self._on_switch_model, mid)
            else:
                item.set_sensitive(False)
            model_submenu.append(item)
        model_menu_item.set_submenu(model_submenu)
        menu.append(model_menu_item)

        menu.append(Gtk.SeparatorMenuItem())

        settings_item = Gtk.MenuItem(label="Settings")
        settings_item.connect("activate", self._on_settings)
        menu.append(settings_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        self._indicator.set_menu(menu)

    def _on_switch_model(self, _item, model_id):
        new_config = self._controller.config
        new_config.model_profile = model_id
        self._controller.apply_config(new_config)
        self._hotkey_mgr.rebuild(new_config)
        self._build_menu()
        self.update_ui()

    def _on_toggle(self, _item=None):
        self._controller.toggle()
        self.update_ui()

    def update_ui(self):
        running = self._controller.is_running
        paused = self._controller.is_paused
        cfg = self._controller.config

        if running:
            if paused:
                self._toggle_item.set_label("Resume Dictation")
                self._indicator.set_icon_full("audio-input-microphone-muted", "Paused")
            else:
                key = cfg.hotkey_toggle if cfg.hotkey_mode == "toggle" else cfg.hotkey_stop
                self._toggle_item.set_label(f"Stop Dictation ({key})")
                self._indicator.set_icon_full("audio-input-microphone", "Listening")
            self._pause_item.set_sensitive(True)
        else:
            key = cfg.hotkey_toggle if cfg.hotkey_mode == "toggle" else cfg.hotkey_start
            self._toggle_item.set_label(f"Start Dictation ({key})")
            self._indicator.set_icon_full("audio-input-microphone-muted", "Idle")
            self._pause_item.set_sensitive(False)
            self._status_item.set_label("Idle")

    def _on_status_update(self, text: str):
        self.update_ui()
        self._main_window.on_status_update(text)
        if text:
            display = text[:60] + "\u2026" if len(text) > 60 else text
            self._status_item.set_label(f"\u25b6 {display}")
        else:
            if self._controller.is_running:
                self._status_item.set_label("Ready")
            else:
                self._status_item.set_label("Idle")

    def _on_settings(self, _item):
        SettingsDialog(
            self._controller.config,
            self._controller.profiles_data,
            on_save=self._apply_settings,
        )

    def _apply_settings(self, new_config: AppConfig):
        self._controller.apply_config(new_config)
        self._controller.notify(self._hotkey_mgr.rebuild(new_config))
        self._build_menu()
        self.update_ui()

    def _on_quit(self, _item):
        self._controller.shutdown()
        Gtk.main_quit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _active_config
    config = AppConfig.load()
    _active_config = config

    # Ensure typer is a valid Wayland method
    if config.typer not in ("clipboard", "wtype", "ydotool"):
        config.typer = "clipboard"

    DIAG.set_enabled(config.diagnostics)
    DIAG.log("app_start", profile=config.model_profile,
             threads=config.num_threads, typer=config.typer,
             paste_chord=config.paste_chord,
             kdotool=bool(shutil.which("kdotool")))

    controller = DictationController(config)
    overlay = PillOverlay(config)
    controller.set_overlay(overlay)
    # Warm the model now; otherwise the first dictation start pays the ~1.6 s
    # load while the user is already speaking.
    controller.preload()

    # tray referenced in hotkey lambdas — assigned after creation
    tray = None

    hotkey_mgr = HotkeyManager(
        config,
        on_toggle=lambda: (controller.toggle(), tray and tray.update_ui()),
        on_start=lambda: (controller.start(), tray and tray.update_ui()),
        on_stop=lambda: (controller.stop(), tray and tray.update_ui()),
        on_pause=lambda: controller.pause(),
        on_hold_press=lambda: (controller.hold_press(), tray and tray.update_ui()),
        on_hold_release=lambda: (controller.hold_release(), tray and tray.update_ui()),
    )

    main_window = MainWindow(controller, hotkey_mgr)

    tray = TrayIcon(controller, hotkey_mgr, main_window)
    main_window._tray = tray  # So model changes from window rebuild tray menu
    controller.notify(hotkey_mgr.start())

    signal.signal(signal.SIGINT, lambda *_: (controller.shutdown(), Gtk.main_quit()))

    # Global hotkeys on Wayland: pynput cannot grab keys outside the app's own
    # windows, so the desktop's shortcut system drives us over signals instead.
    # Bind a KDE shortcut to `parakeet-toggle` (SIGUSR1 = toggle, SIGUSR2 = pause).
    def _signal_toggle(*_):
        controller.toggle()
        if tray:
            tray.update_ui()
        return GLib.SOURCE_CONTINUE

    def _signal_pause(*_):
        controller.pause()
        if tray:
            tray.update_ui()
        return GLib.SOURCE_CONTINUE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, _signal_toggle)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, _signal_pause)

    # First-run: show welcome dialog if no models are downloaded
    profiles_data = controller.profiles_data
    if not _any_model_downloaded(profiles_data["profiles"]):
        def _on_model_ready(new_config):
            controller.apply_config(new_config)
            hotkey_mgr.rebuild(new_config)
            tray._build_menu()
            tray.update_ui()
        WelcomeDialog(profiles_data, config, _on_model_ready)

    profile_name = controller.profiles.get(
        config.model_profile, {}
    ).get("name", config.model_profile)
    if config.hotkey_mode == "hold":
        mode_desc = f"Push-to-talk: hold {config.hotkey_hold}"
    elif config.hotkey_mode == "toggle":
        mode_desc = f"Toggle: {config.hotkey_toggle}"
    else:
        mode_desc = f"Start: {config.hotkey_start}, Stop: {config.hotkey_stop}"
    print(f"{APP_NAME} running. {mode_desc}. Pause: {config.hotkey_pause}")
    print(f"Model: {profile_name} | Typer: {config.typer} | Threads: {config.num_threads}")

    Gtk.main()
    hotkey_mgr.stop()
    overlay.shutdown()


if __name__ == "__main__":
    main()
