"""Pill overlay and transcript preview (gtk-layer-shell, with a plain-window fallback)."""

import math
import time

import cairo
import gi
from gi.repository import Gdk, GLib, Gtk

from ..config import APP_NAME, AppConfig
from ..diagnostics import DIAG
from ..insert import _WS_RE

PREPARING_GRACE_MS = 600

# The overlay claims the microphone is live; that claim has to be cheap to
# falsify.  The watchdog re-checks it against the engine, and the meter stops
# animating once the capture loop has stopped feeding it.
OVERLAY_WATCHDOG_MS = 2000
OVERLAY_LEVEL_STALE_S = 2.0


def _fallback_window(win) -> bool:
    """A plain toplevel in place of a layer-shell surface.  Returns `degraded`.

    Asks for everything GTK offers: no decorations, the NOTIFICATION type
    hint, keep-above, fixed size.  On an X11 display the window manager
    honours those and Gtk.Window.move() places the window.  On a Wayland
    display Gtk.Window.move() is a documented no-op for a toplevel and the
    hints are not delivered, so the compositor decides where the window goes
    and whether it gets a titlebar — and the client cannot observe either.
    That case is reported as degraded rather than pretended away: the overlay
    keeps working, it just is not where the code asked for it.
    """
    win.set_decorated(False)
    win.set_resizable(False)
    win.set_keep_above(True)
    win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
    return _display_backend(win) != "GdkX11Display"


def _display_backend(win) -> str:
    display = win.get_display()
    return display.__gtype__.name if display is not None else "none"


# ---------------------------------------------------------------------------
# Pill overlay — a capsule that says what dictation is doing, right now
#
# gtk-layer-shell puts it on the OVERLAY layer with an exclusive zone of -1, so
# panels keep their geometry, and with an EMPTY input region, so every click
# lands in the window underneath.  Shape carries meaning: every normal state is
# the same capsule, and only an error is a wider rounded rectangle — the shape
# says "something is wrong" before the message has been read.
# ---------------------------------------------------------------------------


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
    with text are measure it and paint it.  tests/test_hold_take.py asserts
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
        self.degraded = False         # fallback window the compositor places itself
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
            self.degraded = _fallback_window(win)
            DIAG.log("preview_backend", backend="fallback_window",
                     degraded=self.degraded, display=_display_backend(win),
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
        if self.layered or self.degraded or self._win is None:
            return          # degraded: move() would be a no-op, not a placement
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
        self.degraded = False         # fallback window the compositor places itself
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
            # A plain keep-above window instead; it will not sit over
            # fullscreen clients and the compositor may still focus it.  See
            # _fallback_window for what a Wayland compositor does and does
            # not let it ask for.
            self.degraded = _fallback_window(win)
            DIAG.log("overlay_backend", backend="fallback_window",
                     degraded=self.degraded, display=_display_backend(win),
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
        if self.layered or self.degraded:
            return          # degraded: move() would be a no-op, not a placement
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
