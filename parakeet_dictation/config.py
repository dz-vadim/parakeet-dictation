"""Paths and AppConfig (+ load/save)."""

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_NAME = "Parakeet Dictation"
APP_ID = "parakeet-dictation"
CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
APP_DIR = Path(__file__).resolve().parent.parent   # the checkout root, as before the split
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_ID
MODELS_DIR = DATA_DIR / "models"

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
    # The chord is chosen per focused window (see insert.choose_chord):
    # `paste_chord` for everything unknown or unlisted, `terminal_paste_chord`
    # for `terminal_window_classes` (Ctrl+V is destructive in a terminal),
    # Shift+Insert for `no_ctrl_v_classes` (Emacs), and `paste_overrides`
    # ({window class: chord}) wins over all of the tables.  Chords are
    # "ctrl+v", "ctrl+shift+v" or "shift+insert"; classes are KWin
    # resourceClass values, matched case-insensitively.
    keep_on_clipboard: bool = False
    paste_chord: str = "ctrl+v"
    terminal_paste_chord: str = "ctrl+shift+v"
    terminal_window_classes: list = field(default_factory=lambda: [
        "kitty", "Alacritty", "alacritty", "org.kde.konsole", "konsole",
        "yakuake", "foot", "footclient", "org.wezfurlong.wezterm", "wezterm",
        "com.mitchellh.ghostty", "XTerm", "xterm", "st", "contour",
        "gnome-terminal-server", "terminator", "com.gexperts.Tilix",
    ])
    no_ctrl_v_classes: list = field(default_factory=lambda: ["emacs", "Emacs"])
    paste_overrides: dict = field(default_factory=dict)

    # How the chord is pressed: "portal" (xdg-desktop-portal RemoteDesktop,
    # layout independent, one "Remote Control" prompt ever), "ydotool"
    # (uinput keycodes, needs ydotoold), or "auto" — the portal when it is
    # available, ydotool otherwise.
    paste_transport: str = "auto"

    # Learn the focused window from a resident KWin script so the chord can
    # be chosen per app.  Off, or on a compositor without KWin scripting, the
    # target is "unknown" and `paste_chord` is used everywhere.
    focus_script: bool = True

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
    def load(log=None) -> "AppConfig":
        """Read CONFIG_FILE.  Never raises; every problem is reported.

        `log(event, **fields)` receives one `config_error` per problem (the
        app passes DIAG.log so it lands in diagnostics.log); the same line
        always goes to stderr as well, since a config problem is something
        the person at the keyboard has to act on.

        A file that does not parse is moved aside as
        `config.json.broken-<timestamp>` and the defaults are used — the
        edit is never lost, and a later save() cannot overwrite it.  A file
        that parses keeps every key whose value has the right type; a key of
        the wrong type is dropped (its default applies) and named.  Unknown
        keys are ignored, as before.
        """
        report = _reporter(log)
        if not CONFIG_FILE.exists():
            return AppConfig()
        try:
            raw = CONFIG_FILE.read_bytes()
        except OSError as e:
            report("config_error", reason="unreadable", err=type(e).__name__,
                   path=CONFIG_FILE.name)
            return AppConfig()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"top level is {type(data).__name__}, not an object")
        except ValueError as e:              # JSONDecodeError, UnicodeDecodeError
            backup = _back_up_broken(CONFIG_FILE)
            report("config_error", reason="unparseable", err=type(e).__name__,
                   line=getattr(e, "lineno", 0), col=getattr(e, "colno", 0),
                   backup=backup.name if backup else "failed",
                   detail=str(e)[:80])
            return AppConfig()
        kwargs = {}
        for key, value in data.items():
            field_ = AppConfig.__dataclass_fields__.get(key)
            if field_ is None:
                continue
            ok, value = _coerce(field_.type, value)
            if not ok:
                report("config_error", reason="bad_value", key=key,
                       expected=field_.type.__name__, got=type(value).__name__)
                continue
            kwargs[key] = value
        return AppConfig(**kwargs)


def _coerce(field_type, value):
    """(accepted, value) for one JSON value against a field's annotation.

    JSON has no int/float split, so an int is widened for a float field;
    everything else must match exactly.  bool is an int subclass in Python,
    so it is checked first and rejected for numeric fields.
    """
    if field_type is bool:
        return isinstance(value, bool), value
    if isinstance(value, bool):
        return False, value
    if field_type is int:
        return isinstance(value, int), value
    if field_type is float:
        if isinstance(value, (int, float)):
            return True, float(value)
        return False, value
    if field_type is str:
        return isinstance(value, str), value
    if field_type is list:
        return isinstance(value, list), value
    if field_type is dict:
        return isinstance(value, dict), value
    return True, value


def _back_up_broken(path: Path):
    """Move an unparseable config aside.  Returns the backup path, or None."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = path.with_name(f"{path.name}.broken-{stamp}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.broken-{stamp}-{n}")
        n += 1
    try:
        os.replace(path, backup)
    except OSError:
        return None
    return backup


def _reporter(log):
    def report(event, **fields):
        line = " ".join([event] + [f"{k}={v}" for k, v in fields.items()])
        print(f"WARNING: {line}", file=sys.stderr)
        if log is not None:
            log(event, **fields)
    return report
