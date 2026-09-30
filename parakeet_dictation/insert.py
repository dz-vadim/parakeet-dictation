"""Text insertion: filler filter and TextTyper (clipboard save/restore, paste chords, ydotool keycodes)."""

import re
import shutil
import subprocess
import sys
import threading
import time

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
