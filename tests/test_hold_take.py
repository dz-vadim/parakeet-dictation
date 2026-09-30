#!/usr/bin/env python3
"""Drive realistic push-to-talk takes through the real code with a fake microphone.

Covers the three defects the user hit in real use:

  1. text inserted mid-take, while the key was still held
  2. speech lost when the key is released without pausing first
  3. a 0.25 s thinking pause treated as the end of a phrase
  4. no feedback at all during an end-of-take hold — the floating transcript
     preview, which must show every decoded segment and insert none of them
  5. one VAD phrase per decode call, which cost accuracy — consecutive phrases
     are coalesced into ~4 s blocks, and each block is loudness-normalised for
     the model only
  6. nothing on the panel until a phrase closes — a timed preview pass decodes
     the still-open audio and shows it as a replaceable hypothesis

Everything below the hotkey is real: the real TenVadDetector, the real
ASREngine offline loop with its decode queue and end-of-take flush, the real
Parakeet model, the real DictationController state machine and the real
TextTyper insertion path including its clipboard save/restore.  Only two things
are replaced — the microphone (a scripted stream that runs in real time, so a
release really does land mid-phrase) and the external binaries (wl-copy,
wl-paste, ydotool), whose stand-in keeps an in-memory clipboard so what the
"focused window" receives on each paste is observable.

Run:  .venv/bin/python tests/test_hold_take.py
"""

import ast
import inspect
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"   # fixture audio lives with the benchmark data
sys.path.insert(0, str(HERE.parent))

from parakeet_dictation import (audio as da_audio, config as da_config,  # noqa: E402
                                controller as da_controller, diagnostics as da_diagnostics,
                                engine as da_engine, focus as da_focus, insert as da_insert,
                                models as da_models)
from parakeet_dictation.ui import overlay as da_overlay  # noqa: E402
from gi.repository import GLib  # noqa: E402

SR = da_audio.SAMPLE_RATE
FAILURES = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Fixture audio — three real utterances with no shared marker word, plus the
# real noise floor of the same recording to stand in for a pause.
# ---------------------------------------------------------------------------

def load_wav(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR, f"{path} is not {SR} Hz"
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def trim(audio, thr=0.02):
    loud = np.nonzero(np.abs(audio) > thr)[0]
    return audio[loud[0]:loud[-1] + 1] if len(loud) else audio


SPEECH_A = trim(load_wav(BENCH / "staging" / "s90.wav")[: 3 * SR])
SPEECH_B = trim(load_wav(BENCH / "segs" / "01.wav"))
SPEECH_C = trim(load_wav(BENCH / "segs" / "00.wav"))
ROOM = load_wav(BENCH / "segs" / "04.wav")   # -45 dBFS noise floor, not zeros

# What the model makes of each piece on its own (checked in by observation, see
# the header of bench/BENCHMARK.md for how these recordings were made):
#   A  "Навіть коли я говорю досить довго й не роблю па"   -> marker "навіть"
#   B  "Open the settings and check whether the microphone works."
#                                                          -> marker "microphone"
#   C  "Довго і не роблю пауз посередині думки."           -> marker "посеред"
MARK_A, MARK_B, MARK_C = "навіть", ("microphone", "мікрофон"), "посеред"


def carries(text, mark) -> bool:
    """Whether a decode carries this phrase, under any spelling of it.

    MARK_B has two: inside a long Ukrainian block the multilingual model
    sometimes transliterates the English word ("microphone" -> "мікрофон"), and
    which one it picks flips with the exact block boundary the take ends on.
    These checks are about the phrase reaching the document, not about the
    model's choice of alphabet.
    """
    marks = mark if isinstance(mark, tuple) else (mark,)
    low = (text or "").lower()
    return any(m in low for m in marks)


def quiet(seconds):
    """A pause made of real room noise — the pause a person actually makes."""
    return np.resize(ROOM, int(seconds * SR))


# ---------------------------------------------------------------------------
# Stand-ins for the world outside the app
# ---------------------------------------------------------------------------

CLIP = {"text": ""}
PRIMARY = {"text": ""}
USER_CLIPBOARD = "USER-CLIPBOARD-BEFORE-DICTATION"
USER_PRIMARY = "USER-PRIMARY-SELECTION"
PASTES = []        # (t, text) that the focused window would have received
CHORDS = []        # the chord pressed for each of those pastes, in order
YDOTOOL_FAIL = {"n": 0}   # make the next n `ydotool key` calls exit non-zero

_KEYCODES_TO_CHORD = {v: k for k, v in da_insert._CHORD_KEYCODES.items()}


class _Completed:
    def __init__(self, rc=0, out=b""):
        self.returncode, self.stdout, self.stderr = rc, out, b""


def fake_run(args, **_kw):
    args = list(args)
    if args[0] == "wl-copy":
        sel = PRIMARY if ("--primary" in args or "-p" in args) else CLIP
        if "--clear" in args:
            sel["text"] = ""
        elif "--" in args:
            sel["text"] = args[-1]                       # staging: argv text
        else:                                            # restore: raw bytes on stdin
            sel["text"] = (_kw.get("input") or b"").decode("utf-8")
    elif args[0] == "wl-paste":
        sel = PRIMARY if ("--primary" in args or "-p" in args) else CLIP
        return _Completed(0, sel["text"].encode())
    elif args[0] == "ydotool" and len(args) > 1 and args[1] == "key":
        if YDOTOOL_FAIL["n"] > 0:
            YDOTOOL_FAIL["n"] -= 1
            return _Completed(1)
        PASTES.append((time.monotonic(), CLIP["text"]))
        CHORDS.append(_KEYCODES_TO_CHORD.get(tuple(args[2:]), "?"))
    return _Completed()


da_insert.subprocess.run = fake_run
da_insert.shutil.which = lambda name: f"/usr/bin/{name}" if name == "ydotool" else None
da_insert.portal_keyboard = lambda: None   # never open a portal session from a test
da_engine.play_beep_start = lambda *a, **k: None
da_engine.play_beep_stop = lambda *a, **k: None
da_engine.play_beep_pause = lambda *a, **k: None
da_engine.resolve_audio_device = lambda _value: None

# Nothing here calls AppConfig.save(), but point it at a scratch file anyway:
# a test must not be one refactor away from rewriting the user's real config.
da_config.CONFIG_DIR = HERE
da_config.CONFIG_FILE = HERE / ".test-hold-take-config.json"

DIAG_PATH = HERE / ".test-hold-take.log"
DIAG_PATH.unlink(missing_ok=True)
TEST_DIAG = da_diagnostics.DiagnosticLog(DIAG_PATH)
for _mod in (da_engine, da_controller, da_insert, da_overlay):   # every module a take runs through
    _mod.DIAG = TEST_DIAG


def diag_lines():
    return DIAG_PATH.read_text().splitlines() if DIAG_PATH.exists() else []


class FakeMic:
    """Scripted microphone that hands out 100 ms blocks in real time.

    Real time is the point: the release has to land while speech is still being
    delivered.  Past the end of the script it returns room noise rather than
    stopping, so the take ends on the key release and on nothing else.
    """

    def __init__(self, audio, block=1600):
        self._blocks = [audio[i:i + block]
                        for i in range(0, len(audio) - block + 1, block)]
        self._i = 0
        self.delivered = 0.0     # seconds of the script handed over so far

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, n):
        time.sleep(n / SR)
        if self._i < len(self._blocks):
            block = self._blocks[self._i]
            self._i += 1
            self.delivered += len(block) / SR
        else:
            block = quiet(n / SR)
        return block.reshape(-1, 1), False


