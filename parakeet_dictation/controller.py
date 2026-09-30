"""DictationController: take state machine, deferred stop, release tail, insert modes, preview feed."""

import sys
import threading
import time

from gi.repository import GLib

from .config import AppConfig
from .diagnostics import DIAG
from .engine import ASREngine
from .insert import TextTyper, _WS_RE, filter_fillers
from .models import load_model_profiles

# Push-to-talk timing.  The tail and the UI run on two independent clocks from
# one key release: capture keeps going a moment longer so the last word is not
# clipped, while the pill flips to "processing" immediately so the release
# still feels instant.
RELEASE_TAIL_MS = 200

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


def overlay_error_message(msg: str) -> str:
    """Short and actionable for the pill; the full text still goes to stderr."""
    low = msg.lower()
    if "missing model" in low or "download_models" in low:
        return "Model files missing — open Settings › Models"
    if "portaudio" in low or "device" in low or "sounddevice" in low:
        return "Microphone unavailable — check Settings › General"
    first = msg.strip().splitlines()[0] if msg.strip() else ""
    return (first[:58] + "…") if len(first) > 58 else (first or "Dictation failed")


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
        # Focused-window probe (FocusTracker.snapshot) and the snapshot taken
        # the moment the take ended — the paste target is whatever was focused
        # when the user let go, not whatever is focused once the decode lands.
        self._focus_probe = None
        self._focus_at_release = None
        self._rebuild_engine()

    def _make_typer(self, config: AppConfig) -> TextTyper:
        return TextTyper(
            config.typer,
            keep_on_clipboard=config.keep_on_clipboard,
            paste_chord=config.paste_chord,
            terminal_paste_chord=config.terminal_paste_chord,
            terminal_window_classes=config.terminal_window_classes,
            no_ctrl_v_classes=config.no_ctrl_v_classes,
            paste_overrides=config.paste_overrides,
            paste_transport=config.paste_transport,
            on_failure=self._on_paste_failed,
        )

    def set_focus_probe(self, probe):
        """`probe()` returns a FocusSnapshot (or None) for the active window."""
        self._focus_probe = probe

    def _snapshot_focus(self, when: str):
        if not self._focus_probe:
            return None
        try:
            snap = self._focus_probe()
        except Exception as e:   # a probe must never break a take
            DIAG.log("focus_probe_failed", err=type(e).__name__, at=when)
            return None
        DIAG.log("focus_snapshot", at=when, take=self._take_seq,
                 target=(getattr(snap, "resource_class", "") or "unknown"))
        return snap

    def _on_paste_failed(self, message: str):
        """Called by the typer, possibly off the main loop, when no transport
        could press the chord.  The text is left on the clipboard; say so."""
        print(f"WARNING: {message}", file=sys.stderr)
        GLib.idle_add(self._overlay_state, "error", message)

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
        self._focus_at_release = self._snapshot_focus("stop")
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
        if decision in (STOP_PROCEED, STOP_DEFER):
            # The target is fixed HERE, at the release: the decode still has
            # a few hundred ms to run and the user may already be elsewhere.
            self._focus_at_release = self._snapshot_focus("release")
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
            # stop() returns once capture has stopped and the decoder has
            # drained (bounded), so the segment the flush just queued is in
            # hand before the take is declared over — in end-of-take mode
            # because it belongs in the one insertion, in per-segment mode
            # because it is the tail the user was still speaking when they
            # let go.  wait_drained() is the belt to that brace.
            self._engine.stop()
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
        # After the release the target is the release-time snapshot; a
        # per-segment insertion while the key is still down goes to whatever
        # is focused right now.
        target = self._focus_at_release
        if target is None and not self._take_stop_t0:
            target = self._snapshot_focus("insert")
        DIAG.log("take_insert", mode=self._config.insert_mode,
                 index=self._take_inserts, chars=len(text), segments=segments,
                 waited_ms=waited_ms, take=self._take_seq,
                 target=(getattr(target, "resource_class", "") or "unknown"))
        self._typer.type_text(text, target=target)
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
        self._focus_at_release = None
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
