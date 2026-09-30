"""Text insertion: filler filter, paste-chord choice, clipboard staging and the
chord transports (xdg-desktop-portal keysyms, ydotool keycodes).

How a paste reaches ANY focused application on KDE Plasma Wayland, all of it
measured on the machine this ships on:

* The text is staged on BOTH the clipboard and the primary selection.  Ctrl+V
  is destructive in a terminal (it delivers VLNEXT and eats the next key), so
  terminals get Ctrl+Shift+V; Shift+Insert reads PRIMARY in kitty/alacritty but
  CLIPBOARD in Qt/GTK/Chromium, so with both selections holding the same text
  it is correct everywhere and serves as the fallback chord.  XF86Paste is not
  universal (nothing in Qt/GTK3/kitty/konsole) and is not used.
* The chord is pressed as keysyms through the RemoteDesktop portal when a
  session exists (layout independent, one prompt ever thanks to the restore
  token), else as `ydotool key` keycodes.  Never `ydotool type` — its ASCII
  table indexes out of bounds on Cyrillic.  Never Ctrl+Shift+Insert (Qt pastes
  PRIMARY), never bare Insert (overwrite mode / Paste Special).
* `wl-copy --paste-once` was meant to be the success signal (it exits once a
  consumer reads the selection).  On this desktop a clipboard watcher reads
  every new selection within a few hundred ms and the selection is then EMPTY,
  so --paste-once would make the paste itself fail.  Plain wl-copy and fixed
  timing are used instead; the fallback ladder therefore triggers on transport
  failure (portal call error, ydotool missing or non-zero), not on a paste
  that silently went nowhere.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace

from gi.repository import Gio, GLib

from . import config as _config
from .diagnostics import DIAG


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
# Paste chords
# ---------------------------------------------------------------------------

CHORD_CTRL_V = "ctrl+v"
CHORD_CTRL_SHIFT_V = "ctrl+shift+v"
CHORD_SHIFT_INSERT = "shift+insert"

# Linux input event keycodes (what `ydotool key` expects):
#   29 = LEFTCTRL, 42 = LEFTSHIFT, 47 = V, 110 = INSERT
_CHORD_KEYCODES = {
    CHORD_CTRL_V: ("29:1", "47:1", "47:0", "29:0"),
    CHORD_CTRL_SHIFT_V: ("29:1", "42:1", "47:1", "47:0", "42:0", "29:0"),
    CHORD_SHIFT_INSERT: ("42:1", "110:1", "110:0", "42:0"),
}

# X keysyms for the portal: Control_L, Shift_L, v, Insert.  Keysyms, not
# keycodes, are what makes the chord layout independent (Ctrl+V pasted under
# both `us` and `ua` here).
_KS_CONTROL_L, _KS_SHIFT_L, _KS_V, _KS_INSERT = 0xffe3, 0xffe1, 0x76, 0xff63
_CHORD_KEYSYMS = {
    CHORD_CTRL_V: ((_KS_CONTROL_L, 1), (_KS_V, 1), (_KS_V, 0), (_KS_CONTROL_L, 0)),
    CHORD_CTRL_SHIFT_V: ((_KS_CONTROL_L, 1), (_KS_SHIFT_L, 1), (_KS_V, 1),
                         (_KS_V, 0), (_KS_SHIFT_L, 0), (_KS_CONTROL_L, 0)),
    CHORD_SHIFT_INSERT: ((_KS_SHIFT_L, 1), (_KS_INSERT, 1), (_KS_INSERT, 0),
                         (_KS_SHIFT_L, 0)),
}

CHORDS = tuple(_CHORD_KEYCODES)

# Window classes (KWin resourceClass) that need Ctrl+Shift+V.  The config
# carries its own copy of this list; this is the seed for it.
DEFAULT_TERMINAL_CLASSES = [
    "kitty", "Alacritty", "alacritty", "org.kde.konsole", "konsole", "yakuake",
    "foot", "footclient", "org.wezfurlong.wezterm", "wezterm",
    "com.mitchellh.ghostty", "XTerm", "xterm", "st", "contour",
    "gnome-terminal-server", "terminator", "com.gexperts.Tilix",
]
# Classes where Ctrl+V is not paste at all; Shift+Insert is.
DEFAULT_NO_CTRL_V_CLASSES = ["emacs", "Emacs"]

# Delay between staging the selections and pressing the chord: wl-copy's
# ownership lands on the next compositor round trip.
STAGE_SETTLE_S = 0.06
# How long after the chord the clipboard is put back (fixed timing, see the
# module docstring for why there is no consumer signal to wait on).
RESTORE_AFTER_S = 0.5

_DETECT_FOCUS, _DETECT_CONFIG, _DETECT_NONE = "focus", "config", "none"


def _lower_set(items) -> set:
    return {str(c).lower() for c in (items or [])}


def choose_chord(cls, config) -> tuple:
    """(chord, detect) for a window class.

    `config` is anything with `paste_overrides`, `terminal_window_classes`,
    `no_ctrl_v_classes`, `paste_chord` and `terminal_paste_chord` (an AppConfig).
    Matching is case-insensitive.  Priority: explicit per-class override from
    the config (detect="config"), then the terminal and no-Ctrl+V tables
    (detect="focus"), then the configured default chord — also for an unknown
    class (detect="none"), since Ctrl+V is right in every non-terminal app.
    """
    default = getattr(config, "paste_chord", CHORD_CTRL_V)
    if default not in _CHORD_KEYCODES:
        default = CHORD_CTRL_V
    terminal_chord = getattr(config, "terminal_paste_chord", CHORD_CTRL_SHIFT_V)
    if terminal_chord not in _CHORD_KEYCODES:
        terminal_chord = CHORD_CTRL_SHIFT_V
    low = str(cls or "").lower()
    if not low:
        return default, _DETECT_NONE
    overrides = getattr(config, "paste_overrides", None) or {}
    for key, chord in overrides.items():
        if str(key).lower() == low and str(chord).lower() in _CHORD_KEYCODES:
            return str(chord).lower(), _DETECT_CONFIG
    if low in _lower_set(getattr(config, "terminal_window_classes", None)):
        return terminal_chord, _DETECT_FOCUS
    if low in _lower_set(getattr(config, "no_ctrl_v_classes", None)):
        return CHORD_SHIFT_INSERT, _DETECT_FOCUS
    return default, _DETECT_FOCUS


def prepare_for_target(text: str, terminal: bool) -> str:
    """Newline safety at the staging layer.

    A trailing newline is a real Return in many apps (submits forms, sends
    chats), so it always goes.  Terminals execute multi-line pastes when
    bracketed paste is off (foot/alacritty/VTE) or pop a modal that swallows
    the paste (kitty), so for them internal newlines collapse to spaces too.
    """
    text = text.rstrip("\r\n")
    if terminal:
        text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return text


def target_class(target) -> str:
    """Window class out of a FocusSnapshot, a plain string, or nothing."""
    if target is None:
        return ""
    cls = getattr(target, "resource_class", target)
    return str(cls or "")


# ---------------------------------------------------------------------------
# Chord transport: xdg-desktop-portal RemoteDesktop (keysyms)
# ---------------------------------------------------------------------------

class PortalKeyboard:
    """One RemoteDesktop session (keyboard only) used to press paste chords.

    CreateSession -> SelectDevices(types=KEYBOARD, persist_mode=2,
    restore_token=<saved>) -> Start.  persist_mode and restore_token go on
    SelectDevices; Start returns the token to persist, and with it the user is
    prompted ONCE ("Remote Control") — never again for this app.  The prompt
    only the user can answer, so the session is started at app start on a
    thread with its own main context, never from the insertion path: a paste
    that finds no ready session uses ydotool instead of raising a dialog.
    Typing whole text through the portal is not viable (Chromium/Electron on
    Plasma <= 6.7 drop non-layout characters), so this is chord-only.
    """

    BUS = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.RemoteDesktop"
    REQUEST_IFACE = "org.freedesktop.portal.Request"
    SESSION_IFACE = "org.freedesktop.portal.Session"
    TOKEN_FILE = "portal-restore-token"
    DEVICE_KEYBOARD = 1
    PERSIST_UNTIL_REVOKED = 2
    START_TIMEOUT_S = 120.0     # a dialog the user has to click
    REQUEST_TIMEOUT_S = 10.0

    def __init__(self):
        self._bus = None
        self._session = None
        self._state = "idle"     # idle | starting | ready | unavailable | failed
        self._thread = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state == "ready" and self._session is not None

    # -- token persistence -------------------------------------------------

    @staticmethod
    def _token_path():
        return _config.CONFIG_DIR / PortalKeyboard.TOKEN_FILE

    @classmethod
    def load_token(cls) -> str:
        try:
            return cls._token_path().read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @classmethod
    def save_token(cls, token: str):
        try:
            path = cls._token_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token, encoding="utf-8")
            path.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def forget_token(cls):
        try:
            cls._token_path().unlink()
        except OSError:
            pass

    # -- D-Bus helpers -----------------------------------------------------

    def _call(self, method, params, reply_type=None, timeout_ms=5000, path=None,
              iface=None):
        return self._bus.call_sync(
            self.BUS, path or self.PATH, iface or self.IFACE, method, params,
            GLib.VariantType.new(reply_type) if reply_type else None,
            Gio.DBusCallFlags.NONE, timeout_ms, None)

    def interface_present(self) -> bool:
        """RemoteDesktop >= 2 on the bus (NotifyKeyboardKeysym exists)."""
        try:
            if self._bus is None:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            version = self._bus.call_sync(
                self.BUS, self.PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (self.IFACE, "version")),
                GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 3000,
                None).unpack()[0]
            return int(version) >= 2
        except (GLib.Error, TypeError, ValueError):
            return False

    def _request(self, ctx, method, build_params, timeout_s):
        """Call a portal method and wait for its Request's Response on `ctx`.

        Returns (response_code, results) — code 0 is success, 1 the user
        cancelled, 2 anything else; None if nothing came back in time.
        """
        token = "parakeet" + uuid.uuid4().hex
        sender = self._bus.get_unique_name().lstrip(":").replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        outcome = {}
        loop = GLib.MainLoop.new(ctx, False)

        def on_response(_c, _s, _p, _i, _sig, params):
            code, results = params.unpack()
            outcome["code"], outcome["results"] = code, results
            loop.quit()

        sub = self._bus.signal_subscribe(self.BUS, self.REQUEST_IFACE, "Response",
                                         handle, None, Gio.DBusSignalFlags.NONE,
                                         on_response)
        timer = GLib.timeout_source_new(int(timeout_s * 1000))
        timer.set_callback(lambda *_: (loop.quit(), GLib.SOURCE_REMOVE)[1])
        timer.attach(ctx)
        try:
            self._call(method, build_params(token), "(o)")
            loop.run()
        finally:
            timer.destroy()
            self._bus.signal_unsubscribe(sub)
        if "code" not in outcome:
            return None
        return outcome["code"], outcome["results"]

    # -- session lifecycle -------------------------------------------------

    def start_async(self):
        """Begin the session on a worker thread; `ready` flips when it is up."""
        with self._lock:
            if self._thread is not None or self._state == "ready":
                return
            self._state = "starting"
            self._thread = threading.Thread(target=self._start_worker, daemon=True,
                                            name="portal-keyboard")
            self._thread.start()

    def wait(self, timeout: float) -> bool:
        """Block until the start attempt finishes (live tests); returns ready."""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return self.ready

    def _start_worker(self):
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            self._start(ctx)
        except Exception as e:   # a crashed worker must not stay "starting"
            self._state = "failed"
            DIAG.log("portal_session", ready=False, err=type(e).__name__,
                     stage="dbus")
        finally:
            ctx.pop_thread_default()
            with self._lock:
                self._thread = None

    def _start(self, ctx):
        t0 = time.monotonic()
        if not self.interface_present():
            self._state = "unavailable"
            DIAG.log("portal_session", ready=False, stage="interface", available=False)
            return
        def opts(token, **extra):
            # A plain dict of Variants: the outer tuple Variant builds the
            # a{sv} itself and rejects a pre-built one.
            return {"handle_token": GLib.Variant("s", token), **extra}

        res = self._request(ctx, "CreateSession", lambda tok: GLib.Variant(
            "(a{sv})", (opts(tok, session_handle_token=GLib.Variant(
                "s", "parakeet" + uuid.uuid4().hex)),)), self.REQUEST_TIMEOUT_S)
        if not res or res[0] != 0 or "session_handle" not in res[1]:
            self._state = "failed"
            DIAG.log("portal_session", ready=False, stage="create",
                     code=res[0] if res else "timeout")
            return
        session = str(res[1]["session_handle"])

        saved = self.load_token()
        extra = {"types": GLib.Variant("u", self.DEVICE_KEYBOARD),
                 "persist_mode": GLib.Variant("u", self.PERSIST_UNTIL_REVOKED)}
        if saved:
            extra["restore_token"] = GLib.Variant("s", saved)
        res = self._request(ctx, "SelectDevices", lambda tok: GLib.Variant(
            "(oa{sv})", (session, opts(tok, **extra))), self.REQUEST_TIMEOUT_S)
        if not res or res[0] != 0:
            self._state = "failed"
            DIAG.log("portal_session", ready=False, stage="select",
                     code=res[0] if res else "timeout")
            return

        res = self._request(ctx, "Start", lambda tok: GLib.Variant(
            "(osa{sv})", (session, "", opts(tok))), self.START_TIMEOUT_S)
        if not res or res[0] != 0:
            self._state = "failed"
            if saved:
                self.forget_token()   # a revoked grant must not be retried forever
            DIAG.log("portal_session", ready=False, stage="start",
                     code=res[0] if res else "timeout", had_token=bool(saved))
            return
        token = str(res[1].get("restore_token", "") or "")
        if token:
            self.save_token(token)
        self._session = session
        self._state = "ready"
        DIAG.log("portal_session", ready=True, restored=bool(saved),
                 token=bool(token), ms=(time.monotonic() - t0) * 1000)

    def press(self, chord: str) -> bool:
        """Press a chord as keysyms.  False (and the session marked dead) on
        any D-Bus error, so the caller can fall through to ydotool."""
        if not self.ready or chord not in _CHORD_KEYSYMS:
            return False
        try:
            for keysym, state in _CHORD_KEYSYMS[chord]:
                self._call("NotifyKeyboardKeysym",
                           GLib.Variant("(oa{sv}iu)", (self._session, {}, keysym, state)),
                           None, timeout_ms=2000)
        except GLib.Error as e:
            self._state = "failed"
            self._session = None
            DIAG.log("portal_press_failed", err=type(e).__name__)
            return False
        return True

    def close(self):
        session, self._session = self._session, None
        self._state = "idle"
        if session and self._bus is not None:
            try:
                self._call("Close", None, None, path=session, iface=self.SESSION_IFACE)
            except GLib.Error:
                pass


_portal = None
_portal_lock = threading.Lock()


def portal_keyboard() -> PortalKeyboard:
    """The process-wide portal session (a typer is rebuilt on every settings
    save; the grant must not be)."""
    global _portal
    with _portal_lock:
        if _portal is None:
            _portal = PortalKeyboard()
        return _portal


# ---------------------------------------------------------------------------
# Text typer
# ---------------------------------------------------------------------------

def _run_quiet(args, timeout=5, input=None):
    """Run a helper that may daemonise (wl-copy): no inherited pipes, else the
    caller deadlocks waiting for a stdout the daemon never closes.

    `input` (bytes) goes to the helper's stdin.  That is how a restore hands
    wl-copy the user's clipboard back: the bytes as read, not an argv string
    — argv would decode them (and clear a non-UTF-8 clipboard) and caps one
    argument at 128 KiB (E2BIG, measured: a 300 KB clipboard was silently
    not restored).
    """
    return subprocess.run(args, timeout=timeout, input=input, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, close_fds=True)


# What to install when a helper is missing, by executable name.
_HELPER_PACKAGE = {
    "wl-copy": "wl-clipboard", "wl-paste": "wl-clipboard",
    "ydotool": "ydotool (and start ydotoold)", "xdotool": "xdotool", "wtype": "wtype",
}


def _report_missing(exc, fallback: str, method: str, notify=None) -> str:
    """Name the executable that is actually missing, in the log and the error.

    subprocess puts argv[0] in `exc.filename`; when that is absent (a wrapped
    or re-raised OSError) `fallback` is the helper that was being run.  The
    typer method ("clipboard") is never the answer — it is not a program
    anyone can install.
    """
    exe = os.path.basename(str(getattr(exc, "filename", None) or fallback))
    DIAG.log("helper_missing", exe=exe, method=method)
    hint = _HELPER_PACKAGE.get(exe, exe)
    print(f"ERROR: {exe} not found — install {hint}.", file=sys.stderr)
    if notify:
        notify(f"{exe} not found — install {hint}")
    return exe


class TextTyper:
    def __init__(self, method: str = "clipboard", keep_on_clipboard: bool = False,
                 paste_chord: str = CHORD_CTRL_V,
                 terminal_paste_chord: str = CHORD_CTRL_SHIFT_V,
                 terminal_window_classes=None, no_ctrl_v_classes=None,
                 paste_overrides=None, paste_transport: str = "auto",
                 on_failure=None):
        self._method = method
        self._keep_on_clipboard = keep_on_clipboard
        # `typer: "ydotool"` used to mean `ydotool type`, which cannot type
        # Cyrillic; it now means "clipboard staging, chord via ydotool".
        if method == "ydotool":
            paste_transport = "ydotool"
        self._transport = paste_transport if paste_transport in ("auto", "portal",
                                                                 "ydotool") else "auto"
        self._chord_config = SimpleNamespace(
            paste_chord=paste_chord,
            terminal_paste_chord=terminal_paste_chord,
            terminal_window_classes=list(terminal_window_classes
                                         if terminal_window_classes is not None
                                         else DEFAULT_TERMINAL_CLASSES),
            no_ctrl_v_classes=list(no_ctrl_v_classes
                                   if no_ctrl_v_classes is not None
                                   else DEFAULT_NO_CTRL_V_CLASSES),
            paste_overrides=dict(paste_overrides or {}),
        )
        self._on_failure = on_failure
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
        self._clip_saved = None        # (clipboard, primary) of the user's
        self._clip_restore_pending = False
        self._clip_gen = 0

    @property
    def chord_config(self):
        return self._chord_config

    # -- low-level helpers --------------------------------------------------

    def _type_raw(self, text: str, target=None):
        """Type a string into the active window (no newline safety)."""
        try:
            if self._method == "wtype":
                subprocess.run(["wtype", "--", text], timeout=5)
            else:
                self._clipboard_paste(text, target)
        except FileNotFoundError as e:
            # Only wtype and the wl-copy staging can raise here: the chord
            # transports report their own absence (see _send_chord).
            _report_missing(e, "wtype" if self._method == "wtype" else "wl-copy",
                            self._method, self._on_failure)
        except subprocess.TimeoutExpired:
            pass
        except OSError as e:
            # E2BIG from an oversized argv, EACCES/EPERM on the helper, ...:
            # reported the same way as a failed paste, never raised, because
            # an exception out of here used to end the caller's take at
            # STOPPING for good.
            exe = os.path.basename(str(getattr(e, "filename", None)
                                       or ("wtype" if self._method == "wtype" else "wl-copy")))
            DIAG.log("insert_failed", err=type(e).__name__, errno=e.errno or 0, exe=exe)
            print(f"ERROR: {exe}: {e.strerror or e}", file=sys.stderr)
            if self._on_failure:
                self._on_failure(f"Insertion failed: {exe}: {e.strerror or e}")

    def _clipboard_paste(self, text: str, target=None):
        """Stage *text* on both selections, press the chord, restore later."""
        cls = target_class(target)
        chord, detect = choose_chord(cls, self._chord_config)
        text = prepare_for_target(text, terminal=(chord == CHORD_CTRL_SHIFT_V))
        if not text:
            return
        saved, gen = None, 0
        with self._clip_lock:
            if not self._keep_on_clipboard:
                if not self._clip_restore_pending:
                    self._clip_saved = (self._read_selection(primary=False),
                                        self._read_selection(primary=True))
                # A restore still pending means the clipboard holds the
                # PREVIOUS segment's text, not the user's, so re-reading it
                # here would save our own output and hand it back later.
                self._clip_restore_pending = True
                self._clip_gen += 1   # invalidates any restore already waiting
                gen = self._clip_gen
                saved = self._clip_saved
            try:
                _run_quiet(["wl-copy", "--", text])
                _run_quiet(["wl-copy", "--primary", "--", text])
            except OSError:
                # Nothing (or only half) was staged: the user's clipboard is
                # still theirs, so there is nothing to put back later — and
                # the next paste must read it afresh.
                self._clip_restore_pending = False
                self._clip_saved = None
                raise
            time.sleep(STAGE_SETTLE_S)
            ok = self._paste(chord, detect, cls)
            if not ok:
                # The text stays where the user can still get it: on the
                # clipboard.  Drop the saved value rather than restore over it.
                self._clip_restore_pending = False
                self._clip_saved = None
                DIAG.log("paste_failed", target=cls or "unknown", chars=len(text))
                if self._on_failure:
                    self._on_failure("Text is on the clipboard — press Ctrl+V")
                return
            if not self._keep_on_clipboard:
                self._restore_clipboard_later(saved, gen)

    @staticmethod
    def _read_selection(primary: bool = False):
        """Current selection as raw bytes, or None if empty or not text.

        Verbatim: `--no-newline` makes wl-paste output the content as is (it
        otherwise appends one '\\n'), and the bytes are kept as bytes so the
        restore can hand back exactly what was read — a trailing newline, a
        non-UTF-8 encoding, any size.
        """
        try:
            # No --type: wl-paste then picks whichever text flavour the owner
            # offers.  Pinning "text/plain" fails against apps that only offer
            # "text/plain;charset=utf-8", which would clear the clipboard
            # instead of restoring it.
            args = ["wl-paste", "--no-newline"] + (["--primary"] if primary else [])
            res = subprocess.run(args, capture_output=True, timeout=2)
        except FileNotFoundError as e:
            _report_missing(e, "wl-paste", "clipboard")
            return None
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0:
            return None
        return bytes(res.stdout)

    @classmethod
    def _read_clipboard(cls):
        return cls._read_selection(primary=False)

    def _restore_clipboard_later(self, saved, gen: int):
        """Put the user's selections back RESTORE_AFTER_S after the paste.

        Delayed and on a background thread: the target window reads the
        selection asynchronously, so restoring at once can race the paste, and
        blocking here would stall text delivery.  The bytes wl-paste returned
        go back through wl-copy's stdin untouched.  Only text survives the
        round-trip — an image that was on the clipboard is already lost by the
        time wl-copy ran, so a clipboard that could not be read is cleared
        rather than left holding the dictated phrase (that is the
        Klipper-history leak this exists to prevent).
        """
        def _worker():
            time.sleep(RESTORE_AFTER_S)
            with self._clip_lock:
                if gen != self._clip_gen:
                    return   # a later paste owns the clipboard; it will restore
                clip, primary = saved if saved else (None, None)
                for flag, value in (([], clip), (["--primary"], primary)):
                    try:
                        if value:
                            _run_quiet(["wl-copy", *flag], input=value)
                        else:
                            _run_quiet(["wl-copy", *flag, "--clear"])
                    except (OSError, subprocess.TimeoutExpired) as e:
                        DIAG.log("clipboard_restore_failed", primary=bool(flag),
                                 err=type(e).__name__)
                self._clip_restore_pending = False
                self._clip_saved = None

        threading.Thread(target=_worker, daemon=True).start()

    # -- chord transport ----------------------------------------------------

    def _portal(self):
        if self._transport == "ydotool":
            return None
        portal = portal_keyboard()
        return portal if portal is not None and portal.ready else None

    def _send_chord(self, chord: str) -> tuple:
        """Press one chord.  Returns (ok, transport)."""
        portal = self._portal()
        if portal is not None:
            if portal.press(chord):
                return True, "portal"
            # Session died: fall through to ydotool for this very chord.
        if shutil.which("ydotool"):
            try:
                res = subprocess.run(["ydotool", "key", *_CHORD_KEYCODES[chord]],
                                     timeout=5, capture_output=True)
            except FileNotFoundError as e:      # gone between which() and exec
                _report_missing(e, "ydotool", self._method)
                return False, "ydotool"
            except (OSError, subprocess.TimeoutExpired):
                return False, "ydotool"
            return res.returncode == 0, "ydotool"
        if shutil.which("xdotool"):
            # Last resort — only reaches XWayland windows.
            try:
                res = subprocess.run(["xdotool", "key", chord], timeout=5,
                                     capture_output=True)
            except FileNotFoundError as e:
                _report_missing(e, "xdotool", self._method)
                return False, "xdotool"
            except (OSError, subprocess.TimeoutExpired):
                return False, "xdotool"
            return res.returncode == 0, "xdotool"
        return False, "none"

    def _paste(self, chord: str, detect: str, cls: str) -> bool:
        """The fallback ladder: the chosen chord, then Shift+Insert once.

        Both selections hold the text, so Shift+Insert is the one chord that is
        right in every app; it is the retry after a transport failure.
        """
        attempts = [chord] if chord == CHORD_SHIFT_INSERT else [chord, CHORD_SHIFT_INSERT]
        for attempt, ch in enumerate(attempts, 1):
            ok, transport = self._send_chord(ch)
            DIAG.log("paste", chord=ch, transport=transport, target=cls or "unknown",
                     detect=detect, ok=ok, attempt=attempt)
            if ok:
                return True
        return False

    def _send_backspaces(self, count: int):
        """Erase *count* characters via repeated BackSpace key presses."""
        if count <= 0:
            return
        if self._method == "wtype":
            helper = "wtype"
        elif shutil.which("ydotool"):
            helper = "ydotool"
        else:
            helper = "xdotool"      # last resort — only reaches XWayland windows
        try:
            for _ in range(count):
                if helper == "wtype":
                    subprocess.run(["wtype", "-k", "BackSpace"], timeout=5)
                elif helper == "ydotool":
                    # ydotool key takes Linux input keycodes; BackSpace = 14
                    subprocess.run(["ydotool", "key", "14:1", "14:0"], timeout=5)
                else:
                    subprocess.run(["xdotool", "key", "BackSpace"], timeout=5)
        except FileNotFoundError as e:
            _report_missing(e, helper, self._method, self._on_failure)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _sanitize(text: str) -> str:
        """Strip newlines/carriage-returns — never inject Enter."""
        return text.replace("\n", " ").replace("\r", " ").strip()

    # -- public API ---------------------------------------------------------

    def type_text(self, text: str, target=None):
        """Type final (committed) text — adds trailing space.

        `target` is the focus snapshot the caller took when the take ended
        (whatever was focused at release, not 300 ms later); None means the
        class is unknown and the configured default chord applies.
        """
        text = self._sanitize(text)
        if not text:
            return
        self._type_raw(text + " ", target)

    def type_partial(self, text: str, target=None):
        """Type a streaming partial, erasing the previous partial first."""
        text = self._sanitize(text)
        if not text:
            return
        # Erase whatever we typed last time
        self._send_backspaces(self._partial_len)
        self._type_raw(text, target)
        self._partial_len = len(text)

    def commit_partial(self, text: str, target=None):
        """Commit (finalize) a partial: erase old partial, type final + space."""
        text = self._sanitize(text)
        self._send_backspaces(self._partial_len)
        self._partial_len = 0
        if text:
            self._type_raw(text + " ", target)

    def reset_partial(self):
        """Discard partial tracking without erasing anything on screen."""
        self._partial_len = 0