class FakeOverlay:
    """Records pill states, and runs the REAL preview text model.

    Only the layer-shell window is left out: TranscriptPreview keeps its text,
    its segment count and its wrapped rows with no window at all, so the panel
    the user would see is observable here without putting one on screen.
    """

    def __init__(self, config=None):
        self.states = []
        self.previews = []        # (t, accumulated text, segments, rows)
        self.hypotheses = []      # (t, hypothesis, committed text, rows)
        self.preview_resets = 0
        self._config = config or da_config.AppConfig()
        # The same gate the real PillOverlay applies, so "preview off" here
        # means what it means in the app.
        self._on = bool(self._config.overlay and self._config.preview)
        self.panel = da_overlay.TranscriptPreview(self._config, clearance=84)

    def set_state(self, state, message=""):
        self.states.append((time.monotonic(), state, message))

    def push_level(self, *_a):
        pass

    def capture_started(self):
        pass

    def set_capture_probe(self, _probe):
        pass

    def apply_config(self, _config):
        pass

    def preview_append(self, text):
        if not self._on:
            return
        self.panel.append(text)
        self.previews.append((time.monotonic(), self.panel.text,
                              self.panel.segments, tuple(self.panel.rows)))

    def preview_hypothesis(self, text):
        if not self._on:
            return
        self.panel.set_hypothesis(text)
        self.hypotheses.append((time.monotonic(), self.panel.hypothesis,
                                self.panel.text, tuple(self.panel.rows)))

    def preview_reset(self):
        self.preview_resets += 1
        self.panel.reset()
        self.previews.clear()
        self.hypotheses.clear()


# ---------------------------------------------------------------------------
# One take, press to release, on a real GLib main loop
# ---------------------------------------------------------------------------

def run_take(audio, release_at_audio_s, insert_mode, label, preview=True,
             coalesce=0.0, preview_interval=0.0, normalize=True,
             focus=None, after_release=None):
    """Hold the key, release it after `release_at_audio_s` of audio, report.

    The release is triggered off how much of the script the microphone has
    handed over, not off wall-clock, so the release always lands at the same
    point in the speech however slow the machine is.

    `coalesce` and `preview_interval` default OFF rather than to the shipping
    defaults: the scenarios below them are about WHEN text is inserted, one
    insertion per phrase, and coalescing deliberately changes that timing.
    Their shipping values are exercised in part5_coalescing() and
    part6_preview_pass() instead.

    `focus` stands in for FocusTracker.snapshot; `after_release` runs the
    instant hold_release() returns — the place to move the "focus" elsewhere
    while the decode is still finishing.
    """
    print(f"\n{label}")
    PASTES.clear()
    CHORDS.clear()
    CLIP["text"] = USER_CLIPBOARD
    PRIMARY["text"] = USER_PRIMARY
    before = len(diag_lines())

    # Per-segment insertion cannot run under hold mode (AppConfig.enforce: a
    # paste chord while the key is held reads as its release), so the
    # per-segment scenarios are configured as a toggle session.  The gesture
    # calls below (hold_press / hold_release) do not consult hotkey_mode.
    config = da_config.AppConfig(hotkey_mode="hold" if insert_mode == "end_of_take"
                                 else "toggle",
                                 insert_mode=insert_mode,
                                 preview=preview, coalesce_target_s=coalesce,
                                 preview_interval_s=preview_interval,
                                 normalize=normalize)
    ctl = da_controller.DictationController(config)
    overlay = FakeOverlay(config)
    ctl.set_overlay(overlay)
    if focus is not None:
        ctl.set_focus_probe(focus)

    mic = {}

    def factory(**_kw):
        mic["m"] = FakeMic(audio)
        return mic["m"]

    da_engine.sd.InputStream = factory

    marks = {}
    loop = GLib.MainLoop()
    done = {"quit": False}

    def finish():
        if not done["quit"]:
            done["quit"] = True
            # Leave time for the clipboard restore timer to fire, so a restore
            # that clobbers the user's clipboard is visible to the assertions.
            GLib.timeout_add(1200, lambda: (loop.quit(), False)[1])

    real_capture = ctl._on_capture_start

    def on_capture():
        real_capture()
        marks["capture"] = time.monotonic()
        GLib.timeout_add(50, watch_for_release)

    ctl._engine._on_capture_start = on_capture

    def watch_for_release():
        if mic["m"].delivered < release_at_audio_s:
            return True
        marks["release"] = time.monotonic()
        marks["delivered"] = mic["m"].delivered
        ctl.hold_release()
        if after_release is not None:
            after_release()
        return False

    real_tail = ctl._on_release_tail

    def on_tail():
        marks["tail"] = time.monotonic()
        return real_tail()

    ctl._on_release_tail = on_tail

    real_end = ctl._end_take

    def on_end(outcome, reason):
        marks.setdefault("end", time.monotonic())
        real_end(outcome, reason)
        finish()

    ctl._end_take = on_end

    ctl.hold_press()
    GLib.timeout_add(60000, lambda: (print("  TIMEOUT"), loop.quit(), False)[2])
    loop.run()
    ctl.shutdown()

    lines = diag_lines()[before:]
    pastes = list(PASTES)
    print(f"  released after {marks.get('delivered', 0):.2f} s of audio; "
          f"{len(pastes)} insertion(s)")
    for i, (t, text) in enumerate(pastes, 1):
        rel = t - marks.get("tail", marks.get("release", t))
        print(f"    [{i}] t{rel:+.2f}s vs release tail: {text.strip()[:78]!r}")
    for i, (t, text, segs, _rows) in enumerate(overlay.previews, 1):
        rel = t - marks.get("release", t)
        print(f"    preview[{i}] t{rel:+.2f}s vs release: segments={segs} "
              f"chars={len(text)}")
    for i, (t, hyp, committed, _rows) in enumerate(overlay.hypotheses, 1):
        rel = t - marks.get("capture", t)
        print(f"    hypothesis[{i}] t{rel:+.2f}s vs capture: "
              f"committed={len(committed)} chars, guess={hyp[:54]!r}")
    for line in lines:
        if any(k in line for k in ("event=segment ", "event=segment_discarded",
                                   "event=take_insert", "event=take_end",
                                   "event=release_tail", "event=preview_update")):
            print("    | " + line.split(" ", 1)[1])
    return marks, pastes, lines, overlay


def overlay_window(overlay, t_from, t_to):
    """Pill states the controller asked for between two instants."""
    return [s for t, s, _m in overlay.states if t_from <= t <= t_to]


def overlay_next_after(overlay, t0):
    for t, state, _m in overlay.states:
        if t > t0:
            return state
    return None


# ---------------------------------------------------------------------------

def part1_vad():
    print("\n[1] VAD: a thinking pause is no longer the end of a phrase")
    check("min_silence_duration defaults to 0.8 s",
          da_audio.TenVadDetector.__init__.__defaults__[1] == 0.8,
          str(da_audio.TenVadDetector.__init__.__defaults__[1]))
    check("config default vad_min_silence is 0.8 s",
          da_config.AppConfig().vad_min_silence == 0.8)
    engine = da_engine.ASREngine(da_config.AppConfig(vad_min_silence=1.4),
                          da_models.load_model_profiles()["profiles"]["desktop"],
                          on_text=None, on_partial=None, on_error=None)
    check("config value reaches the detector, not just the dataclass",
          engine._build_vad()._min_silence_samples == int(1.4 * SR),
          str(engine._build_vad()._min_silence_samples))
    engine = da_engine.ASREngine(da_config.AppConfig(vad_min_silence=0.0),
                          da_models.load_model_profiles()["profiles"]["desktop"],
                          on_text=None, on_partial=None, on_error=None)
    check("a nonsense config value is clamped, not obeyed",
          engine._build_vad()._min_silence_samples == int(0.1 * SR))

    def cuts_for(pause, min_silence):
        vad = da_audio.TenVadDetector(threshold=0.5, min_silence_duration=min_silence,
                                min_speech_duration=0.25, max_speech_duration=30.0)
        take = np.concatenate([SPEECH_A, quiet(pause), SPEECH_B])
        cut = 0
        for i in range(0, len(take) - 1600 + 1, 1600):
            vad.accept_waveform(take[i:i + 1600].tolist())
            while not vad.empty():
                cut += 1
                vad.pop()
        return cut, vad

    cut05, vad05 = cuts_for(0.5, 0.8)
    cut12, vad12 = cuts_for(1.2, 0.8)
    cut_old, _ = cuts_for(0.5, 0.25)
    check("0.5 s pause does NOT close a segment at 0.8 s", cut05 == 0,
          f"{cut05} cuts")
    check("1.2 s pause DOES close a segment at 0.8 s", cut12 == 1,
          f"{cut12} cuts")
    check("the old 0.25 s setting fragmented that same 0.5 s pause",
          cut_old >= 1, f"{cut_old} cuts")
    check("everything the 0.5 s pause did not cut is still pending",
          vad05.flush() and len(vad05.front.samples) / SR > 6.0,
          f"{len(vad05.front.samples) / SR:.2f} s pending")

    print("\n[2] VAD flush: the pending buffer is never silently dropped")
    vad = da_audio.TenVadDetector(min_silence_duration=0.8)
    vad.accept_waveform(SPEECH_B[: int(0.6 * SR)].tolist())
    check("flush() reports that it emitted something", vad.flush() is True)
    check("flush() emitted the pending speech", not vad.empty())
    vad = da_audio.TenVadDetector(min_silence_duration=0.8)
    vad.accept_waveform(SPEECH_B[: int(0.2 * SR)].tolist())
    emitted = vad.flush()
    check("flush() emits even below the VAD's own 0.25 s speech floor",
          emitted is True and not vad.empty(),
          "the 0.3 s recognizer floor is the only one allowed to drop it")
    check("and that buffer is what segment_too_short() then judges",
          da_engine.segment_too_short(len(vad.front.samples)) is True,
          f"{len(vad.front.samples)} samples")
    vad = da_audio.TenVadDetector(min_silence_duration=0.8)
    check("flush() with nothing pending is a no-op", vad.flush() is False)


# ---------------------------------------------------------------------------
# Chord choice, newline safety and the transport ladder
# ---------------------------------------------------------------------------

def snap(cls):
    """A FocusSnapshot for a scratch window of class `cls`."""
    return da_focus.FocusSnapshot(cls, cls, "scratch", 4242, time.monotonic())


def part2_insertion():
    print("\n[2b] choose_chord: the chord follows the focused window's class")
    cfg = da_config.AppConfig()
    choose = da_insert.choose_chord
    for cls in ("kitty", "Alacritty", "org.kde.konsole", "konsole", "yakuake",
                "foot", "com.mitchellh.ghostty", "XTerm", "st", "gnome-terminal-server"):
        check(f"terminal {cls} -> Ctrl+Shift+V", choose(cls, cfg) == ("ctrl+shift+v", "focus"),
              str(choose(cls, cfg)))
    check("matching is case-insensitive (KITTY)", choose("KITTY", cfg)[0] == "ctrl+shift+v")
    check("emacs -> Shift+Insert", choose("emacs", cfg) == ("shift+insert", "focus"))
    check("Emacs -> Shift+Insert", choose("Emacs", cfg) == ("shift+insert", "focus"))
    for cls in ("chromium", "com.microsoft.VSCode", "org.kde.kwrite", "python3"):
        check(f"{cls} -> Ctrl+V", choose(cls, cfg) == ("ctrl+v", "focus"), str(choose(cls, cfg)))
    check("unknown (None) -> Ctrl+V, detect=none", choose(None, cfg) == ("ctrl+v", "none"))
    check("unknown ('') -> Ctrl+V, detect=none", choose("", cfg) == ("ctrl+v", "none"))
    over = da_config.AppConfig(paste_overrides={"kitty": "shift+insert",
                                                "Chromium": "ctrl+shift+v"})
    check("an override beats the terminal table",
          choose("kitty", over) == ("shift+insert", "config"), str(choose("kitty", over)))
    check("an override beats the default, case-insensitively",
          choose("chromium", over) == ("ctrl+shift+v", "config"))
    bad = da_config.AppConfig(paste_overrides={"kitty": "xf86paste"})
    check("an override naming a chord that does not exist is ignored",
          choose("kitty", bad) == ("ctrl+shift+v", "focus"))
    compat = da_config.AppConfig(paste_chord="ctrl+shift+v", terminal_paste_chord="shift+insert",
                                 terminal_window_classes=["myterm"])
    check("legacy paste_chord still sets the default for unknown windows",
          choose(None, compat)[0] == "ctrl+shift+v")
    check("legacy terminal_paste_chord + terminal_window_classes still apply",
          choose("MyTerm", compat) == ("shift+insert", "focus"))
    check("a class not in the legacy list gets the default",
          choose("kitty", compat)[0] == "ctrl+shift+v")
    check("the config default terminal list carries every class the research saw",
          {"kitty", "Alacritty", "org.kde.konsole"} <= set(cfg.terminal_window_classes))

    print("\n[2c] newline safety at the staging layer")
    prep = da_insert.prepare_for_target
    check("a trailing newline is stripped everywhere", prep("hello\n", False) == "hello")
    check("a trailing CRLF is stripped everywhere", prep("hello\r\n", False) == "hello")
    check("several trailing newlines are stripped", prep("hello\n\n\r\n", False) == "hello")
    check("an internal newline survives for a non-terminal", prep("a\nb\n", False) == "a\nb")
    check("for a terminal internal newlines collapse to spaces",
          prep("a\nb\r\nc\n", True) == "a b c", repr(prep("a\nb\r\nc\n", True)))
    check("a bare CR is a newline too, for a terminal", prep("a\rb", True) == "a b")
    check("the trailing space the typer adds is kept", prep("x ", True) == "x ")
    check("type_text's own sanitiser still keeps Enter out of every app",
          da_insert.TextTyper._sanitize("a\nb\n") == "a b")

    print("\n[2d] TextTyper: staging on both selections, chord per target, restore")
    PASTES.clear(); CHORDS.clear()
    CLIP["text"] = USER_CLIPBOARD
    PRIMARY["text"] = USER_PRIMARY
    before = len(diag_lines())
    typer = da_insert.TextTyper("clipboard", keep_on_clipboard=False)
    typer.type_text("hello", target=snap("kitty"))
    typer.wait_idle()
    check("the clipboard held the text when the chord went out",
          PASTES and PASTES[-1][1] == "hello ", str(PASTES[-1:]))
    check("and so did the primary selection (what Shift+Insert reads in kitty)",
          PRIMARY["text"] == "hello ", repr(PRIMARY["text"]))
    check("a kitty target got Ctrl+Shift+V", CHORDS[-1:] == ["ctrl+shift+v"], str(CHORDS))
    typer.type_text("hi", target=snap("emacs"))
    typer.wait_idle()
    check("an emacs target got Shift+Insert", CHORDS[-1:] == ["shift+insert"], str(CHORDS))
    typer.type_text("hi", target=snap("org.kde.kwrite"))
    typer.wait_idle()
    check("a kwrite target got Ctrl+V", CHORDS[-1:] == ["ctrl+v"], str(CHORDS))
    typer.type_text("hi", target=None)
    typer.wait_idle()
    check("no target at all got the default Ctrl+V", CHORDS[-1:] == ["ctrl+v"], str(CHORDS))
    typer.type_text("hi", target="Alacritty")
    typer.wait_idle()
    check("a bare class string works as a target too", CHORDS[-1:] == ["ctrl+shift+v"])
    lines = diag_lines()[before:]
    check("paste logged with chord, transport, target and detect",
          any("event=paste " in l and "chord=ctrl+shift+v" in l and "transport=ydotool" in l
              and "target=kitty" in l and "detect=focus" in l and "ok=1" in l for l in lines),
          str([l for l in lines if "event=paste " in l][:1]))
    check("an unknown target is logged as such",
          any("event=paste " in l and "target=unknown" in l and "detect=none" in l
              for l in lines))
    time.sleep(0.8)   # outlive the restore timers
    check("the user's clipboard was put back", CLIP["text"] == USER_CLIPBOARD,
          repr(CLIP["text"]))
    check("the user's primary selection was put back", PRIMARY["text"] == USER_PRIMARY,
          repr(PRIMARY["text"]))

    print("\n[2e] the transport ladder: retry with Shift+Insert, then fail loudly")
    PASTES.clear(); CHORDS.clear()
    CLIP["text"] = USER_CLIPBOARD
    failures = []
    typer = da_insert.TextTyper("clipboard", on_failure=failures.append)
    YDOTOOL_FAIL["n"] = 1                    # the first chord press errors out
    before = len(diag_lines())
    typer.type_text("retry me", target=snap("chromium"))
    typer.wait_idle()
    lines = diag_lines()[before:]
    check("after a transport failure the retry is Shift+Insert",
          CHORDS == ["shift+insert"], str(CHORDS))
    check("both attempts logged, the first not ok, the second ok",
          any("event=paste " in l and "attempt=1" in l and "ok=0" in l and "chord=ctrl+v" in l
              for l in lines)
          and any("event=paste " in l and "attempt=2" in l and "ok=1" in l
                  and "chord=shift+insert" in l for l in lines),
          str([l.split("event=")[1] for l in lines if "event=paste" in l]))
    check("no failure surfaced: the retry delivered it", not failures, str(failures))
    time.sleep(0.8)
    check("clipboard restored after the successful retry", CLIP["text"] == USER_CLIPBOARD)

    YDOTOOL_FAIL["n"] = 2                    # the chord AND the retry error out
    before = len(diag_lines())
    typer.type_text("stranded", target=snap("chromium"))
    typer.wait_idle()
    lines = diag_lines()[before:]
    check("paste_failed logged when the retry fails too",
          any("event=paste_failed" in l and "target=chromium" in l for l in lines))
    check("the failure is surfaced with the recovery hint",
          failures and "clipboard" in failures[-1].lower() and "Ctrl+V" in failures[-1],
          str(failures))
    time.sleep(0.8)
    check("the text is LEFT on the clipboard — nothing restored over it",
          CLIP["text"] == "stranded ", repr(CLIP["text"]))
    check("and the restore bookkeeping is released for the next take",
          typer._clip_restore_pending is False and typer._clip_saved is None)
    YDOTOOL_FAIL["n"] = 0
    PASTES.clear(); CHORDS.clear()
    CLIP["text"] = USER_CLIPBOARD
    typer.type_text("next take", target=snap("chromium"))
    typer.wait_idle()
    check("the next insertion saves the user's clipboard afresh",
          PASTES and PASTES[-1][1] == "next take ")
    time.sleep(0.8)
    check("and restores it", CLIP["text"] == USER_CLIPBOARD, repr(CLIP["text"]))


# ---------------------------------------------------------------------------
# The floating transcript preview
# ---------------------------------------------------------------------------

def preview_unit_checks():
    """TranscriptPreview on its own: the text model and the hard invariant."""
    print("\n[8] transcript preview: text model, wrapping, and no insertion")
    panel = da_overlay.TranscriptPreview(da_config.AppConfig(), clearance=84)

    check("starts empty", panel.chars == 0 and panel.segments == 0
          and panel.rows == [])
    check("append() of nothing is a no-op", panel.append("   ") is False
          and panel.segments == 0)

    fed = ["Open the settings and check whether the microphone works.",
           "Then hold the key down and keep talking for a while,",
           "pausing to think in the middle without losing the sentence,",
           "and let it go only when the whole thought is finished."]
    snapshots = []
    for chunk in fed:
        panel.append(chunk)
        snapshots.append(panel.text)
    check("one segment counted per append", panel.segments == len(fed),
          str(panel.segments))
    check("chars is the length of what it holds", panel.chars == len(panel.text))
    check("append-only: every snapshot extends the previous one, never rewrites it",
          all(later.startswith(earlier)
              for earlier, later in zip(snapshots, snapshots[1:])))
    check("nothing was dropped from the accumulated text",
          all(chunk in panel.text for chunk in fed))

    rows = panel.rows
    check(f"shows at most {da_overlay.TranscriptPreview.ROWS_MAX} rows "
          f"({da_overlay.TranscriptPreview.ROWS_FULL} legible + "
          f"{da_overlay.TranscriptPreview.ROWS_FADE} fading)",
          0 < len(rows) <= da_overlay.TranscriptPreview.ROWS_MAX, f"{len(rows)} rows")
    check("the rows are the TAIL of the text, not its head",
          panel.text.endswith(rows[-1]), rows[-1][-40:])
    words = panel.text.split()
    row_words = " ".join(rows).split()
    check("no mid-word truncation: every word on screen is a whole word of the text",
          all(w in words for w in row_words),
          str([w for w in row_words if w not in words]))
    check("the rows are a contiguous run of the text's words",
          " ".join(row_words) in " ".join(words))
    cr = panel._measure_cr()
    over = [r for r in rows if cr.text_extents(r)[4] > panel._text_width() + 0.5]
    check("every row fits the panel's text column", not over,
          str([(r[:20], round(cr.text_extents(r)[4])) for r in over]))
    check("the panel is wider than the pill but fits the screen",
          da_overlay.PillOverlay.WIDTH < panel.panel_width() <= da_overlay.TranscriptPreview.MAX_WIDTH,
          f"{panel.panel_width()} px")
    check("height grows with the rows and stays two-ish lines tall",
          panel.panel_height() == (da_overlay.TranscriptPreview.PAD_TOP
                                   + len(rows) * da_overlay.TranscriptPreview.LINE_H
                                   + da_overlay.TranscriptPreview.PAD_BOTTOM),
          f"{panel.panel_height()} px for {len(rows)} rows")

    single = da_overlay.TranscriptPreview(da_config.AppConfig(), clearance=84)
    single.append("short")
    check("one short segment is one row, so the panel starts small",
          len(single.rows) == 1 and single.panel_height() < panel.panel_height(),
          f"{single.panel_height()} px vs {panel.panel_height()} px")

    long_word = "x" * 400
    wide = da_overlay.TranscriptPreview(da_config.AppConfig(), clearance=84)
    wide.append(f"before {long_word} after")
    check("a word longer than a line is kept whole on a row of its own, not chopped",
          long_word in wide.rows, str([len(r) for r in wide.rows]))

    huge = da_overlay.TranscriptPreview(da_config.AppConfig(), clearance=84)
    for i in range(400):
        huge.append(f"segment number {i} of a very long take")
    check("a very long take still lays out only a few rows",
          len(huge.rows) <= da_overlay.TranscriptPreview.ROWS_MAX, f"{len(huge.rows)} rows")
    check("and the last row is still the end of the text",
          huge.text.endswith(huge.rows[-1]))
    check("no row starts with a half word after the tail cut",
          all(r.split()[0] in huge.text.split() for r in huge.rows))

    check("reset() empties it", (huge.reset(), huge.chars == 0
                                and huge.segments == 0 and huge.rows == [])[1])

    # --- the hard invariant, behaviourally ---------------------------------
    PASTES.clear()
    CLIP["text"] = USER_CLIPBOARD
    drum = da_overlay.TranscriptPreview(da_config.AppConfig(), clearance=84)
    for chunk in fed * 3:
        drum.append(chunk)
    time.sleep(0.3)
    check("driving the panel inserts nothing: no paste, no clipboard write",
          not PASTES and CLIP["text"] == USER_CLIPBOARD,
          f"{len(PASTES)} pastes, clipboard={CLIP['text'][:30]!r}")

    # --- the hard invariant, structurally ---------------------------------
    # Not "we did not call the typer" but "there is nothing here to call it
    # with": no insertion binary, no clipboard, no typer, no subprocess.
    src = inspect.getsource(da_overlay.TranscriptPreview)
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    banned_names = {"subprocess", "TextTyper", "Gtk_Clipboard"}
    banned_attrs = {"type_text", "type_partial", "commit_partial", "run",
                    "Popen", "call", "check_output"}
    banned_strings = {"wl-copy", "wl-paste", "ydotool", "wtype", "xdotool"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in banned_names:
            bad.append(f"name {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
            bad.append(f"attribute .{node.attr}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings and node.value in banned_strings:
            bad.append(f"string {node.value!r}")
    check("no insertion machinery is even reachable from TranscriptPreview",
          not bad, "; ".join(sorted(set(bad))))
    check("and it holds no typer attribute", not hasattr(panel, "_typer"))


def preview_take_checks():
    """The user's scenario: three segments decoded while the key is held."""
    take = np.concatenate([SPEECH_A, quiet(1.2), SPEECH_B, quiet(1.2), SPEECH_C])
    release = (len(SPEECH_A) + len(SPEECH_B)) / SR + 2.4 + 1.9
    marks, pastes, lines, overlay = run_take(
        take, release, "end_of_take",
        "[7] preview on, three segments, insert_mode=end_of_take: the panel "
        "grows while the document stays untouched")

    before_end = [t for t, _ in pastes if t < marks["tail"]]
    check("ZERO insertions before the take ends", not before_end,
          f"{len(before_end)} insertion(s) while the key was down")
    check("exactly one insertion for the whole take", len(pastes) == 1,
          f"{len(pastes)} insertions")
    check("and it came after the release tail fired",
          bool(pastes) and pastes[0][0] > marks["tail"])
    inserted = pastes[0][1].lower() if pastes else ""
    check("the one insertion carries segment 1", carries(inserted, MARK_A))
    check("the one insertion carries segment 2", carries(inserted, MARK_B))
    check("the one insertion carries segment 3 (pending at release)",
          carries(inserted, MARK_C), inserted[-60:])

    during = [p for p in overlay.previews if p[0] < marks["release"]]
    check("the preview was fed at least twice WHILE the key was held",
          len(during) >= 2, f"{len(during)} update(s) before the release")
    check("the panel had text on screen before anything was inserted",
          bool(during) and (not pastes or during[0][0] < pastes[0][0]))
    check("the preview reset at the start of the take", overlay.preview_resets >= 1,
          str(overlay.preview_resets))
    check("segment count climbs one per update",
          [segs for _t, _txt, segs, _r in overlay.previews]
          == list(range(1, len(overlay.previews) + 1)),
          str([segs for _t, _txt, segs, _r in overlay.previews]))
    texts = [txt for _t, txt, _s, _r in overlay.previews]
    check("append-only across the take: no shown word is ever rewritten",
          all(b.startswith(a) for a, b in zip(texts, texts[1:])),
          str([len(t) for t in texts]))
    check("every segment that reached the preview also reached the insertion",
          all(carries(inserted, m) for m in (MARK_A, MARK_B, MARK_C)))
    shown = da_insert._WS_RE.sub(" ", texts[-1]).strip() if texts else ""
    check("what the panel finally showed IS what was inserted",
          bool(pastes) and shown == pastes[0][1].strip(),
          f"panel={shown[:40]!r} inserted={pastes[0][1][:40]!r}" if pastes else "")
    rows = overlay.previews[-1][3] if overlay.previews else ()
    check("the panel stayed at a few bottom-anchored rows",
          0 < len(rows) <= da_overlay.TranscriptPreview.ROWS_MAX, f"{len(rows)} rows")
    check("the visible rows are the tail of what was inserted",
          bool(rows) and shown.endswith(rows[-1]), rows[-1][-40:] if rows else "")

    upd = [l for l in lines if "event=preview_update" in l]
    check("preview_update logged once per segment, not per redraw",
          len(upd) == len(overlay.previews) == 3,
          f"{len(upd)} log lines for {len(overlay.previews)} updates")
    check("each preview_update carries chars= and segments=",
          all("chars=" in l and "segments=" in l for l in upd))
    check("no take_insert is logged before the last preview_update",
          bool(upd) and all(lines.index(u) < lines.index(i)
                            for u in upd for i in lines
                            if "event=take_insert" in i))
    check("the pill went to success after the insertion, taking the panel with it",
          bool(pastes) and overlay_next_after(overlay, pastes[-1][0]) == "success",
          str([s for _t, s, _m in overlay.states]))

    # Preview off: the controller still runs the take, the panel stays empty.
    marks, pastes, lines, overlay = run_take(
        take, release, "end_of_take",
        "[7b] the same take with preview disabled", preview=False)
    check("with preview=False nothing is previewed at all",
          not overlay.previews
          and not [l for l in lines if "event=preview_update" in l],
          f"{len(overlay.previews)} updates")
    check("with preview=False the take still produces its one insertion",
          len(pastes) == 1, f"{len(pastes)} insertions")
    check("and it still carries all three segments",
          all(carries(pastes[0][1], m) for m in (MARK_A, MARK_B, MARK_C))
          if pastes else False)


# ---------------------------------------------------------------------------
# Coalescing and loudness normalisation
# ---------------------------------------------------------------------------

def vad_segments(audio, min_silence=0.8):
    """Run the real detector over `audio` and return the segments it closed."""
    vad = da_audio.TenVadDetector(min_silence_duration=min_silence)
    out = []
    for i in range(0, len(audio) - 1600 + 1, 1600):
        vad.accept_waveform(audio[i:i + 1600].tolist())
        while not vad.empty():
            out.append(vad.front)
            vad.pop()
    return out, vad


def contiguous_offset(haystack, needle):
    """Index where `needle` occurs verbatim in `haystack`, or None."""
    if len(needle) > len(haystack):
        return None
    starts = np.flatnonzero(haystack[: len(haystack) - len(needle) + 1] == needle[0])
    for i in starts:
        if np.array_equal(haystack[i:i + len(needle)], needle):
            return int(i)
    return None


def part5_coalescing():
    print("\n[9] coalescing: short phrases reach the model as ONE block")
    check("coalesce_target_s defaults to 4.0 s",
          da_config.AppConfig().coalesce_target_s == 4.0,
          str(da_config.AppConfig().coalesce_target_s))

    # Four different phrases, each well under 4 s, separated by 0.9 s of room
    # noise: long enough for the VAD to close every one of them, short enough
    # that none reaches the target on its own.  Different phrases on purpose —
    # a repeating take would make the contiguity check below pass by accident.
    parts = [SPEECH_A[: int(1.6 * SR)], SPEECH_B[: int(1.6 * SR)],
             SPEECH_C[: int(1.6 * SR)], SPEECH_A[int(1.0 * SR): int(2.6 * SR)]]
    pieces = []
    for phrase in parts:
        pieces += [phrase, quiet(0.9)]
    take = np.concatenate(pieces + [quiet(0.6)])

    segs, _vad = vad_segments(take)
    durs = [len(s.samples) / SR for s in segs]
    print(f"  VAD closed {len(segs)} segments: "
          + ", ".join(f"{d:.2f}s" for d in durs))
    check("the VAD still closes one segment per phrase", len(segs) == 4,
          f"{len(segs)} segments")
    check("and every one of them is under the 4 s target on its own",
          all(d < 4.0 for d in durs), str([round(d, 2) for d in durs]))

    coalescer = da_audio._BlockCoalescer(4.0)
    blocks = []
    for seg in segs:
        blocks.extend(coalescer.add(seg))
    blocks.extend(coalescer.flush())
    print("  coalesced into "
          + ", ".join(f"{len(b) / SR:.2f}s/{n} segments" for b, n in blocks))
    check("two phrases are merged into one block once they pass 4 s",
          blocks and blocks[0][1] == 2 and len(blocks[0][0]) / SR >= 4.0,
          f"first block {len(blocks[0][0]) / SR:.2f}s from "
          f"{blocks[0][1]} segments" if blocks else "no blocks")
    check("four phrases become two blocks, not four", len(blocks) == 2,
          f"{len(blocks)} blocks")
    first = blocks[0][0]
    check("the block is longer than the phrases in it — the pause is kept, "
          "not spliced out",
          len(first) > len(segs[0].samples) + len(segs[1].samples),
          f"{len(first)} vs {len(segs[0].samples) + len(segs[1].samples)} samples")
    offset = contiguous_offset(take, first)
    check("the block is a CONTIGUOUS span of the take, gaps and all",
          offset is not None, f"offset={offset}")

    # target 0 restores one decode per VAD segment
    off = da_audio._BlockCoalescer(0.0)
    raw = []
    for seg in segs:
        raw.extend(off.add(seg))
    raw.extend(off.flush())
    check("coalesce_target_s=0 restores one block per segment",
          len(raw) == len(segs) and all(n == 1 for _b, n in raw),
          f"{len(raw)} blocks")

    # A pause too long to be context breaks the block instead of being carried.
    long_gap = np.concatenate([SPEECH_A[: int(1.6 * SR)],
                               quiet(da_audio.COALESCE_MAX_GAP_SAMPLES / SR + 1.5),
                               SPEECH_C[: int(1.6 * SR)], quiet(1.2)])
    gap_segs, _ = vad_segments(long_gap)
    gap_c = da_audio._BlockCoalescer(4.0)
    gap_blocks = []
    for seg in gap_segs:
        gap_blocks.extend(gap_c.add(seg))
    gap_blocks.extend(gap_c.flush())
    check("a pause longer than the carry cap breaks the block instead of "
          "padding it with dead air",
          len(gap_segs) >= 2 and len(gap_blocks) == len(gap_segs)
          and all(n == 1 for _b, n in gap_blocks),
          f"{len(gap_segs)} segments -> {len(gap_blocks)} blocks")


def part5b_normalize():
    print("\n[10] normalisation: bounded gain, model input only")
    speech = SPEECH_B[: int(2.0 * SR)].copy()

    def at_dbfs(audio, dbfs):
        rms = float(np.sqrt(np.mean(audio ** 2)))
        return (audio * (10 ** (dbfs / 20.0) / rms)).astype(np.float32)

    def dbfs(audio):
        rms = float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float32) ** 2)))
        return 20 * np.log10(rms) if rms > 0 else -120.0

    loud = at_dbfs(speech, -13.0)
    quiet_speech = at_dbfs(speech, -40.0)
    silence = np.zeros(2 * SR, dtype=np.float32)
    noise = at_dbfs(np.resize(ROOM, 2 * SR).astype(np.float32), -70.0)

    out_loud, i_loud = da_audio.normalize_for_model(loud, -18.0)
    out_quiet, i_quiet = da_audio.normalize_for_model(quiet_speech, -18.0)
    out_sil, i_sil = da_audio.normalize_for_model(silence, -18.0)
    out_noise, i_noise = da_audio.normalize_for_model(noise, -18.0)
    for tag, info in (("loud -13", i_loud), ("quiet -40", i_quiet),
                      ("silence", i_sil), ("noise -70", i_noise)):
        print(f"  {tag:10} gain_db={info['gain_db']:+6.1f} "
              f"rms_in_db={info['rms_in_db']:7.1f} "
              f"rms_out_db={info['rms_out_db']:7.1f} clipped={info['clipped']}")

    check("a loud block is NOT amplified", i_loud["gain_db"] <= 0.0,
          f"{i_loud['gain_db']:+.1f} dB")
    check("and it is passed through unchanged (boost only, never attenuate)",
          i_loud["gain_db"] == 0.0 and abs(dbfs(out_loud) - dbfs(loud)) < 0.01,
          f"{dbfs(out_loud):.1f} dBFS in, gain {i_loud['gain_db']:+.1f} dB")
    check("a quiet-but-real block IS boosted", i_quiet["gain_db"] > 10.0,
          f"{i_quiet['gain_db']:+.1f} dB")
    check(f"never by more than {da_audio.NORMALIZE_MAX_GAIN_DB:.0f} dB",
          i_quiet["gain_db"] <= da_audio.NORMALIZE_MAX_GAIN_DB + 0.01,
          f"{i_quiet['gain_db']:+.1f} dB")
    check("and the peak stays under the ceiling, so nothing clips",
          float(np.max(np.abs(out_quiet))) <= 10 ** (da_audio.NORMALIZE_PEAK_DBFS / 20.0)
          + 1e-6,
          f"peak {20 * np.log10(float(np.max(np.abs(out_quiet)))):.2f} dBFS")
    check("digital silence is not blown up",
          i_sil["gain_db"] == 0.0 and float(np.max(np.abs(out_sil))) == 0.0)
    check("a -70 dBFS noise floor is left alone, not lifted into speech",
          i_noise["gain_db"] == 0.0
          and abs(dbfs(out_noise) - dbfs(noise)) < 0.01,
          f"{i_noise['gain_db']:+.1f} dB, {dbfs(out_noise):.1f} dBFS")

    # MODEL ONLY: the captured samples the caller holds are never touched.
    before = quiet_speech.copy()
    da_audio.normalize_for_model(quiet_speech, -18.0)
    check("the gain lands on the copy handed to the model, never on the "
          "captured samples", np.array_equal(before, quiet_speech))

    prepared, info = da_engine.ASREngine._prepare_audio(quiet_speech, True, -18.0)
    check("the decode path applies it and keeps the TDT's trailing pad",
          len(prepared) == len(quiet_speech) + da_engine.TAIL_PAD_SAMPLES
          and info["gain_db"] > 10.0,
          f"{len(prepared)} samples, {info['gain_db']:+.1f} dB")
    bare, none_info = da_engine.ASREngine._prepare_audio(quiet_speech, False, -18.0)
    check("normalize=False hands the model exactly what was captured",
          none_info is None
          and np.array_equal(bare[: len(quiet_speech)], quiet_speech))


def part5c_take():
    """The whole thing end to end: two phrases in one block, a flushed tail."""
    take = np.concatenate([SPEECH_A, quiet(0.9), SPEECH_B, quiet(0.9), SPEECH_C])
    release = (len(SPEECH_A) + len(SPEECH_B)) / SR + 1.8 + 1.9
    marks, pastes, lines, overlay = run_take(
        take, release, "end_of_take",
        "[11] hold take at the shipping defaults: two phrases coalesced into "
        "one block, the third flushed at release", coalesce=4.0)

    blocks = [l for l in lines if "event=segment " in l]
    counts = [int(l.split("segments=")[1].split()[0]) for l in blocks]
    print(f"  blocks submitted: {counts}")
    check("the two phrases before the release went as ONE block",
          2 in counts, str(counts))
    check("two blocks for three phrases, not three", len(blocks) == 2,
          f"{len(blocks)} blocks")
    check("the merged block was cut on silence",
          any("segments=2" in l and "reason=silence" in l for l in blocks))
    check("the phrase still open at release was flushed, not dropped",
          any("reason=flush" in l for l in blocks))
    merged = [l for l in blocks if "segments=2" in l]
    dur = float(merged[0].split("dur_ms=")[1].split()[0]) / 1000 if merged else 0
    # Both phrases, the VAD's own 0.8 s closing silence and whatever of the
    # 0.9 s pause the detector did not already count as the start of the second
    # phrase (room noise crosses the threshold now and then, so the second
    # segment usually opens a little early and the carried gap is short).
    floor = (len(SPEECH_A) + len(SPEECH_B)) / SR + 0.8
    ceil = floor + 0.9
    check("its duration is the contiguous span over both phrases, the closing "
          "silence and the pause between them", floor - 0.05 <= dur <= ceil,
          f"{dur:.2f}s, expected {floor:.2f}-{ceil:.2f}s")
    check("every block was normalised before decoding",
          len([l for l in lines if "event=normalize" in l]) == len(blocks),
          f"{len([l for l in lines if 'event=normalize' in l])} normalize lines")
    check("and the gain is logged with its in/out levels",
          all("gain_db=" in l and "rms_in_db=" in l and "rms_out_db=" in l
              and "clipped=" in l
              for l in lines if "event=normalize" in l))

    check("still exactly one insertion for the take", len(pastes) == 1,
          f"{len(pastes)} insertions")
    inserted = pastes[0][1].lower() if pastes else ""
    check("the merged block carries both phrases",
          carries(inserted, MARK_A) and carries(inserted, MARK_B),
          inserted[:70])
    check("the flushed tail is in there too", carries(inserted, MARK_C),
          inserted[-60:])
    check("the panel showed the whole take, one update per block",
          overlay.previews and overlay.previews[-1][2] == len(blocks)
          and all(carries(overlay.previews[-1][1], m)
                  for m in (MARK_A, MARK_B, MARK_C)),
          f"{len(overlay.previews)} updates")
    shown = da_insert._WS_RE.sub(" ", overlay.previews[-1][1]).strip() if overlay.previews else ""
    check("what the panel finally showed IS what was inserted",
          bool(pastes) and shown == pastes[0][1].strip())


# ---------------------------------------------------------------------------
# The live preview pass
# ---------------------------------------------------------------------------

def part6_preview_pass():
    """Text on the panel while a phrase is still being spoken."""
    # s90.wav runs 12 s and its first VAD segment does not close until t=5 s:
    # before this pass existed the panel stayed empty for those five seconds.
    take = load_wav(BENCH / "staging" / "s90.wav")
    release = 10.5
    cpu0 = time.process_time()
    marks, pastes, lines, overlay = run_take(
        take, release, "end_of_take",
        "[12] preview pass: a running hypothesis while the phrase is still open",
        coalesce=4.0, preview_interval=1.0)
    cpu_on = time.process_time() - cpu0

    hyps = overlay.hypotheses
    first_commit = overlay.previews[0][0] if overlay.previews else marks["release"]
    early = [h for h in hyps if h[0] < first_commit]
    print(f"  {len(hyps)} hypotheses, {len(early)} of them before the first "
          f"committed block")
    check("something was on the panel before any phrase closed",
          len(early) >= 1, f"{len(early)} early hypotheses")
    check("the first one arrived within 3 s of capture opening",
          bool(hyps) and hyps[0][0] - marks["capture"] < 3.0,
          f"{(hyps[0][0] - marks['capture']):.2f} s" if hyps else "none")
    check("it said something, it was not an empty panel",
          any(h[1] for h in early), str([h[1][:30] for h in early]))
    check("committed text was still empty while those were shown",
          all(h[2] == "" for h in early),
          str([h[2][:30] for h in early]))

    committed = [h[2] for h in hyps]
    final = overlay.panel.text
    check("committed text is only ever extended, never rewritten under a "
          "hypothesis", all(final.startswith(c) for c in committed),
          str([len(c) for c in committed]))
    check("a hypothesis never counts as a decoded segment",
          overlay.panel.segments == len(overlay.previews),
          f"{overlay.panel.segments} vs {len(overlay.previews)} blocks")
    check("the hypothesis is gone by the end of the take",
          overlay.panel.hypothesis == "", repr(overlay.panel.hypothesis[:40]))

    # The hard invariant: none of this reaches the document.
    check("ZERO insertions while the key was held",
          not [t for t, _ in pastes if t < marks["tail"]])
    check("exactly one insertion for the take", len(pastes) == 1,
          f"{len(pastes)} insertions")
    check("and it is exactly the committed text — no hypothesis leaked into it",
          bool(pastes) and pastes[0][1].strip() == final.strip(),
          f"inserted={pastes[0][1][:50]!r} committed={final[:50]!r}"
          if pastes else "")
    blocks = [l for l in lines if "event=segment " in l]
    words_committed = sum(len(t.split()) for _t, t, _s, _r in overlay.previews[-1:])
    check("the inserted text is the blocks' text, nothing doubled",
          bool(pastes) and len(pastes[0][1].split()) == words_committed,
          f"{len(pastes[0][1].split())} words vs {words_committed}"
          if pastes else "")

    passes = [l for l in lines if "event=preview_pass" in l]
    ran = [l for l in passes if "skipped=0" in l]
    skipped = [l for l in passes if "skipped=1" in l]
    print(f"  preview passes: {len(ran)} ran, {len(skipped)} skipped for the "
          f"decoder")
    check("preview_pass logged with its duration and decode time",
          bool(ran) and all("dur_ms=" in l and "decode_ms=" in l for l in ran))
    window = da_config.AppConfig().preview_window_s
    over = [l for l in ran
            if float(l.split("dur_ms=")[1].split()[0]) > window * 1000 + 1]
    check(f"no pass decodes more than the {window:.0f} s window", not over,
          str([l.split("dur_ms=")[1].split()[0] for l in over]))
    held = release + 1.0
    check("single flight: no more passes than the cadence allows",
          len(passes) <= held / 1.0 + 2, f"{len(passes)} passes in ~{held:.0f} s")

    # Cost: the same take with the pass off.
    cpu0 = time.process_time()
    marks2, pastes2, lines2, overlay2 = run_take(
        take, release, "end_of_take",
        "[12b] the same take with the preview pass off (cost baseline)",
        coalesce=4.0, preview_interval=0.0)
    cpu_off = time.process_time() - cpu0

    def flush_decode_ms(ls):
        got = [float(l.split("decode_ms=")[1].split()[0]) for l in ls
               if "event=segment " in l and "reason=flush" in l]
        return got[-1] if got else 0.0

    print(f"  CPU per take: {cpu_on:.1f} s with the pass, {cpu_off:.1f} s "
          f"without (+{cpu_on - cpu_off:.1f} s over {release:.0f} s of audio)")
    print(f"  final block decode: {flush_decode_ms(lines):.0f} ms with the "
          f"pass, {flush_decode_ms(lines2):.0f} ms without")
    check("the pass does not change what the take inserts",
          len(pastes2) == 1 and bool(pastes)
          and pastes2[0][1].strip() == pastes[0][1].strip(),
          f"{pastes2[0][1][:40]!r}" if pastes2 else "no insertion")
    check("with the pass off nothing is previewed as a hypothesis",
          not overlay2.hypotheses
          and not [l for l in lines2 if "event=preview_pass" in l])


def part3_clipboard():
    print("\n[6] clipboard save/restore across back-to-back insertions")
    PASTES.clear()
    CLIP["text"] = USER_CLIPBOARD
    typer = da_insert.TextTyper("clipboard", keep_on_clipboard=False)
    typer.type_text("first phrase")
    typer.type_text("second phrase")
    typer.type_text("third phrase")
    typer.wait_idle()      # the pastes are queued; let them go out
    delivered = [text.strip() for _t, text in PASTES]
    check("every insertion pasted its own text, none pasted a stale value",
          delivered == ["first phrase", "second phrase", "third phrase"],
          str(delivered))
    time.sleep(1.5)   # outlive all three restore timers
    check("the user's clipboard is what is left behind",
          CLIP["text"] == USER_CLIPBOARD, repr(CLIP["text"][:50]))
    check("no dictated text left on the clipboard",
          "phrase" not in CLIP["text"], repr(CLIP["text"][:50]))
    check("the restore bookkeeping is released, not leaked",
          typer._clip_restore_pending is False and typer._clip_saved is None)


def main():
    # Load the model up front so the first take is not also a model load.
    warm = da_engine.ASREngine(da_config.AppConfig(),
                        da_models.load_model_profiles()["profiles"]["desktop"],
                        on_text=None, on_partial=None, on_error=None)
    t0 = time.perf_counter()
    warm._acquire_offline_recognizer()
    print(f"model ready in {(time.perf_counter() - t0) * 1000:.0f} ms")

    part1_vad()
    part2_insertion()

    # ---- the user's scenario, per_segment (the pre-hold-mode default) -------
    take1 = np.concatenate([SPEECH_A, quiet(0.5), SPEECH_B, SPEECH_C])
    release1 = (len(SPEECH_A) + len(SPEECH_B)) / SR + 0.5 + 2.0
    marks, pastes, lines, overlay = run_take(
        take1, release1, "per_segment",
        "[3] hold take: speech, 0.5 s pause, speech, release mid-speech "
        "(insert_mode=per_segment)")

    before_tail = [t for t, _ in pastes if t < marks["tail"]]
    check("nothing was inserted during the 0.5 s pause", not before_tail,
          f"{len(before_tail)} insertion(s) before the release tail fired")
    check("exactly one insertion for the take", len(pastes) == 1,
          f"{len(pastes)} insertions")
    joined = " ".join(text for _t, text in pastes).lower()
    check("it carries the speech from BEFORE the pause", carries(joined, MARK_A))
    check("it carries the speech from AFTER the pause", carries(joined, MARK_B))
    check("it carries the audio still pending at release", carries(joined, MARK_C),
          joined[-60:])
    check("the insertion came after the release tail fired",
          bool(pastes) and pastes[0][0] > marks["tail"],
          f"{(pastes[0][0] - marks['tail']) * 1000:.0f} ms after" if pastes else "")
    check("the pending audio was closed by the flush, not by silence",
          any("event=segment " in l and "reason=flush" in l for l in lines))
    check("no segment was cut on silence during the hold",
          not any("event=segment " in l and "reason=silence" in l for l in lines))
    check("take_insert logged with its index, size and wait",
          any("event=take_insert" in l and "index=1" in l and "chars=" in l
              and "segments=" in l and "waited_ms=" in l for l in lines))
    held = overlay_window(overlay, marks["release"], pastes[-1][0]) if pastes else []
    check("the pill stayed on processing from release until the insertion",
          held == ["processing"], str(held))
    check("and flipped to success only once the text had landed",
          bool(pastes) and overlay_next_after(overlay, pastes[-1][0]) == "success",
          str([s for _t, s, _m in overlay.states]))
    time.sleep(1.2)
    check("the user's clipboard survived the take", CLIP["text"] == USER_CLIPBOARD,
          repr(CLIP["text"][:50]))

    # ---- per_segment really does insert while the key is down -------------
    # C, not B, after the pause: cutting B mid-word makes Parakeet return an
    # empty result (B[0:2.8 s] decodes to ""), which would make this test about
    # the model rather than about the insertion path.
    take2 = np.concatenate([SPEECH_A, quiet(1.2), SPEECH_C])
    release2 = len(SPEECH_A) / SR + 1.2 + 1.9
    marks, pastes, lines, overlay = run_take(
        take2, release2, "per_segment",
        "[4] hold take: speech, 1.2 s pause, speech, release mid-speech "
        "(insert_mode=per_segment)")

    check("two insertions: the cut phrase and the flushed tail",
          len(pastes) == 2, f"{len(pastes)} insertions")
    check("the first landed while the key was still down",
          bool(pastes) and pastes[0][0] < marks["release"])
    check("the second landed after the release tail",
          len(pastes) > 1 and pastes[1][0] > marks["tail"])
    check("the first insertion is the pre-pause phrase",
          bool(pastes) and carries(pastes[0][1], MARK_A),
          pastes[0][1][:50] if pastes else "")
    check("the second insertion is the speech pending at release",
          len(pastes) > 1 and carries(pastes[1][1], MARK_C),
          pastes[1][1][:50] if len(pastes) > 1 else "")
    check("the 1.2 s pause cut a segment on silence",
          any("event=segment " in l and "reason=silence" in l for l in lines))
    check("the release flushed one too",
          any("event=segment " in l and "reason=flush" in l for l in lines))
    check("each insertion logged with its index within the take",
          sorted(l.split("index=")[1].split()[0] for l in lines
                 if "event=take_insert" in l) == ["1", "2"])
    time.sleep(1.2)
    check("the user's clipboard survived two insertions",
          CLIP["text"] == USER_CLIPBOARD, repr(CLIP["text"][:50]))

    # ---- the same take held back to a single insertion --------------------
    marks, pastes, lines, overlay = run_take(
        take2, release2, "end_of_take",
        "[5] the same take with insert_mode=end_of_take")

    check("nothing inserted while the key was held",
          not [t for t, _ in pastes if t < marks["tail"]])
    check("exactly one insertion for the whole take", len(pastes) == 1,
          f"{len(pastes)} insertions")
    check("it came after the release tail fired",
          bool(pastes) and pastes[0][0] > marks["tail"])
    text = pastes[0][1].lower() if pastes else ""
    check("both phrases are in that one insertion",
          carries(text, MARK_A) and carries(text, MARK_C), text[:90])
    check("joined on a single space, no double spacing",
          bool(pastes) and "  " not in pastes[0][1])
    check("logged as one end_of_take insertion over two segments",
          any("event=take_insert" in l and "mode=end_of_take" in l
              and "segments=2" in l and "index=1" in l for l in lines))
    held = overlay_window(overlay, marks["release"], pastes[-1][0]) if pastes else []
    check("the pill stayed on processing from release until the insertion",
          held == ["processing"], str(held))
    check("and flipped to success only once the text had landed",
          bool(pastes) and overlay_next_after(overlay, pastes[-1][0]) == "success",
          str([s for _t, s, _m in overlay.states]))
    check("with no focus probe the chord is the configured default",
          list(CHORDS) == ["ctrl+v"], str(CHORDS))
    check("and the insertion is logged against an unknown target",
          any("event=take_insert" in l and "target=unknown" in l for l in lines))

    # ---- the paste target is fixed at the RELEASE, not at the insertion ---
    focus = {"now": snap("kitty")}

    def alt_tab_away():
        # The user lets go over a terminal and is already in an editor by the
        # time the decode of the flushed tail lands.
        focus["now"] = snap("org.kde.kwrite")

    marks, pastes, lines, overlay = run_take(
        take2, release2, "end_of_take",
        "[5b] focus at release = kitty, focus at insertion = kwrite",
        focus=lambda: focus["now"], after_release=alt_tab_away)
    check("exactly one insertion", len(pastes) == 1, f"{len(pastes)}")
    check("it landed after the release, when the probe already said kwrite",
          bool(pastes) and pastes[0][0] > marks["release"]
          and focus["now"].resource_class == "org.kde.kwrite")
    check("the chord is the TERMINAL's — chosen from the release-time snapshot",
          list(CHORDS) == ["ctrl+shift+v"], str(CHORDS))
    check("focus_snapshot logged at the release with the terminal's class",
          any("event=focus_snapshot" in l and "at=release" in l and "target=kitty" in l
              for l in lines))
    check("take_insert names the release-time target",
          any("event=take_insert" in l and "target=kitty" in l for l in lines))
    check("the paste went to kitty with Ctrl+Shift+V, detect=focus",
          any("event=paste " in l and "target=kitty" in l and "chord=ctrl+shift+v" in l
              and "detect=focus" in l for l in lines),
          str([l.split("event=")[1] for l in lines if "event=paste " in l]))
    check("nothing was pasted against the later window",
          not any("event=paste " in l and "kwrite" in l for l in lines))
    check("the snapshot is dropped with the take",
          not any("focus_snapshot" in l and "at=insert" in l for l in lines))

    focus["now"] = snap("org.kde.kwrite")
    marks, pastes, lines, overlay = run_take(
        take2, release2, "end_of_take",
        "[5c] control: focus at release = kwrite", focus=lambda: focus["now"])
    check("the editor target gets Ctrl+V", list(CHORDS) == ["ctrl+v"], str(CHORDS))
    check("logged against org.kde.kwrite",
          any("event=paste " in l and "target=org.kde.kwrite" in l for l in lines))

    preview_unit_checks()
    preview_take_checks()
    part5_coalescing()
    part5b_normalize()
    part5c_take()
    part6_preview_pass()
    part3_clipboard()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
