# Tray Menu and Settings Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the pynput hotkeys, the tray menu, the main window and the tabbed settings dialog with two KDE-registered shortcuts (hold `Insert`, start/stop `Meta+Insert`), a short honest tray menu, a KDE-style settings window, and a Ukrainian/English interface.

**Architecture:** Pure-Python logic (shortcut parsing/capture, config migration, menu model, settings draft, autostart file) lives in display-free modules with their own tests; thin GTK3 layers render it. kglobalaccel gets one component with two actions. gettext catalogues ship inside the package as committed `.po` + `.mo`.

**Tech Stack:** Python 3.12 (`.venv`), PyGObject GTK 3.24, AyatanaAppIndicator3, Gio D-Bus (kglobalaccel), stdlib `gettext`; gettext tools (`xgettext`, `msgmerge`, `msgfmt`) only for the translation refresh script.

**Spec:** `docs/superpowers/specs/2026-10-15-tray-settings-redesign-design.md` (mockups: `docs/design/2026-10-15-settings-mockups/index.html`).

## Global Constraints

- Toolkit stays GTK3; no new runtime dependency. `python-pynput` / `pynput` is **removed** from every dependency list.
- Defaults for everyone: `hotkey_hold = "Insert"`, `hotkey_toggle = "Meta+Insert"`; both configurable; an empty string disables that action.
- Shortcuts are stored and shown in KDE notation (`Meta+Alt+D`), modifier order `Meta`, `Ctrl`, `Alt`, `Shift`.
- `insert_mode` default `end_of_take`; it applies to start/stop takes only; a push-to-talk take always inserts at release.
- `paste_min_delay_ms` (default 350) applies only when the shortcut that ended the take contains a modifier, or the stop came from SIGUSR1; never after a bare-key release or a tray click.
- `ui_language`: `system` | `uk` | `en`, default `system`; takes effect at the next start.
- Source strings are English, wrapped in `_()`; every user-visible string in tray, settings, welcome dialog and controller/insert/hotkey notices is translated to Ukrainian. Diagnostics log lines stay English and are never translated.
- The app never stores dictated text or audio (privacy decision 2026-10-15) — no history feature.
- Test gate after every task: all suites pass — `.venv/bin/python tests/test_defects.py`, `tests/test_hold_take.py`, `tests/test_pipeline.py`, `tests/test_review.py`, `tests/ptt_state.py`, `tests/overlay_safety.py`, and the new `tests/test_settings.py`.
- Never send real key events (ydotool/portal) or restart the user's running app from a task; the final task restarts it once.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push.

## Review Focus

1. Hold and toggle set to the **same** binding (e.g. both `Insert`) — expected: settings refuses it inline ("already used for Hold to talk"); a hand-edited config is fixed by `enforce()` (toggle reset), never two actions on one key. → Task 3 (enforce) and Task 2 (`capture` has no knowledge; checked in Task 9 window `_validate`).
2. User changes a shortcut in **System Settings > Shortcuts** while the app runs — expected: the app adopts it and saves it to config, so the next start doesn't overwrite it with the old value. → Task 4 (`yourShortcutsChanged` handler).
3. Model switched from the **tray** while the Settings window is open with other unsaved edits — expected: Apply writes only the keys edited in the window on top of the *current* config; the tray's model choice survives. → Task 7 (`SettingsDraft.commit` diff-merge).
4. Saved microphone **not connected** — expected: the combo shows "<name> (not connected)" selected, the value is kept on Apply, dictation uses the default device. → Task 8 (`device_choices`).
5. Old config from before the redesign (`hotkey_mode: "toggle"`, `hotkey_toggle: "<ctrl>+0"`, `hotkey_pause`, `hotkey_hold: "Meta+Z"`) — expected: loads without error, migrates to `Meta+Insert` / `Insert`, logs `config_migrated`, the next save drops the dead keys. → Task 3.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `parakeet_dictation/shortcuts.py` | create | KDE shortcut strings: parse to Qt int, reverse, validate, modifier test, key-capture state machine. No GTK, no D-Bus. |
| `parakeet_dictation/i18n.py` | create | gettext setup (`setup(language)`), `_`, `ngettext`. |
| `parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po` / `.mo` | create | Ukrainian catalogue. |
| `tools/update-translations.sh` | create | xgettext → msgmerge → msgfmt. |
| `parakeet_dictation/autostart.py` | create | read/write `~/.config/autostart/parakeet-dictation.desktop`. |
| `parakeet_dictation/ui/menu_model.py` | create | tray menu as data (pure). |
| `parakeet_dictation/ui/settings/__init__.py` | create | exports `SettingsWindow`. |
| `parakeet_dictation/ui/settings/model.py` | create | `SettingsDraft`, `PAGE_KEYS` (pure). |
| `parakeet_dictation/ui/settings/widgets.py` | create | `Section`, `ShortcutButton`, `device_choices`. |
| `parakeet_dictation/ui/settings/pages.py` | create | five page builders. |
| `parakeet_dictation/ui/settings/window.py` | create | `SettingsWindow` (sidebar, stack, buttons, InfoBar). |
| `parakeet_dictation/config.py` | modify | fields, migration, enforce. |
| `parakeet_dictation/hotkeys.py` | modify | two kglobalaccel actions, no pynput. |
| `parakeet_dictation/controller.py` | modify | take kind, hold-off rule, `stop(hold_off=)`. |
| `parakeet_dictation/ui/tray.py` | rewrite | render `menu_model`, tooltip, single settings window. |
| `parakeet_dictation/ui/windows.py` | shrink | only `WelcomeDialog` stays. |
| `parakeet_dictation/app.py` | modify | i18n, wiring, no MainWindow. |
| `tests/test_settings.py` | create | display-free tests for all pure modules + GTK smoke tests. |
| `pyproject.toml`, `requirements.txt`, `packaging/PKGBUILD`, `packaging/.SRCINFO`, `README.md` | modify | drop pynput, package locale data, docs. |

---

### Task 1: `shortcuts.py` — parse, reverse, validate, capture

**Files:**
- Create: `parakeet_dictation/shortcuts.py`
- Modify: `parakeet_dictation/hotkeys.py` (delete `QT_MODIFIER_BITS`, `QT_NAMED_KEYS`, `qt_key_sequence`; add `from .shortcuts import qt_key_sequence  # noqa: F401  (re-export)`)
- Test: `tests/test_settings.py` (new suite, harness copied from `tests/test_review.py`)

**Interfaces:**
- Produces:
  - `qt_key_sequence(binding: str) -> int` (moved unchanged; raises `ValueError`)
  - `binding_from_qt(key: int) -> str` (inverse; `""` for 0)
  - `is_valid(binding: str) -> bool` (`""` is valid = disabled)
  - `has_modifier(binding: str) -> bool`
  - `display(binding: str) -> str` (`""` → `""`; otherwise the binding itself)
  - `capture(keyname: str, mods: frozenset) -> tuple[str, str]` — status in `{"wait","cancel","clear","done","reject"}`, binding (`""` unless `done`)
  - `MODIFIERS = ("Meta", "Ctrl", "Alt", "Shift")`

- [ ] **Step 1: Create the test suite skeleton with failing tests**

`tests/test_settings.py`:

```python
#!/usr/bin/env python3
"""Checks for the tray/settings redesign (2026-10-15).

Display-free sections test pure modules; sections marked GTK build widgets
offscreen.  Nothing touches the real config, autostart dir, D-Bus names or
the diagnostics log.

Run:  .venv/bin/python tests/test_settings.py            # every section
      .venv/bin/python tests/test_settings.py 1 3        # sections 1 and 3
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

FAILURES = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def s1_shortcuts():
    print("\n[1] shortcuts: parse, reverse, validate, capture")
    from parakeet_dictation import shortcuts as sc
    check("Meta+Alt+D is 0x18000044", sc.qt_key_sequence("Meta+Alt+D") == 0x18000044)
    check("Insert parses", sc.qt_key_sequence("Insert") == 0x01000006)
    for b in ("Insert", "Meta+Insert", "Meta+Alt+D", "Ctrl+Shift+F5", "Pause", "Meta+Space"):
        rt = sc.binding_from_qt(sc.qt_key_sequence(b))
        check(f"round trip {b}", rt == b, rt)
    check("binding_from_qt(0) is empty", sc.binding_from_qt(0) == "")
    check("empty is valid (disabled)", sc.is_valid(""))
    check("<ctrl>+0 is invalid", not sc.is_valid("<ctrl>+0"))
    check("Insert has no modifier", not sc.has_modifier("Insert"))
    check("Meta+Insert has a modifier", sc.has_modifier("Meta+Insert"))
    F = frozenset
    cases = [
        (("Super_L", F({"Meta"})), ("wait", "")),
        (("Control_R", F({"Ctrl"})), ("wait", "")),
        (("Escape", F()), ("cancel", "")),
        (("BackSpace", F()), ("clear", "")),
        (("Insert", F()), ("done", "Insert")),
        (("Insert", F({"Meta"})), ("done", "Meta+Insert")),
        (("d", F({"Alt", "Meta"})), ("done", "Meta+Alt+D")),
        (("F5", F({"Shift", "Ctrl"})), ("done", "Ctrl+Shift+F5")),
        (("F13", F()), ("done", "F13")),
        (("a", F()), ("reject", "")),
        (("space", F()), ("reject", "")),
        (("space", F({"Meta"})), ("done", "Meta+Space")),
        (("Scroll_Lock", F()), ("done", "ScrollLock")),
        (("Prior", F({"Ctrl"})), ("done", "Ctrl+PageUp")),
        (("dead_acute", F({"Ctrl"})), ("reject", "")),
    ]
    for (name, mods), want in cases:
        got = sc.capture(name, mods)
        check(f"capture {name} {sorted(mods)}", got == want, str(got))


SECTIONS = {1: s1_shortcuts}


def main(argv):
    wanted = [int(a) for a in argv] or sorted(SECTIONS)
    for n in wanted:
        section = SECTIONS.get(n)
        if section is None:
            check(f"section {n} exists", False)
            continue
        try:
            section()
        except Exception as e:
            check(f"{section.__name__} ran without raising", False, f"{type(e).__name__}: {e}")
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 2: Run, expect FAIL**

Run: `.venv/bin/python tests/test_settings.py 1`
Expected: `s1_shortcuts ran without raising — ModuleNotFoundError: ... shortcuts`

- [ ] **Step 3: Implement `parakeet_dictation/shortcuts.py`**

```python
"""KDE shortcut strings ("Meta+Alt+D") — parsing, display and key capture.

kglobalaccel speaks QKeySequence ints: modifier bits high (Meta 0x10000000,
Alt 0x08000000, Ctrl 0x04000000, Shift 0x02000000), the key code low.  This
module is the one translation between that, the config's strings and the
key events the settings window captures.  No GTK, no D-Bus.
"""

import re

MODIFIERS = ("Meta", "Ctrl", "Alt", "Shift")
QT_MODIFIER_BITS = {
    "shift": 0x02000000,
    "ctrl": 0x04000000, "control": 0x04000000,
    "alt": 0x08000000,
    "meta": 0x10000000, "super": 0x10000000, "win": 0x10000000,
}
_BIT_TO_NAME = (("Meta", 0x10000000), ("Ctrl", 0x04000000),
                ("Alt", 0x08000000), ("Shift", 0x02000000))
_MOD_MASK = 0x1E000000

# Canonical key name -> Qt key code.  Canonical spelling is what the config
# holds and the UI shows.
_NAMED = {
    "Escape": 0x01000000, "Tab": 0x01000001, "Backtab": 0x01000002,
    "Backspace": 0x01000003, "Return": 0x01000004, "Enter": 0x01000005,
    "Insert": 0x01000006, "Delete": 0x01000007, "Pause": 0x01000008,
    "Print": 0x01000009, "SysReq": 0x0100000A, "Clear": 0x0100000B,
    "Home": 0x01000010, "End": 0x01000011, "Left": 0x01000012,
    "Up": 0x01000013, "Right": 0x01000014, "Down": 0x01000015,
    "PageUp": 0x01000016, "PageDown": 0x01000017, "CapsLock": 0x01000024,
    "NumLock": 0x01000025, "ScrollLock": 0x01000026, "Menu": 0x01000055,
    "Space": 0x20,
}
QT_NAMED_KEYS = {k.lower(): v for k, v in _NAMED.items()}
_CODE_TO_NAME = {v: k for k, v in _NAMED.items()}

# Gdk keyval names that differ from the canonical name.
_GDK_ALIASES = {
    "space": "Space", "Scroll_Lock": "ScrollLock", "Prior": "PageUp",
    "Next": "PageDown", "Page_Up": "PageUp", "Page_Down": "PageDown",
    "Caps_Lock": "CapsLock", "Num_Lock": "NumLock", "KP_Enter": "Enter",
    "ISO_Left_Tab": "Backtab", "Sys_Req": "SysReq", "BackSpace": "Backspace",
}
_LONE_MODIFIERS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "Hyper_L", "Hyper_R",
    "ISO_Level3_Shift", "ISO_Level5_Shift", "Caps_Lock",
}
# Keys that may be a shortcut on their own: nothing types them.
_BARE_OK = {"Insert", "Pause", "ScrollLock", "Menu", "Print"} | {f"F{n}" for n in range(1, 25)}


def qt_key_sequence(binding: str) -> int:
    """KDE shortcut string -> Qt key int.  Raises ValueError."""
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
    if len(key) == 1 and key.isascii() and key.isprintable():
        return value | ord(key.upper())
    raise ValueError(f"unknown key {key!r}")


def binding_from_qt(key: int) -> str:
    """Qt key int -> canonical KDE string; "" for 0 or an unknown code."""
    if not key:
        return ""
    mods = [name for name, bit in _BIT_TO_NAME if key & bit]
    code = key & ~_MOD_MASK
    if code in _CODE_TO_NAME:
        name = _CODE_TO_NAME[code]
    elif 0x01000030 <= code <= 0x01000052:
        name = f"F{code - 0x01000030 + 1}"
    elif 0x21 <= code <= 0x7E:
        name = chr(code).upper()
    else:
        return ""
    return "+".join([*mods, name])


def is_valid(binding: str) -> bool:
    if binding == "":
        return True
    if not isinstance(binding, str):
        return False
    try:
        qt_key_sequence(binding)
    except ValueError:
        return False
    return True


def has_modifier(binding: str) -> bool:
    try:
        return bool(qt_key_sequence(binding) & _MOD_MASK)
    except ValueError:
        return False


def display(binding: str) -> str:
    return binding or ""


def _canonical_key(keyname: str):
    name = _GDK_ALIASES.get(keyname, keyname)
    if name in _NAMED:
        return name
    m = re.fullmatch(r"F(\d{1,2})", name)
    if m and 1 <= int(m.group(1)) <= 35:
        return name
    if len(name) == 1 and name.isascii() and name.isprintable() and name != " ":
        return name.upper()
    return None


def capture(keyname: str, mods: frozenset) -> tuple:
    """One key-press of the shortcut-capture state machine.

    `keyname` is the Gdk keyval name, `mods` the held modifiers as a subset of
    MODIFIERS.  Returns (status, binding):
      wait   – a lone modifier went down; keep listening
      cancel – Escape with no modifiers
      clear  – Backspace with no modifiers (disable the action)
      done   – binding is the captured shortcut
      reject – not usable (a bare typing key, an unknown key)
    """
    if keyname in _LONE_MODIFIERS:
        return ("wait", "")
    if not mods and keyname == "Escape":
        return ("cancel", "")
    if not mods and keyname == "BackSpace":
        return ("clear", "")
    key = _canonical_key(keyname)
    if key is None:
        return ("reject", "")
    if not mods and key not in _BARE_OK:
        return ("reject", "")
    binding = "+".join([m for m in MODIFIERS if m in mods] + [key])
    if not is_valid(binding):
        return ("reject", "")
    return ("done", binding)
```

In `parakeet_dictation/hotkeys.py` delete the `QT_MODIFIER_BITS` / `QT_NAMED_KEYS` dicts and the `qt_key_sequence` function and the `import re` line, and add after the other imports:

```python
from .shortcuts import qt_key_sequence  # noqa: F401  (re-exported for tests)
```

- [ ] **Step 4: Run, expect PASS**

Run: `.venv/bin/python tests/test_settings.py 1 && .venv/bin/python tests/test_review.py 4`
Expected: `ALL CHECKS PASSED` twice.

- [ ] **Step 5: Commit**

```bash
git add parakeet_dictation/shortcuts.py parakeet_dictation/hotkeys.py tests/test_settings.py
git commit -m "shortcuts: one module for KDE shortcut strings, reverse mapping and key capture

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: i18n infrastructure

**Files:**
- Create: `parakeet_dictation/i18n.py`, `tools/update-translations.sh`, `parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po`, `.../parakeet-dictation.mo`
- Modify: `pyproject.toml` (package-data `locale/*/LC_MESSAGES/*.mo`)
- Test: `tests/test_settings.py` section 2

**Interfaces:**
- Produces: `i18n.setup(language: str) -> str` (returns the language actually loaded: `"uk"` or `"en"`), `i18n._(msg: str) -> str`, `i18n.ngettext(s: str, p: str, n: int) -> str`, `i18n.LOCALE_DIR: Path`, `i18n.DOMAIN = "parakeet-dictation"`. Every later task imports `from ..i18n import _` (or `from .i18n import _`).

- [ ] **Step 1: Failing test** — add to `tests/test_settings.py`:

```python
def s2_i18n():
    print("\n[2] i18n: explicit language, system fallback, missing catalogue")
    import os
    from parakeet_dictation import i18n
    got = i18n.setup("uk")
    check("uk loads", got == "uk", got)
    check("a known string is translated", i18n._("Settings") == "Налаштування", i18n._("Settings"))
    got = i18n.setup("en")
    check("en loads", got == "en" and i18n._("Settings") == "Settings")
    saved = {k: os.environ.get(k) for k in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ["LANG"] = "uk_UA.UTF-8"
        check("system follows LANG=uk_UA", i18n.setup("system") == "uk")
        os.environ["LANG"] = "de_DE.UTF-8"
        check("system with no catalogue falls back to en", i18n.setup("system") == "en"
              and i18n._("Settings") == "Settings")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    i18n.setup("en")
    po = i18n.LOCALE_DIR / "uk" / "LC_MESSAGES" / "parakeet-dictation.po"
    mo = po.with_suffix(".mo")
    check(".mo is not older than .po", mo.stat().st_mtime >= po.stat().st_mtime - 1)
    check("no fuzzy entries", "#, fuzzy" not in po.read_text().split("\n\n", 1)[1])
```

Register `2: s2_i18n` in `SECTIONS`. Run `.venv/bin/python tests/test_settings.py 2` → FAIL (no module).

- [ ] **Step 2: Implement `parakeet_dictation/i18n.py`**

```python
"""Interface translations (gettext).  Diagnostics are never translated."""

import gettext as _gettext
import os
from pathlib import Path

DOMAIN = "parakeet-dictation"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
_translation = _gettext.NullTranslations()


def _system_languages():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            return [part.split(".")[0].split("_")[0] for part in value.split(":") if part]
    return []


def setup(language: str = "system") -> str:
    """Load the catalogue for `language` ("system", "uk", "en").  Returns
    the language now in effect: "en" whenever no catalogue matches."""
    global _translation
    wanted = _system_languages() if language == "system" else [language]
    for lang in wanted:
        if lang == "en":
            break
        try:
            _translation = _gettext.translation(DOMAIN, LOCALE_DIR, languages=[lang])
            return lang
        except OSError:
            continue
    _translation = _gettext.NullTranslations()
    return "en"


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
```

- [ ] **Step 3: Seed the catalogue and the refresh script**

`tools/update-translations.sh` (chmod +x):

```bash
#!/usr/bin/env bash
# Refresh the Ukrainian catalogue: extract _() strings, merge, compile.
# Needs gettext (xgettext, msgmerge, msgfmt).  Commit the .po AND the .mo.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
pot=$(mktemp --suffix=.pot)
trap 'rm -f "$pot"' EXIT
find parakeet_dictation -name '*.py' | sort | xargs xgettext \
    --language=Python --keyword=_ --keyword=ngettext:1,2 --from-code=UTF-8 \
    --package-name=parakeet-dictation --output="$pot"
po=parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po
mkdir -p "$(dirname "$po")"
if [ -f "$po" ]; then
    msgmerge --quiet --update --no-fuzzy-matching --backup=none "$po" "$pot"
else
    msginit --no-translator --locale=uk_UA.UTF-8 --input="$pot" --output-file="$po"
fi
msgfmt --check --output-file="${po%.po}.mo" "$po"
untranslated=$(msgattrib --untranslated "$po" | grep -c '^msgid ' || true)
echo "untranslated entries (incl. header): $untranslated"
```

Create `parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po` with a header and one seed entry (Task 11 fills the rest):

```
msgid ""
msgstr ""
"Project-Id-Version: parakeet-dictation\n"
"Language: uk\n"
"MIME-Version: 1.0\n"
"Content-Type: text/plain; charset=UTF-8\n"
"Content-Transfer-Encoding: 8bit\n"
"Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\n"

msgid "Settings"
msgstr "Налаштування"
```

Compile: `msgfmt --check -o parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.mo parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po`

In `pyproject.toml`, extend the package-data list for `parakeet_dictation` with `"locale/*/LC_MESSAGES/*.mo"` (keep the existing entries).

- [ ] **Step 4: Run** `.venv/bin/python tests/test_settings.py 2` → PASS.

- [ ] **Step 5: Commit** (`git add parakeet_dictation/i18n.py parakeet_dictation/locale tools pyproject.toml tests/test_settings.py`, message `i18n: gettext setup with a packaged Ukrainian catalogue` + Co-Authored-By line).

---

### Task 3: Config — new fields, migration, enforce

**Files:**
- Modify: `parakeet_dictation/config.py`
- Modify tests: `tests/test_defects.py` (~lines 905-990), `tests/test_hold_take.py:290-300`, `tests/test_review.py` sections #4 (~494-527), #19 (~1358-1415), #20-ish around 1435 and 1464-1500, 1904, 1935
- Test: `tests/test_settings.py` section 3

**Interfaces:**
- Consumes: `shortcuts.is_valid`
- Produces: `AppConfig` without `hotkey_mode`, `hotkey_start`, `hotkey_stop`, `hotkey_pause`; with `hotkey_hold: str = "Insert"`, `hotkey_toggle: str = "Meta+Insert"`, `ui_language: str = "system"`. Diagnostics events `config_migrated` (key=, reason=) and `config_forced` reason `unparseable_binding` / `duplicate_binding`.

- [ ] **Step 1: Failing test** — add section 3:

```python
def s3_config_migration():
    print("\n[3] config: defaults, migration of the pynput era, enforce")
    import json, os, tempfile
    from parakeet_dictation import config as cfgmod
    d = cfgmod.AppConfig()
    check("defaults Insert / Meta+Insert / system",
          (d.hotkey_hold, d.hotkey_toggle, d.ui_language) == ("Insert", "Meta+Insert", "system"))
    for gone in ("hotkey_mode", "hotkey_start", "hotkey_stop", "hotkey_pause"):
        check(f"{gone} is gone", not hasattr(d, gone))
    events = []
    log = lambda ev, **kw: events.append((ev, kw))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"hotkey_mode": "toggle", "hotkey_toggle": "<ctrl>+0",
                                    "hotkey_start": "<ctrl>+9", "hotkey_pause": "<ctrl>+<alt>+0",
                                    "hotkey_hold": "Meta+Z", "insert_mode": "per_segment"}))
        saved = (cfgmod.CONFIG_FILE, cfgmod.CONFIG_DIR)
        cfgmod.CONFIG_FILE, cfgmod.CONFIG_DIR = path, Path(tmp)
        try:
            import io
            from contextlib import redirect_stderr
            with redirect_stderr(io.StringIO()):
                c = cfgmod.AppConfig.load(log)
                c.save(log)
        finally:
            cfgmod.CONFIG_FILE, cfgmod.CONFIG_DIR = saved
        check("pynput toggle -> Meta+Insert", c.hotkey_toggle == "Meta+Insert", c.hotkey_toggle)
        check("old default Meta+Z -> Insert", c.hotkey_hold == "Insert", c.hotkey_hold)
        check("per_segment survives (start/stop takes)", c.insert_mode == "per_segment")
        check("config_migrated logged", any(e == "config_migrated" for e, _ in events), str(events))
        on_disk = json.loads(path.read_text())
        check("dead keys dropped on save",
              not {"hotkey_mode", "hotkey_start", "hotkey_pause"} & set(on_disk), str(on_disk))
    c = cfgmod.AppConfig(hotkey_hold="Meta+Q", hotkey_toggle="Meta+Q")
    forced = c.enforce(lambda *a, **k: None)
    check("duplicate binding: toggle reset", c.hotkey_toggle == "Meta+Insert"
          and "hotkey_toggle" in forced, c.hotkey_toggle)
    c = cfgmod.AppConfig(hotkey_hold="Meta+Insert", hotkey_toggle="Meta+Insert")
    c.enforce(lambda *a, **k: None)
    check("duplicate of the default: toggle disabled", c.hotkey_toggle == "")
    c = cfgmod.AppConfig(hotkey_hold="Meta+", hotkey_toggle="")
    c.enforce(lambda *a, **k: None)
    check("unparseable hold reset, empty toggle kept", (c.hotkey_hold, c.hotkey_toggle) == ("Insert", ""))
    c = cfgmod.AppConfig(ui_language="de")
    c.enforce(lambda *a, **k: None)
    check("unknown ui_language reset", c.ui_language == "system")
```

Register `3: s3_config_migration`. Run → FAIL.

- [ ] **Step 2: Implement in `config.py`**

1. Add `from . import shortcuts` at the top (shortcuts imports nothing from the package, so no cycle).
2. Delete the fields `hotkey_mode`, `hotkey_start`, `hotkey_stop`, `hotkey_pause` and their comment block; replace the `hotkey_toggle` field and the `hotkey_hold` block with:

```python
    # Two kglobalaccel actions (hotkeys.KGlobalAccelHotkey), KDE notation.
    # A bare key for push-to-talk: no modifier can still be held when the
    # paste chord goes out.  "" disables an action.
    hotkey_hold: str = "Insert"
    hotkey_toggle: str = "Meta+Insert"
    # Interface language: "system" follows the locale; "uk"/"en" force it.
    ui_language: str = "system"
```

3. In `_ENUMS` remove `"hotkey_mode"` and add `"ui_language": ("system", "uk", "en"),`.
4. In `enforce()` delete the `hotkey_mode == "hold"` block and the pynput loop, and append before `return forced`:

```python
        for key in ("hotkey_hold", "hotkey_toggle"):
            if not shortcuts.is_valid(getattr(self, key)):
                setattr(self, key, defaults[key].default)
                forced.append(key)
                report("config_forced", key=key, value=defaults[key].default,
                       reason="unparseable_binding")
        if self.hotkey_toggle and self.hotkey_toggle == self.hotkey_hold:
            default = defaults["hotkey_toggle"].default
            self.hotkey_toggle = "" if default == self.hotkey_hold else default
            forced.append("hotkey_toggle")
            report("config_forced", key="hotkey_toggle", value=self.hotkey_toggle,
                   reason="duplicate_binding")
```

Update the `enforce()` docstring: drop the hold/per_segment paragraph.
5. Delete `_pynput_binding_ok`.
6. In `load()`, right after the `isinstance(data, dict)` check succeeds and before the kwargs loop, call `data = _migrate(data, report)` and add:

```python
_DEAD_KEYS = ("hotkey_mode", "hotkey_start", "hotkey_stop", "hotkey_pause")


def _migrate(data: dict, report) -> dict:
    """Bring a pre-2026-10-15 config to the two-shortcut model."""
    data = dict(data)
    for key in _DEAD_KEYS:
        if key in data:
            data.pop(key)
            report("config_migrated", key=key, reason="removed")
    toggle = data.get("hotkey_toggle")
    if isinstance(toggle, str) and "<" in toggle:          # pynput syntax
        data.pop("hotkey_toggle")
        report("config_migrated", key="hotkey_toggle", reason="pynput_syntax")
    if data.get("hotkey_hold") == "Meta+Z":                 # the old default
        data["hotkey_hold"] = "Insert"
        report("config_migrated", key="hotkey_hold", reason="old_default")
    return data
```

`config_migrated` is a `log` event, not an error: `_reporter(log)` prints to stderr too — acceptable (one line per migrated key, once).

- [ ] **Step 3: Update the old tests to the new model**

- `tests/test_defects.py`: delete the checks that assert `hotkey_mode` defaults / `hold` + `per_segment` coercion and the `SettingsDialog` block (~905-990); keep any check that is still meaningful by rewriting it to `AppConfig(insert_mode="per_segment")` staying `per_segment` after `enforce()`.
- `tests/test_hold_take.py:295`: replace `da_config.AppConfig(hotkey_mode=... , insert_mode=...)` with `da_config.AppConfig(insert_mode=insert_mode)`.
- `tests/test_review.py`: in #4, #19 and the `HotkeyManager` sections remove the `pynput` stubbing and every `hotkey_mode=`/`hotkey_start/stop/pause=` argument; #19's pynput assertions are replaced in Task 4. At ~1904/1935 drop `hotkey_mode` from the bad/good dicts. Mark the #19 section body `print("  (moved to test_settings #4)")` and delete its checks.

- [ ] **Step 4: Run all suites** (gate command from Global Constraints) → all PASS. If `test_review` #4 needs `HotkeyManager` changes, leave it for Task 4 only if it already fails *before* this task; otherwise fix the arguments here.

- [ ] **Step 5: Commit** — `config: Insert / Meta+Insert defaults, ui_language, migrate the pynput-era keys`.

---

### Task 4: Hotkeys — two kglobalaccel actions, no pynput

**Files:**
- Modify: `parakeet_dictation/hotkeys.py`
- Test: `tests/test_settings.py` section 4 (fake bus like `tests/test_review.py:fake_hotkeys_gio`)

**Interfaces:**
- Consumes: `shortcuts.qt_key_sequence`, `shortcuts.binding_from_qt`, `AppConfig.hotkey_hold/hotkey_toggle`
- Produces:
  - `KGlobalAccelHotkey(binding, action, description, on_press, on_release=None)`; `ACTION` class attr removed; `.action` instance attr.
  - `HotkeyManager(config, on_hold_press, on_hold_release, on_toggle, on_binding_changed=None)`; `.start() -> str`; `.stop()`; `.rebuild(config) -> str`.
  - `on_binding_changed(key: str, binding: str)` is called (on the GLib main loop) when KDE reports a new binding for one of our actions; `key` is `"hotkey_hold"` or `"hotkey_toggle"`.
  - action ids: `"push-to-talk"` ("Hold to talk"), `"toggle"` ("Start / stop dictation").

- [ ] **Step 1: Failing test** — section 4 builds a `FakeBus` recording `call_sync` calls and signal subscriptions:

```python
class FakeBus:
    def __init__(self):
        self.calls, self.subs, self.unsubs = [], {}, []
        self._next = 1

    def signal_subscribe(self, _svc, _iface, signal, _path, _arg0, _flags, handler):
        sid = self._next
        self._next += 1
        self.subs[sid] = (signal, handler)
        return sid

    def signal_unsubscribe(self, sid):
        self.unsubs.append(sid)
        self.subs.pop(sid, None)


def s4_hotkeys_two_actions():
    print("\n[4] hotkeys: hold + toggle registered with kglobalaccel, no pynput")
    from types import SimpleNamespace
    from gi.repository import Gio, GLib
    from parakeet_dictation import hotkeys as hk, config as cfgmod, shortcuts as sc
    bus = FakeBus()
    calls = []

    def fake_call(_bus, _svc, path, iface, method, params, reply_type=None, _t=0):
        calls.append((method, params.unpack() if params is not None else None))
        if method == "globalShortcutAvailable":
            return GLib.Variant("(b)", (True,))
        if method == "getComponent":
            return GLib.Variant("(o)", ("/component/parakeet_dictation",))
        if method == "setShortcutKeys":
            return GLib.Variant("(a(ai))", ([],))
        return None

    saved = (hk.Gio, hk.call_sync)
    hk.Gio = SimpleNamespace(
        bus_get_sync=lambda *_a: bus, BusType=Gio.BusType, DBusSignalFlags=Gio.DBusSignalFlags,
        BusNameWatcherFlags=Gio.BusNameWatcherFlags,
        bus_watch_name_on_connection=lambda *a: 7, bus_unwatch_name=lambda *_a: None)
    hk.call_sync = fake_call
    pressed, released, toggled, changed = [], [], [], []
    try:
        mgr = hk.HotkeyManager(cfgmod.AppConfig(), on_hold_press=lambda: pressed.append(1),
                               on_hold_release=lambda: released.append(1),
                               on_toggle=lambda: toggled.append(1),
                               on_binding_changed=lambda k, b: changed.append((k, b)))
        status = mgr.start()
        check("start() is clean", status == "", status)
        regs = [p[0][1] for m, p in calls if m == "doRegister"]
        check("both actions registered", regs == ["push-to-talk", "toggle"], str(regs))
        keys = [p[1][0][0][0] for m, p in calls if m == "setShortcutKeys"]
        check("Insert and Meta+Insert bound",
              keys == [sc.qt_key_sequence("Insert"), sc.qt_key_sequence("Meta+Insert")], str(keys))
        press = [h for s, h in bus.subs.values() if s == "globalShortcutPressed"]
        for h in press:
            h(None, None, None, None, None, GLib.Variant("(ssx)", ("parakeet-dictation", "toggle", 0)))
            h(None, None, None, None, None, GLib.Variant("(ssx)", ("parakeet-dictation", "push-to-talk", 0)))
        check("toggle press -> on_toggle once", len(toggled) == 1, str(toggled))
        check("hold press -> on_hold_press once", len(pressed) == 1, str(pressed))
        changed_handlers = [h for s, h in bus.subs.values() if s == "yourShortcutsChanged"]
        check("listens for KDE-side changes", len(changed_handlers) == 1)
        changed_handlers[0](None, None, None, None, None, GLib.Variant(
            "(asa(ai))", (["parakeet-dictation", "toggle", "Parakeet Dictation", "x"],
                          [([sc.qt_key_sequence("Meta+Alt+D")],)])))
        check("KDE change reported as hotkey_toggle=Meta+Alt+D",
              changed == [("hotkey_toggle", "Meta+Alt+D")], str(changed))
        mgr.stop()
        calls.clear()
        status = mgr.rebuild(cfgmod.AppConfig(hotkey_toggle=""))
        regs = [p[0][1] for m, p in calls if m == "doRegister"]
        check("empty toggle is not registered", regs == ["push-to-talk"], str(regs))
        check("no pynput import", "pynput" not in sys.modules)
    finally:
        hk.Gio, hk.call_sync = saved
```

Register `4: s4_hotkeys_two_actions`. Run → FAIL.

- [ ] **Step 2: Implement**

In `KGlobalAccelHotkey`:
- `__init__(self, binding, action, description, on_press, on_release=None)`; store `self.action = action`; `self._action_id = [self.COMPONENT, action, APP_NAME, description]`; delete the `ACTION` class attribute.
- `_handle_press` / `_handle_release` compare against `self.action`; `_handle_release` returns early when `self._on_release is None`.
- Subscribe the release signal only when `on_release` is given.
- Make `call_sync` reachable as a module global used via `call_sync(...)` (it already is; tests patch `hk.call_sync`).
- Change the `hold_hotkey_*` diagnostics events to carry `action=self.action`.

Replace `HotkeyManager` entirely:

```python
class HotkeyManager:
    """The app's two global shortcuts, both registered with kglobalaccel.

    pynput is gone: on KDE Wayland it could not grab keys outside the app's
    own windows, so its Toggle/Start/Stop/Pause bindings never fired.
    """

    ACTIONS = (
        # (config key, action id, description shown in System Settings)
        ("hotkey_hold", "push-to-talk", "Hold to talk"),
        ("hotkey_toggle", "toggle", "Start / stop dictation"),
    )

    def __init__(self, config: AppConfig, on_hold_press, on_hold_release, on_toggle,
                 on_binding_changed=None):
        self._config = config
        self._callbacks = {
            "push-to-talk": (on_hold_press, on_hold_release),
            "toggle": (on_toggle, None),
        }
        self._on_binding_changed = on_binding_changed
        self._hotkeys = []
        self._changed_sub = 0

    def start(self) -> str:
        problems = []
        for key, action, description in self.ACTIONS:
            binding = getattr(self._config, key)
            if not binding:
                continue
            press, release = self._callbacks[action]
            hotkey = KGlobalAccelHotkey(binding, action, description, press, release)
            try:
                owner = hotkey.register()
            except (GLib.Error, ValueError) as e:
                DIAG.log("hotkey_failed", action=action, err=type(e).__name__)
                problems.append(_("{binding} could not be registered: {error}")
                                .format(binding=binding, error=e))
                continue
            self._hotkeys.append(hotkey)
            if owner:
                DIAG.log("hotkey_conflict", action=action, owner=owner)
                problems.append(_("{binding} is already used by {owner} — change it in "
                                  "Settings or in System Settings > Shortcuts")
                                .format(binding=binding, owner=owner))
        self._watch_changes()
        return "; ".join(problems)

    def _watch_changes(self):
        if self._changed_sub or not self._hotkeys:
            return
        bus = self._hotkeys[0]._bus
        self._changed_bus = bus
        self._changed_sub = bus.signal_subscribe(
            KGlobalAccelHotkey.SERVICE, KGlobalAccelHotkey.ROOT_IFACE,
            "yourShortcutsChanged", KGlobalAccelHotkey.ROOT_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_shortcuts_changed)

    def _on_shortcuts_changed(self, _conn, _sender, _path, _iface, _signal, params):
        action_id, keys = params.unpack()
        if len(action_id) < 2 or action_id[0] != KGlobalAccelHotkey.COMPONENT:
            return
        config_key = {a: k for k, a, _d in self.ACTIONS}.get(action_id[1])
        if config_key is None:
            return
        # a(ai): a list of 1-tuples, each holding the key ints of one sequence
        first = keys[0][0][0] if keys and keys[0] and keys[0][0] else 0
        binding = binding_from_qt(first)
        if binding == getattr(self._config, config_key):
            return
        DIAG.log("hotkey_changed_in_kde", action=action_id[1])
        if self._on_binding_changed:
            self._on_binding_changed(config_key, binding)

    def stop(self):
        if self._changed_sub:
            self._changed_bus.signal_unsubscribe(self._changed_sub)
            self._changed_sub = 0
        for hotkey in self._hotkeys:
            hotkey.unregister()
        self._hotkeys = []

    def rebuild(self, config: AppConfig) -> str:
        self.stop()
        self._config = config
        return self.start()
```

Verify the signal on the live bus before relying on it: `gdbus introspect --session --dest org.kde.kglobalaccel --object-path /kglobalaccel | grep -A2 yourShortcutsChanged` must show `yourShortcutsChanged(as actionId, a(ai) newKeys)`; if the signature differs, adapt the unpacking and the test's Variant to it.

Imports at the top of `hotkeys.py`: `from .shortcuts import binding_from_qt, qt_key_sequence` and `from .i18n import _`. Update the module docstring (no pynput). Remove the `bindings`/`keyboard` code. Keep `KGlobalAccelHotkey`'s existing re-registration on kglobalaccel restart.

- [ ] **Step 3: Drop pynput from dependencies** — remove `pynput` from `pyproject.toml` `dependencies` and `requirements.txt`; remove `'python-pynput'` from `packaging/PKGBUILD` `depends` and regenerate `packaging/.SRCINFO` (`cd packaging && makepkg --printsrcinfo > .SRCINFO`). Update the PKGBUILD header comment (delete the `python-pynput AUR` line).

- [ ] **Step 4: Fix `app.py` call site** (minimal, Task 10 rewires fully): construct `HotkeyManager(config, on_hold_press=..., on_hold_release=..., on_toggle=lambda: (controller.toggle(hold_off=shortcuts.has_modifier(config.hotkey_toggle)), tray and tray.update_ui()))`; remove `on_start/on_stop/on_pause`. Replace the `if config.hotkey_mode == ...` startup-description block with `mode_desc = f"Hold {config.hotkey_hold} to talk, {config.hotkey_toggle or 'no'} start/stop shortcut"`. (`controller.toggle(hold_off=)` arrives in Task 5; until then call `controller.toggle()`.)

- [ ] **Step 5: Run all suites** → PASS (update `tests/test_review.py` #4 and the `HotkeyManager` section near 1464 to the new constructor; their assertion — one press/release subscription pair per action after `rebuild()` — still holds: assert exactly one `globalShortcutPressed` subscription per registered action).

- [ ] **Step 6: Commit** — `hotkeys: hold and start/stop as two kglobalaccel actions; drop pynput`.

---

### Task 5: Controller — take kind and the modifier-only hold-off

**Files:**
- Modify: `parakeet_dictation/controller.py`
- Test: `tests/test_settings.py` section 5 (uses `make_controller`-style stub; copy `StubEngine`/`FakeOverlay` from `tests/test_review.py:200-250`)

**Interfaces:**
- Consumes: `shortcuts.has_modifier`
- Produces: `DictationController.start(kind: str = "toggle")`, `.stop(hold_off: bool = True)`, `.toggle(hold_off: bool = True)`; `hold_press()` starts with `kind="hold"`; attribute `_take_kind`; `_insert_at_end` = `self._take_kind == "hold" or self._config.insert_mode == "end_of_take"`; `_release_t` set only when a hold-off applies.

- [ ] **Step 1: Failing test**

```python
def s5_controller_take_kind():
    print("\n[5] controller: hold takes insert at the end; hold-off only after a modifier")
    import io
    from contextlib import redirect_stderr
    from parakeet_dictation import config as cfgmod, controller as ctlmod

    class Eng:
        running = paused = False
        recognizer_key = ("desktop", 4, "en")
        def start(self): pass
        def stop(self): self.running = False
        def stop_async(self, *a, **k): self.running = False
        def wait_drained(self, *a, **k): return True
        @property
        def is_running(self): return self.running
        @property
        def is_paused(self): return self.paused

    def make(**kw):
        with redirect_stderr(io.StringIO()):
            ctl = ctlmod.DictationController(cfgmod.AppConfig(**kw))
        ctl._engine = Eng()
        return ctl

    ctl = make(insert_mode="per_segment")
    ctl.hold_press()
    check("hold take inserts at end even with per_segment", ctl._insert_at_end)
    ctl = make(insert_mode="per_segment")
    ctl.start()
    check("toggle take honours per_segment", not ctl._insert_at_end)
    ctl = make(hotkey_hold="Insert")
    ctl.hold_press(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.hold_release()
    check("bare-key release: no hold-off", ctl._release_t == 0.0, str(ctl._release_t))
    ctl = make(hotkey_hold="Meta+Z")
    ctl.hold_press(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.hold_release()
    check("modifier hold release: hold-off armed", ctl._release_t > 0.0)
    ctl = make()
    ctl.start(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.stop(hold_off=False)
    check("tray stop: no hold-off", ctl._release_t == 0.0)
    ctl = make()
    ctl.start(); ctl._engine.running = True; ctl._on_capture_start()
    ctl.stop()
    check("signal/shortcut stop: hold-off armed", ctl._release_t > 0.0)
```

Register `5`. Run → FAIL (`start()` takes no `kind`, `stop()` no `hold_off`). If the controller calls engine methods the small `Eng` stub lacks, copy `StubEngine` and `FakeOverlay` from `tests/test_review.py` (~lines 200-250) instead of extending `Eng` ad hoc.

- [ ] **Step 2: Implement**

- In `__init__`: `self._take_kind = "toggle"`.
- `start(self, kind: str = "toggle")`: set `self._take_kind = kind` right after `self._take_seq += 1`.
- `hold_press()`: wherever it calls `self.start()` (and the queued-press path that starts the next take), call `self.start(kind="hold")`.
- `_insert_at_end`:

```python
    @property
    def _insert_at_end(self) -> bool:
        """Push-to-talk always holds text back to the release: a chord sent
        while the key is down is reported by kglobalaccel as its release.
        A start/stop take follows insert_mode."""
        return self._take_kind == "hold" or self._config.insert_mode == "end_of_take"
```

- `stop(self, hold_off: bool = True)`: replace `self._release_t = time.monotonic()` with `self._release_t = time.monotonic() if hold_off else 0.0` and update its comment: "A tray click leaves no key held; a shortcut or SIGUSR1 may."
- `toggle(self, hold_off: bool = True)`: pass `hold_off` to `self.stop(...)`.
- `hold_release()`: replace `self._release_t = time.monotonic()` with

```python
            # Only a binding with a modifier can leave a key physically down
            # when the chord goes out; a bare Insert release cannot.
            self._release_t = (time.monotonic()
                               if shortcuts.has_modifier(self._config.hotkey_hold) else 0.0)
```

- Add `from . import shortcuts` to the imports. Update the `take_insert` diag `mode=` field to `mode=("end_of_take" if self._insert_at_end else "per_segment")`.

- [ ] **Step 3: Run all suites** → PASS. `tests/test_hold_take.py` section [7] (hold-off) uses `Meta+Z`-style holds: if it builds a config with the new default `Insert`, pass `hotkey_hold="Meta+Z"` explicitly there so it still measures the delayed path.

- [ ] **Step 4: Commit** — `controller: push-to-talk takes always insert at release; hold-off only after a modifier`.

---

### Task 6: Tray menu model (pure)

**Files:**
- Create: `parakeet_dictation/ui/menu_model.py`
- Test: `tests/test_settings.py` section 6

**Interfaces:**
- Produces:

```python
@dataclass
class Item:
    kind: str                 # "action" | "radio" | "submenu" | "separator"
    label: str = ""
    accel: str = ""           # shown right-aligned, KDE notation
    action: str = ""          # "toggle" | "pause" | "resume" | "settings" | "about" | "quit"
                              # | "mic:<name or ''>" | "model:<id>" | "download_models"
    active: bool = False      # radio state
    children: list = field(default_factory=list)

def build_menu(state: str, config, profiles: dict, downloaded: set, devices: list) -> list
def tooltip(state: str, config) -> str
```

`state` ∈ `{"idle", "recording", "paused"}`; `devices` = list of input device names (strings); `downloaded` = set of profile ids on disk.

- [ ] **Step 1: Failing test**

```python
def s6_menu_model():
    print("\n[6] tray menu model")
    from parakeet_dictation import config as cfgmod, i18n
    from parakeet_dictation.ui import menu_model as mm
    i18n.setup("en")
    cfg = cfgmod.AppConfig(audio_device="WM105 Keyboard", model_profile="desktop")
    profiles = {"desktop": {"name": "Parakeet TDT 0.6B v3 (int8)"},
                "laptop": {"name": "Canary 180M Flash (int8)"},
                "streaming": {"name": "Nemotron Streaming 0.6B (int8)"}}
    items = mm.build_menu("idle", cfg, profiles, {"desktop", "laptop"}, ["WM105 Keyboard", "Built-in"])
    labels = [i.label for i in items if i.kind != "separator"]
    check("idle top-level order", labels == ["Start Dictation", "Microphone", "Model",
                                             "Settings…", "About", "Quit"], str(labels))
    check("start shows Meta+Insert", items[0].accel == "Meta+Insert")
    check("no disabled/status rows", all(i.kind in ("action", "submenu", "separator") for i in items))
    mic = next(i for i in items if i.label == "Microphone")
    check("mic: default + devices, saved one active",
          [(c.label, c.active) for c in mic.children] ==
          [("System default", False), ("WM105 Keyboard", True), ("Built-in", False)])
    model = next(i for i in items if i.label == "Model")
    check("model: only downloaded + Download More…",
          [c.label for c in model.children if c.kind != "separator"] ==
          ["Parakeet TDT 0.6B v3 (int8)", "Canary 180M Flash (int8)", "Download More…"])
    rec = mm.build_menu("recording", cfg, profiles, {"desktop"}, [])
    check("recording: Stop + Pause",
          [i.label for i in rec[:2]] == ["Stop Dictation", "Pause"]
          and [i.action for i in rec[:2]] == ["toggle", "pause"])
    pau = mm.build_menu("paused", cfg, profiles, {"desktop"}, [])
    check("paused: Stop + Resume", [i.label for i in pau[:2]] == ["Stop Dictation", "Resume"])
    cfg2 = cfgmod.AppConfig(hotkey_toggle="")
    check("no toggle binding -> no accel", mm.build_menu("idle", cfg2, profiles, {"desktop"}, [])[0].accel == "")
    gone = cfgmod.AppConfig(audio_device="USB Mic")
    mic = next(i for i in mm.build_menu("idle", gone, profiles, {"desktop"}, ["Built-in"]) if i.label == "Microphone")
    check("disconnected saved mic still listed and active",
          ("USB Mic (not connected)", True) in [(c.label, c.active) for c in mic.children])
    check("tooltip idle names the hold key",
          mm.tooltip("idle", cfg) == "Parakeet Dictation — ready · hold Insert to talk", mm.tooltip("idle", cfg))
    i18n.setup("uk")
    check("labels are translated", mm.build_menu("idle", cfg, profiles, {"desktop"}, [])[0].label
          == "Почати диктування")
    i18n.setup("en")
```

Register `6`. Run → FAIL.

- [ ] **Step 2: Implement `parakeet_dictation/ui/menu_model.py`**

```python
"""The tray menu as data, so its contents are testable without a display."""

from dataclasses import dataclass, field

from ..config import APP_NAME
from ..i18n import _


@dataclass
class Item:
    kind: str
    label: str = ""
    accel: str = ""
    action: str = ""
    active: bool = False
    children: list = field(default_factory=list)


SEP = Item("separator")


def _mic_items(config, devices):
    saved = config.audio_device
    items = [Item("radio", _("System default"), action="mic:", active=not saved)]
    for name in devices:
        items.append(Item("radio", name, action=f"mic:{name}", active=(name == saved)))
    if saved and saved not in devices and not saved.isdigit():
        items.append(Item("radio", _("{name} (not connected)").format(name=saved),
                          action=f"mic:{saved}", active=True))
    return items


def _model_items(config, profiles, downloaded):
    items = [Item("radio", data.get("name", pid), action=f"model:{pid}",
                  active=(pid == config.model_profile))
             for pid, data in profiles.items() if pid in downloaded]
    if len(downloaded) < len(profiles):
        items += [SEP, Item("action", _("Download More…"), action="download_models")]
    return items


def build_menu(state, config, profiles, downloaded, devices):
    if state == "idle":
        top = [Item("action", _("Start Dictation"), accel=config.hotkey_toggle, action="toggle")]
    else:
        top = [Item("action", _("Stop Dictation"), accel=config.hotkey_toggle, action="toggle"),
               Item("action", _("Resume") if state == "paused" else _("Pause"),
                    action="resume" if state == "paused" else "pause")]
    return top + [
        SEP,
        Item("submenu", _("Microphone"), children=_mic_items(config, devices)),
        Item("submenu", _("Model"), children=_model_items(config, profiles, downloaded)),
        SEP,
        Item("action", _("Settings…"), action="settings"),
        Item("action", _("About"), action="about"),
        Item("action", _("Quit"), action="quit"),
    ]


def tooltip(state, config):
    if state == "recording":
        status = _("listening")
    elif state == "paused":
        status = _("paused")
    elif config.hotkey_hold:
        status = _("ready · hold {key} to talk").format(key=config.hotkey_hold)
    else:
        status = _("ready")
    return f"{APP_NAME} — {status}"
```

Add the Ukrainian entries for every string above to the `.po` now (msgid/msgstr pairs), recompile the `.mo` with `msgfmt --check -o ... .po`:

| msgid | msgstr |
|---|---|
| System default | Типовий системний |
| {name} (not connected) | {name} (не під'єднано) |
| Download More… | Завантажити інші… |
| Start Dictation | Почати диктування |
| Stop Dictation | Зупинити диктування |
| Resume | Продовжити |
| Pause | Пауза |
| Microphone | Мікрофон |
| Model | Модель |
| Settings… | Налаштування… |
| About | Про програму |
| Quit | Вийти |
| listening | слухаю |
| paused | на паузі |
| ready · hold {key} to talk | готово · утримуйте {key}, щоб говорити |
| ready | готово |

- [ ] **Step 3: Run** section 6 and section 2 → PASS.
- [ ] **Step 4: Commit** — `tray: menu model as data (no status rows, radio choices, real shortcut)`.

---

### Task 7: Settings draft model and autostart (pure)

**Files:**
- Create: `parakeet_dictation/ui/settings/__init__.py` (empty for now: `"""Settings window package."""`), `parakeet_dictation/ui/settings/model.py`, `parakeet_dictation/autostart.py`
- Test: `tests/test_settings.py` sections 7 and 8

**Interfaces:**
- Produces:

```python
PAGE_KEYS: dict[str, tuple[str, ...]]   # page id -> config keys it edits
class SettingsDraft:
    def __init__(self, applied: AppConfig)
    values: dict                        # key -> draft value
    def set(self, key: str, value) -> None
    def get(self, key: str)
    @property dirty -> bool             # any key differs from the snapshot taken at init/after commit
    def reset_page(self, page: str) -> None   # draft keys of that page := AppConfig() defaults
    def commit(self, current: AppConfig) -> AppConfig  # copy of `current` with only changed keys applied; re-snapshots
    def reload(self, applied: AppConfig) -> None       # after Apply: snapshot + values from applied
```

```python
# autostart.py
def desktop_path() -> Path        # $XDG_CONFIG_HOME/autostart/parakeet-dictation.desktop
def exec_line() -> str            # "<checkout>/start.sh" when APP_DIR/"start.sh" exists, else "parakeet-dictation"
def is_enabled() -> bool          # file exists and has no Hidden=true / X-GNOME-Autostart-enabled=false
def set_enabled(on: bool) -> None # write or delete; raises OSError
```

`PAGE_KEYS` (exact):

```python
PAGE_KEYS = {
    "general": ("hotkey_hold", "hotkey_toggle", "audio_device", "beep_volume",
                "night_mode", "night_start", "night_end", "ui_language"),
    "recognition": ("model_profile", "language", "preview", "filter_fillers"),
    "output": ("insert_mode", "keep_on_clipboard", "overlay", "overlay_position",
               "tray_icon_style"),
    "advanced": ("num_threads", "vad_min_silence", "paste_min_delay_ms",
                 "paste_transport", "terminal_window_classes", "diagnostics"),
    "about": (),
}
```

"Beep at start and end" maps to `beep_volume > 0`; the checkbox off stores `0.0`, on restores the slider value (default 0.5). Autostart is **not** a config key: the window calls `autostart.set_enabled` on Apply when the checkbox changed.

- [ ] **Step 1: Failing tests**

```python
def s7_settings_draft():
    print("\n[7] settings draft: dirty, page defaults, diff-merge commit")
    from parakeet_dictation import config as cfgmod
    from parakeet_dictation.ui.settings.model import SettingsDraft, PAGE_KEYS
    applied = cfgmod.AppConfig(num_threads=2, overlay_position="top")
    d = SettingsDraft(applied)
    check("fresh draft is clean", not d.dirty)
    d.set("overlay_position", "bottom")
    check("edit makes it dirty", d.dirty)
    d.set("overlay_position", "top")
    check("edit back is clean again", not d.dirty)
    d.set("num_threads", 6)
    d.set("overlay_position", "bottom")
    d.reset_page("advanced")
    check("Defaults resets only the page", d.get("num_threads") == cfgmod.AppConfig().num_threads
          and d.get("overlay_position") == "bottom")
    current = cfgmod.AppConfig(num_threads=2, overlay_position="top", model_profile="laptop")
    out = d.commit(current)
    check("commit keeps a change made elsewhere (tray model switch)",
          out.model_profile == "laptop", out.model_profile)
    check("commit applies the window's edits", out.overlay_position == "bottom"
          and out.num_threads == cfgmod.AppConfig().num_threads)
    check("commit returns a copy", out is not current)
    check("clean after commit", not d.dirty)
    every = {k for keys in PAGE_KEYS.values() for k in keys}
    check("every page key is a config field", every <= set(cfgmod.AppConfig.__dataclass_fields__), str(every - set(cfgmod.AppConfig.__dataclass_fields__)))


def s8_autostart():
    print("\n[8] autostart file")
    import os, tempfile
    from parakeet_dictation import autostart
    saved = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            check("off by default", not autostart.is_enabled())
            autostart.set_enabled(True)
            text = autostart.desktop_path().read_text()
            check("written under XDG_CONFIG_HOME", str(autostart.desktop_path()).startswith(tmp))
            check("Exec line present", f"Exec={autostart.exec_line()}" in text, text)
            check("now enabled", autostart.is_enabled())
            autostart.desktop_path().write_text(text + "Hidden=true\n")
            check("Hidden=true counts as off", not autostart.is_enabled())
            autostart.set_enabled(False)
            check("removed", not autostart.desktop_path().exists())
        finally:
            if saved is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = saved
```

Register `7`, `8`. Run → FAIL.

- [ ] **Step 2: Implement `ui/settings/model.py`**

```python
"""What the settings window edits — a draft over AppConfig, no GTK."""

import copy

from ...config import AppConfig

PAGE_KEYS = {  # (as listed in the Interfaces block above)
    "general": ("hotkey_hold", "hotkey_toggle", "audio_device", "beep_volume",
                "night_mode", "night_start", "night_end", "ui_language"),
    "recognition": ("model_profile", "language", "preview", "filter_fillers"),
    "output": ("insert_mode", "keep_on_clipboard", "overlay", "overlay_position",
               "tray_icon_style"),
    "advanced": ("num_threads", "vad_min_silence", "paste_min_delay_ms",
                 "paste_transport", "terminal_window_classes", "diagnostics"),
    "about": (),
}
_ALL_KEYS = tuple(k for keys in PAGE_KEYS.values() for k in keys)


class SettingsDraft:
    def __init__(self, applied: AppConfig):
        self.reload(applied)

    def reload(self, applied: AppConfig):
        self._snapshot = {k: copy.deepcopy(getattr(applied, k)) for k in _ALL_KEYS}
        self.values = copy.deepcopy(self._snapshot)

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        if key not in self.values:
            raise KeyError(key)
        self.values[key] = value

    @property
    def dirty(self) -> bool:
        return self.values != self._snapshot

    def changed_keys(self):
        return [k for k in _ALL_KEYS if self.values[k] != self._snapshot[k]]

    def reset_page(self, page: str):
        defaults = AppConfig()
        for key in PAGE_KEYS[page]:
            self.values[key] = copy.deepcopy(getattr(defaults, key))

    def commit(self, current: AppConfig) -> AppConfig:
        out = copy.deepcopy(current)
        for key in self.changed_keys():
            setattr(out, key, copy.deepcopy(self.values[key]))
        self.reload(out)
        return out
```

- [ ] **Step 3: Implement `parakeet_dictation/autostart.py`**

```python
"""Start at login: ~/.config/autostart/parakeet-dictation.desktop."""

import os
from pathlib import Path

from .config import APP_DIR, APP_ID, APP_NAME


def desktop_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / "autostart" / f"{APP_ID}.desktop"


def exec_line() -> str:
    start = APP_DIR / "start.sh"
    return str(start) if start.is_file() else APP_ID


def is_enabled() -> bool:
    path = desktop_path()
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    return "Hidden=true" not in text and "X-GNOME-Autostart-enabled=false" not in text


def set_enabled(on: bool) -> None:
    path = desktop_path()
    if not on:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Offline push-to-talk dictation\n"
        f"Exec={exec_line()}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n")
```

- [ ] **Step 4: Run** sections 7, 8 → PASS. **Step 5: Commit** — `settings: draft model with per-page defaults and diff-merge apply; autostart file`.

---

### Task 8: Settings widgets

**Files:**
- Create: `parakeet_dictation/ui/settings/widgets.py`
- Test: `tests/test_settings.py` section 9 (GTK offscreen)

**Interfaces:**
- Consumes: `shortcuts.capture`, `shortcuts.MODIFIERS`, `i18n._`, `audio.list_input_devices`
- Produces:
  - `class Section(Gtk.Box)`: `Section(title: str)`; `.row(label: str | None, widget, hint: str | None = None)`; `.add_full(widget)`.
  - `class ShortcutButton(Gtk.Button)`: `ShortcutButton(binding: str, on_change: Callable[[str], None])`; `.binding` property; `.set_binding(str)`; handles a key event through `.feed(keyname: str, mods: frozenset) -> str` (returns the capture status; used by the real `key-press-event` handler and by tests).
  - `def device_choices(saved: str, names: list[str]) -> list[tuple[str, str]]` → `(id, label)` pairs: `("", _("System default"))`, then each name, then `(saved, _("{name} (not connected)"))` when saved is non-empty, non-numeric and absent.
  - `def gdk_mods(state) -> frozenset` maps `Gdk.ModifierType` to `MODIFIERS` names (SUPER/META→Meta, CONTROL→Ctrl, MOD1→Alt, SHIFT→Shift).

- [ ] **Step 1: Failing test**

```python
def s9_widgets():
    print("\n[9] widgets: shortcut button state machine, device choices (GTK)")
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    from parakeet_dictation import i18n
    from parakeet_dictation.ui.settings import widgets as w
    i18n.setup("en")
    got = []
    b = w.ShortcutButton("Insert", got.append)
    check("shows its binding", b.get_label() == "Insert")
    b.clicked()
    check("listening prompt", b.get_label() == "Press a shortcut…", b.get_label())
    check("lone Meta keeps listening", b.feed("Super_L", frozenset({"Meta"})) == "wait")
    check("Meta+Insert accepted", b.feed("Insert", frozenset({"Meta"})) == "done"
          and got == ["Meta+Insert"] and b.get_label() == "Meta+Insert", str(got))
    b.clicked()
    check("bare letter rejected, still listening", b.feed("a", frozenset()) == "reject"
          and b.get_label() == "Press a shortcut…")
    check("Escape restores", b.feed("Escape", frozenset()) == "cancel" and b.get_label() == "Meta+Insert")
    b.clicked()
    b.feed("BackSpace", frozenset())
    check("Backspace disables", got[-1] == "" and b.get_label() == "None", b.get_label())
    ch = w.device_choices("USB Mic", ["Built-in"])
    check("device choices keep a missing saved mic",
          ch == [("", "System default"), ("Built-in", "Built-in"), ("USB Mic", "USB Mic (not connected)")], str(ch))
    check("numeric legacy value is not shown as a name", w.device_choices("7", ["A"])[-1] == ("A", "A"))
```

Register `9`. Run → FAIL.

- [ ] **Step 2: Implement `widgets.py`**

```python
"""Building blocks shared by the settings pages."""

from gi.repository import Gdk, GLib, Gtk

from ... import shortcuts
from ...i18n import _


class Section(Gtk.Box):
    """A bold heading and a two-column grid: right-aligned labels, controls."""

    def __init__(self, title: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(xalign=0.0)
        heading.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        heading.set_margin_top(14)
        self.pack_start(heading, False, False, 0)
        self._grid = Gtk.Grid(column_spacing=14, row_spacing=8, margin_start=12)
        self.pack_start(self._grid, False, False, 0)
        self._row = 0

    def row(self, label, widget, hint=None):
        if label:
            lb = Gtk.Label(label=label, xalign=1.0)
            lb.set_size_request(230, -1)
            lb.set_mnemonic_widget(widget)
            self._grid.attach(lb, 0, self._row, 1, 1)
        widget.set_halign(Gtk.Align.START)
        self._grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        if hint:
            h = Gtk.Label(label=hint, xalign=0.0)
            h.set_line_wrap(True)
            h.set_max_width_chars(60)
            h.get_style_context().add_class("dim-label")
            self._grid.attach(h, 1, self._row, 1, 1)
            self._row += 1
        return widget

    def add_full(self, widget):
        self._grid.attach(widget, 0, self._row, 2, 1)
        self._row += 1
        return widget


def gdk_mods(state) -> frozenset:
    mods = set()
    if state & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK
                | Gdk.ModifierType.MOD4_MASK):
        mods.add("Meta")
    if state & Gdk.ModifierType.CONTROL_MASK:
        mods.add("Ctrl")
    if state & Gdk.ModifierType.MOD1_MASK:
        mods.add("Alt")
    if state & Gdk.ModifierType.SHIFT_MASK:
        mods.add("Shift")
    return frozenset(mods)


class ShortcutButton(Gtk.Button):
    """Click, then press a shortcut.  Esc cancels, Backspace disables."""

    def __init__(self, binding: str, on_change):
        super().__init__()
        self.set_size_request(170, -1)
        self._binding = binding
        self._on_change = on_change
        self._listening = False
        self._handler = 0
        self._show()
        self.connect("clicked", self._on_clicked)

    @property
    def binding(self) -> str:
        return self._binding

    def set_binding(self, binding: str):
        self._binding = binding
        self._show()

    def _show(self):
        self.set_label(shortcuts.display(self._binding) or _("None"))

    def _on_clicked(self, _btn):
        if self._listening:
            return
        self._listening = True
        self.set_label(_("Press a shortcut…"))
        self._handler = self.connect("key-press-event", self._on_key)
        self.grab_focus()

    def _on_key(self, _widget, event):
        name = Gdk.keyval_name(Gdk.keyval_to_lower(event.keyval)) or ""
        self.feed(name, gdk_mods(event.state))
        return True

    def feed(self, keyname: str, mods: frozenset) -> str:
        if not self._listening:
            return "idle"
        status, binding = shortcuts.capture(keyname, mods)
        if status in ("wait", "reject"):
            return status
        self._listening = False
        if self._handler:
            self.disconnect(self._handler)
            self._handler = 0
        if status == "done":
            self._binding = binding
            self._on_change(binding)
        elif status == "clear":
            self._binding = ""
            self._on_change("")
        self._show()
        return status


def device_choices(saved: str, names: list) -> list:
    out = [("", _("System default"))] + [(n, n) for n in names]
    if saved and not saved.isdigit() and saved not in names:
        out.append((saved, _("{name} (not connected)").format(name=saved)))
    return out
```

Note: a key-press on a `Gtk.Button` reaches the handler only while the button has focus; `grab_focus()` in `_on_clicked` ensures that. Add `.po` entries: `None` → `Немає`, `Press a shortcut…` → `Натисніть комбінацію…`; recompile `.mo`.

- [ ] **Step 3: Run** section 9 → PASS. **Step 4: Commit** — `settings: section helper, shortcut capture button, device choices`.

---

### Task 9: Settings pages and window

**Files:**
- Create: `parakeet_dictation/ui/settings/pages.py`, `parakeet_dictation/ui/settings/window.py`; fill `ui/settings/__init__.py` with `from .window import SettingsWindow  # noqa: F401`
- Test: `tests/test_settings.py` section 10 (GTK, offscreen)

**Interfaces:**
- Consumes: `SettingsDraft`, `PAGE_KEYS`, `Section`, `ShortcutButton`, `device_choices`, `autostart`, `models._is_model_downloaded`, `models._download_model`, `models._download_all_models`, `audio.list_input_devices`, `i18n._`
- Produces:
  - `SettingsWindow(get_config: Callable[[], AppConfig], profiles_data: dict, on_apply: Callable[[AppConfig], str], on_models_changed: Callable[[], None])` — a `Gtk.Window`. `on_apply` returns a problem string ("" = fine) that the window shows in its InfoBar.
  - `.show_page(page_id: str)` — presents the window on that page (`"general"`, `"recognition"`, `"output"`, `"advanced"`, `"about"`).
  - `.apply() -> bool`, `.cancel()`, `.defaults()` — button actions (public for tests).
  - `PAGES: list[tuple[str, str, str, Callable]]` in `pages.py`: `(page_id, title, icon_name, builder)`; `builder(ctx) -> Gtk.Widget`, where `ctx` is the window (gives `ctx.draft`, `ctx.bind_*` helpers, `ctx.profiles_data`, `ctx.on_models_changed`).

Window helpers used by page builders (methods on `SettingsWindow`):

```python
def bind_check(self, key, label) -> Gtk.CheckButton
def bind_combo(self, key, choices: list[tuple[str, str]]) -> Gtk.ComboBoxText
def bind_spin(self, key, lo, hi, step, digits=0) -> Gtk.SpinButton
def bind_radio(self, key, choices: list[tuple[str, str]]) -> Gtk.Box
def bind_shortcut(self, key) -> ShortcutButton
```

Each helper reads `self.draft.get(key)`, writes `self.draft.set(key, value)` on change, calls `self._refresh_buttons()`, and registers a `refresh` closure in `self._refreshers` so `reload_widgets()` can push draft values back into widgets (used after Apply/Defaults).

- [ ] **Step 1: Failing test**

```python
def s10_settings_window():
    print("\n[10] settings window: pages, Apply/Cancel/Defaults, duplicate shortcut (GTK)")
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    from parakeet_dictation import config as cfgmod, i18n
    from parakeet_dictation.models import load_model_profiles
    from parakeet_dictation.ui.settings import SettingsWindow
    i18n.setup("en")
    state = {"cfg": cfgmod.AppConfig(), "applied": []}

    def on_apply(cfg):
        state["applied"].append(cfg)
        state["cfg"] = cfg
        return ""

    win = SettingsWindow(lambda: state["cfg"], load_model_profiles(), on_apply, lambda: None)
    for page in ("general", "recognition", "output", "advanced", "about"):
        win.show_page(page)
        check(f"page {page} shown", win.stack.get_visible_child_name() == page)
    check("Apply starts disabled", not win.apply_button.get_sensitive())
    win.draft.set("overlay_position", "top"); win._refresh_buttons()
    check("an edit enables Apply", win.apply_button.get_sensitive())
    check("apply() succeeds", win.apply())
    check("on_apply got the edit", state["applied"][-1].overlay_position == "top")
    check("Apply disabled again", not win.apply_button.get_sensitive())
    win.draft.set("hotkey_toggle", "Insert"); win._refresh_buttons()
    check("same shortcut twice is refused", not win.apply()
          and "already used" in win.info_label.get_text(), win.info_label.get_text())
    win.cancel()
    check("cancel discards the draft", win.draft.get("hotkey_toggle") == state["cfg"].hotkey_toggle)
    win.show_page("advanced")
    win.draft.set("num_threads", 9); win.draft.set("overlay_position", "bottom")
    win.defaults()
    check("Defaults resets the current page only",
          win.draft.get("num_threads") == cfgmod.AppConfig().num_threads
          and win.draft.get("overlay_position") == "bottom")
    win.destroy()
```

Register `10`. Run → FAIL.

- [ ] **Step 2: Implement `window.py`**

```python
"""The settings window: sidebar of pages, one draft, OK / Apply / Cancel."""

from gi.repository import GLib, Gtk

from ... import autostart
from ...config import APP_NAME
from ...i18n import _
from .model import PAGE_KEYS, SettingsDraft
from .widgets import ShortcutButton


class SettingsWindow(Gtk.Window):
    def __init__(self, get_config, profiles_data, on_apply, on_models_changed):
        super().__init__(title=_("{app} — Settings").format(app=APP_NAME))
        self.set_default_size(880, 600)
        self.set_icon_name("parakeet-dictation")
        self._get_config = get_config
        self.profiles_data = profiles_data
        self._on_apply = on_apply
        self.on_models_changed = on_models_changed
        self.draft = SettingsDraft(get_config())
        self._refreshers = []
        self._autostart_initial = autostart.is_enabled()
        self.autostart_wanted = self._autostart_initial
        self.connect("delete-event", self._on_delete)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        self.info = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING, show_close_button=True)
        self.info.set_no_show_all(True)
        self.info_label = Gtk.Label(xalign=0.0)
        self.info_label.set_line_wrap(True)
        self.info.get_content_area().add(self.info_label)
        self.info_label.show()
        self.info.connect("response", lambda bar, _r: bar.hide())
        outer.pack_start(self.info, False, False, 0)

        body = Gtk.Box()
        outer.pack_start(body, True, True, 0)
        self.sidebar = Gtk.ListBox()
        self.sidebar.set_size_request(200, -1)
        self.stack = Gtk.Stack()
        from .pages import PAGES
        self._page_ids = []
        for page_id, title, icon, build in PAGES:
            row = Gtk.Box(spacing=10, margin=8)
            row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
            row.pack_start(Gtk.Label(label=title, xalign=0.0), False, False, 0)
            self.sidebar.add(row)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=22)
            heading = Gtk.Label(xalign=0.0)
            heading.set_markup(f"<big><b>{GLib.markup_escape_text(title)}</b></big>")
            page.pack_start(heading, False, False, 0)
            page.pack_start(build(self), False, False, 0)
            scroller.add(page)
            scroller.show_all()
            self.stack.add_named(scroller, page_id)
            self._page_ids.append(page_id)
        self.sidebar.connect("row-selected", self._on_row_selected)
        body.pack_start(self.sidebar, False, False, 0)
        body.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        body.pack_start(self.stack, True, True, 0)

        outer.pack_start(Gtk.Separator(), False, False, 0)
        buttons = Gtk.Box(spacing=8, margin=10)
        defaults = Gtk.Button.new_with_mnemonic(_("_Defaults"))
        defaults.connect("clicked", lambda _b: self.defaults())
        buttons.pack_start(defaults, False, False, 0)
        ok = Gtk.Button.new_with_mnemonic(_("_OK"))
        ok.get_style_context().add_class("suggested-action")
        ok.connect("clicked", lambda _b: self.apply() and self.hide())
        self.apply_button = Gtk.Button.new_with_mnemonic(_("_Apply"))
        self.apply_button.connect("clicked", lambda _b: self.apply())
        cancel = Gtk.Button.new_with_mnemonic(_("_Cancel"))
        cancel.connect("clicked", lambda _b: self.cancel())
        for b in (ok, self.apply_button, cancel):
            buttons.pack_end(b, False, False, 0)
        outer.pack_start(buttons, False, False, 0)
        self.show_page("general")
        self._refresh_buttons()

    # -- pages ---------------------------------------------------------------
    def show_page(self, page_id: str):
        self.sidebar.select_row(self.sidebar.get_row_at_index(self._page_ids.index(page_id)))
        self.stack.set_visible_child_name(page_id)

    def _on_row_selected(self, _box, row):
        if row is not None:
            self.stack.set_visible_child_name(self._page_ids[row.get_index()])

    @property
    def current_page(self) -> str:
        return self.stack.get_visible_child_name()

    # -- bindings ------------------------------------------------------------
    def _changed(self, key, value):
        self.draft.set(key, value)
        self._refresh_buttons()

    def bind_check(self, key, label):
        w = Gtk.CheckButton(label=label)
        w.set_active(bool(self.draft.get(key)))
        w.connect("toggled", lambda b: self._changed(key, b.get_active()))
        self._refreshers.append(lambda: w.set_active(bool(self.draft.get(key))))
        return w

    def bind_combo(self, key, choices):
        w = Gtk.ComboBoxText()
        for cid, label in choices:
            w.append(cid, label)
        w.set_active_id(str(self.draft.get(key)))
        w.connect("changed", lambda c: c.get_active_id() is not None
                  and self._changed(key, c.get_active_id()))
        self._refreshers.append(lambda: w.set_active_id(str(self.draft.get(key))))
        return w

    def bind_spin(self, key, lo, hi, step, digits=0):
        w = Gtk.SpinButton.new_with_range(lo, hi, step)
        w.set_digits(digits)
        w.set_value(self.draft.get(key))
        cast = float if digits else int
        w.connect("value-changed", lambda s: self._changed(key, cast(s.get_value())))
        self._refreshers.append(lambda: w.set_value(self.draft.get(key)))
        return w

    def bind_radio(self, key, choices):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        group, buttons = None, {}
        for cid, label in choices:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
            group = group or rb
            buttons[cid] = rb
            rb.connect("toggled", lambda b, c=cid: b.get_active() and self._changed(key, c))
            box.pack_start(rb, False, False, 0)
        buttons.get(self.draft.get(key), group).set_active(True)
        self._refreshers.append(lambda: buttons.get(self.draft.get(key), group).set_active(True))
        return box

    def bind_shortcut(self, key):
        w = ShortcutButton(self.draft.get(key), lambda b: self._changed(key, b))
        self._refreshers.append(lambda: w.set_binding(self.draft.get(key)))
        return w

    def reload_widgets(self):
        for refresh in self._refreshers:
            refresh()
        self._refresh_buttons()

    # -- buttons -------------------------------------------------------------
    def _refresh_buttons(self):
        dirty = self.draft.dirty or self.autostart_wanted != self._autostart_initial
        self.apply_button.set_sensitive(dirty)

    def _problem(self, text):
        self.info_label.set_text(text)
        self.info.show()

    def _validate(self) -> str:
        hold, toggle = self.draft.get("hotkey_hold"), self.draft.get("hotkey_toggle")
        if toggle and toggle == hold:
            return _("{key} is already used for Hold to talk — choose another "
                     "Start / stop shortcut.").format(key=toggle)
        return ""

    def apply(self) -> bool:
        problem = self._validate()
        if problem:
            self._problem(problem)
            return False
        self.info.hide()
        if self.autostart_wanted != self._autostart_initial:
            try:
                autostart.set_enabled(self.autostart_wanted)
                self._autostart_initial = self.autostart_wanted
            except OSError as e:
                self.autostart_wanted = self._autostart_initial
                self._problem(_("Could not change Start at login: {error}").format(error=e.strerror or e))
        if self.draft.dirty:
            new = self.draft.commit(self._get_config())
            problem = self._on_apply(new)
            self.draft.reload(self._get_config())
            if problem:
                self._problem(problem)
        self.reload_widgets()
        return True

    def cancel(self):
        self.draft.reload(self._get_config())
        self.autostart_wanted = self._autostart_initial
        self.info.hide()
        self.reload_widgets()
        self.hide()

    def defaults(self):
        self.draft.reset_page(self.current_page)
        self.reload_widgets()

    def _on_delete(self, *_a):
        self.cancel()
        return True          # keep the single instance; hide instead of destroy
```

- [ ] **Step 3: Implement `pages.py`**

```python
"""The five settings pages.  Each builder takes the SettingsWindow."""

import subprocess

from gi.repository import GLib, Gtk

from ... import __version__
from ...audio import list_input_devices
from ...config import APP_NAME, DATA_DIR
from ...i18n import _
from ...models import _download_all_models, _download_model, _is_model_downloaded
from .widgets import Section, device_choices

_ICONS = __import__("pathlib").Path(__file__).resolve().parent.parent.parent / "data" / "icons"


def _box(*children):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    for c in children:
        box.pack_start(c, False, False, 0)
    return box


def general(win):
    shortcuts = Section(_("Shortcuts"))
    shortcuts.row(_("Hold to talk:"), win.bind_shortcut("hotkey_hold"))
    shortcuts.row(_("Start / stop:"), win.bind_shortcut("hotkey_toggle"),
                  _("Click, then press the shortcut. Backspace disables it, Esc cancels."))
    kde = Gtk.LinkButton.new_with_label("", _("Open KDE shortcut settings…"))
    kde.connect("activate-link", lambda *_a: (subprocess.Popen(
        ["systemsettings", "kcm_keys"], start_new_session=True), True)[1])
    shortcuts.row(None, kde)

    mic = Section(_("Microphone"))
    try:
        names = [d["name"] for d in list_input_devices()]
    except Exception:
        names = []
    mic.row(_("Device:"), win.bind_combo("audio_device", device_choices(win.draft.get("audio_device"), names)))

    sounds = Section(_("Sounds"))
    beep_on = Gtk.CheckButton(label=_("Beep at start and end"))
    volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.05, 1.0, 0.05)
    volume.set_draw_value(False)
    volume.set_size_request(180, -1)

    def sync_beep(*_a):
        on = win.draft.get("beep_volume") > 0
        beep_on.set_active(on)
        volume.set_sensitive(on)
        if on:
            volume.set_value(win.draft.get("beep_volume"))
    beep_on.connect("toggled", lambda b: win._changed(
        "beep_volume", volume.get_value() if b.get_active() else 0.0) or volume.set_sensitive(b.get_active()))
    volume.connect("value-changed", lambda s: beep_on.get_active() and win._changed("beep_volume", s.get_value()))
    win._refreshers.append(sync_beep)
    sync_beep()
    row = Gtk.Box(spacing=8)
    row.pack_start(beep_on, False, False, 0)
    row.pack_start(volume, False, False, 0)
    sounds.row(None, row)
    quiet = Gtk.Box(spacing=8)
    quiet.pack_start(win.bind_check("night_mode", _("Quiet hours, no beeps:")), False, False, 0)
    quiet.pack_start(win.bind_spin("night_start", 0, 23, 1), False, False, 0)
    quiet.pack_start(Gtk.Label(label="—"), False, False, 0)
    quiet.pack_start(win.bind_spin("night_end", 0, 23, 1), False, False, 0)
    sounds.row(None, quiet)

    system = Section(_("System"))
    system.row(_("Interface language:"), win.bind_combo("ui_language", [
        ("system", _("System default")), ("uk", "Українська"), ("en", "English")]),
        _("Takes effect after the app restarts."))
    login = Gtk.CheckButton(label=_("Start at login"))
    login.set_active(win.autostart_wanted)

    def on_login(b):
        win.autostart_wanted = b.get_active()
        win._refresh_buttons()
    login.connect("toggled", on_login)
    win._refreshers.append(lambda: login.set_active(win.autostart_wanted))
    system.row(None, login)
    return _box(shortcuts, mic, sounds, system)


def recognition(win):
    section = Section(_("Model"))
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    frame = Gtk.Frame()
    frame.add(listbox)
    progress = Gtk.ProgressBar(show_text=True)
    progress.set_no_show_all(True)
    error = Gtk.Label(xalign=0.0)
    error.set_line_wrap(True)
    error.set_no_show_all(True)
    profiles = win.profiles_data["profiles"]

    def populate():
        for child in listbox.get_children():
            listbox.remove(child)
        for mid, data in profiles.items():
            row = Gtk.Box(spacing=12, margin=8)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = Gtk.Label(label=data["name"], xalign=0.0)
            meta = Gtk.Label(label=data.get("description", ""), xalign=0.0)
            meta.set_line_wrap(True)
            meta.get_style_context().add_class("dim-label")
            text.pack_start(name, False, False, 0)
            text.pack_start(meta, False, False, 0)
            row.pack_start(text, True, True, 0)
            if mid == win.draft.get("model_profile"):
                row.pack_end(Gtk.Label(label=_("In use")), False, False, 0)
            elif _is_model_downloaded(mid, profiles):
                use = Gtk.Button(label=_("Use"))
                use.connect("clicked", lambda _b, m=mid: (win._changed("model_profile", m), populate()))
                row.pack_end(use, False, False, 0)
            else:
                dl = Gtk.Button(label=_("Download"))
                dl.connect("clicked", lambda b, m=mid: download(b, m))
                row.pack_end(dl, False, False, 0)
            listbox.add(row)
        listbox.show_all()

    def download(button, model_id):
        button.set_sensitive(False)
        progress.show()
        error.hide()

        def on_progress(msg, fraction):
            progress.set_text(msg)
            if fraction >= 0:
                progress.set_fraction(min(fraction, 1.0))
            else:
                progress.pulse()

        def on_done(ok, err):
            progress.hide()
            if ok:
                populate()
                win.on_models_changed()
            else:
                button.set_sensitive(True)
                error.set_text(_("Download failed: {error}").format(error=err))
                error.show()
        _download_model(model_id, win.profiles_data, on_progress, on_done)

    win._refreshers.append(populate)
    populate()
    section.add_full(frame)
    section.add_full(progress)
    section.add_full(error)

    text = Section(_("Text"))
    text.row(_("Language:"), win.bind_combo("language", [
        ("en", _("Automatic (Parakeet) / English")), ("uk", "Українська"),
        ("de", "Deutsch"), ("es", "Español"), ("fr", "Français")]),
        _("Parakeet detects the language itself; this matters for Canary only."))
    text.row(None, win.bind_check("preview", _("Show the text live while dictating")))
    text.row(None, win.bind_check("filter_fillers", _("Remove filler words (“um”, “uh”)")))
    return _box(section, text)


def output(win):
    when = Section(_("When to insert text"))
    when.row(None, win.bind_radio("insert_mode", [
        ("end_of_take", _("When dictation ends")),
        ("per_segment", _("After each phrase"))]),
        _("Hold to talk always inserts when the key is released."))
    clip = Section(_("Clipboard"))
    clip.row(None, win.bind_check("keep_on_clipboard", _("Keep dictated text on the clipboard")),
             _("If pasting fails, the text can still be pasted by hand with Ctrl+V."))
    ind = Section(_("On-screen indicator"))
    ind.row(None, win.bind_check("overlay", _("Show the recording indicator")))
    ind.row(_("Position:"), win.bind_combo("overlay_position", [
        ("bottom", _("Bottom of the screen")), ("top", _("Top of the screen"))]))
    tray = Section(_("Tray icon"))
    icons = Gtk.Box(spacing=18)
    group = None
    buttons = {}
    for style, label in (("mic", _("Microphone")), ("parrot", _("Parrot")), ("wave", _("Waveform"))):
        rb = Gtk.RadioButton.new_from_widget(group)
        group = group or rb
        inner = Gtk.Box(spacing=6)
        inner.pack_start(Gtk.Image.new_from_file(str(_ICONS / f"parakeet-{style}-listening.svg")), False, False, 0)
        inner.pack_start(Gtk.Label(label=label), False, False, 0)
        rb.add(inner)
        rb.connect("toggled", lambda b, s=style: b.get_active() and win._changed("tray_icon_style", s))
        buttons[style] = rb
        icons.pack_start(rb, False, False, 0)
    buttons.get(win.draft.get("tray_icon_style"), group).set_active(True)
    win._refreshers.append(lambda: buttons.get(win.draft.get("tray_icon_style"), group).set_active(True))
    tray.row(None, icons)
    return _box(when, clip, ind, tray)


def advanced(win):
    perf = Section(_("Performance"))
    perf.row(_("CPU threads:"), win.bind_spin("num_threads", 1, 16, 1))
    pause = Gtk.Box(spacing=6)
    pause.pack_start(win.bind_spin("vad_min_silence", 0.2, 3.0, 0.1, digits=1), False, False, 0)
    pause.pack_start(Gtk.Label(label=_("s")), False, False, 0)
    perf.row(_("Pause that ends a phrase:"), pause)
    paste = Section(_("Pasting"))
    delay = Gtk.Box(spacing=6)
    delay.pack_start(win.bind_spin("paste_min_delay_ms", 0, 2000, 50), False, False, 0)
    delay.pack_start(Gtk.Label(label=_("ms")), False, False, 0)
    paste.row(_("Delay after a shortcut with Meta/Ctrl/Alt:"), delay,
              _("Only when the take was ended by a shortcut with a modifier."))
    paste.row(_("Key press method:"), win.bind_combo("paste_transport", [
        ("auto", _("Automatic (portal, else ydotool)")), ("portal", _("KDE portal")),
        ("ydotool", "ydotool")]))
    terms = Gtk.Entry()
    terms.set_width_chars(40)
    terms.set_text(", ".join(win.draft.get("terminal_window_classes")))
    terms.connect("changed", lambda e: win._changed(
        "terminal_window_classes", [t.strip() for t in e.get_text().split(",") if t.strip()]))
    win._refreshers.append(lambda: terms.get_text() != ", ".join(win.draft.get("terminal_window_classes"))
                           and terms.set_text(", ".join(win.draft.get("terminal_window_classes"))))
    paste.row(_("Terminals (Ctrl+Shift+V):"), terms, _("Window classes, comma-separated."))
    diag = Section(_("Diagnostics"))
    row = Gtk.Box(spacing=8)
    row.pack_start(win.bind_check("diagnostics", _("Write a diagnostics log")), False, False, 0)
    folder = Gtk.Button(label=_("Open folder"))
    folder.connect("clicked", lambda _b: subprocess.Popen(["xdg-open", str(DATA_DIR)], start_new_session=True))
    row.pack_start(folder, False, False, 0)
    diag.row(None, row, _("Timings and character counts only. Text and audio are never recorded."))
    return _box(perf, paste, diag)


def about(win):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10)
    head = Gtk.Box(spacing=10)
    img = Gtk.Image.new_from_file(str(_ICONS / "parakeet-wave-listening.svg"))
    head.pack_start(img, False, False, 0)
    title = Gtk.Label(xalign=0.0)
    title.set_markup(f"<big><b>{GLib.markup_escape_text(APP_NAME)} {__version__}</b></big>")
    head.pack_start(title, False, False, 0)
    box.pack_start(head, False, False, 0)
    desc = Gtk.Label(label=_("Offline dictation for KDE Plasma: hold a key, speak, release.\n"
                             "Recognition runs on this computer; nothing is sent over the network."),
                     xalign=0.0)
    desc.set_line_wrap(True)
    box.pack_start(desc, False, False, 0)
    for line in (_("MIT licence · © Daniel Rosehill, Vadym Dziuba"),
                 _("Parakeet TDT model — NVIDIA, CC-BY-4.0 · TEN VAD — Agora, Apache-2.0 with additional conditions")):
        lb = Gtk.Label(label=line, xalign=0.0)
        lb.set_line_wrap(True)
        lb.get_style_context().add_class("dim-label")
        box.pack_start(lb, False, False, 0)
    links = Gtk.Box(spacing=12)
    links.pack_start(Gtk.LinkButton.new_with_label("https://github.com/dz-vadim/parakeet-dictation", "GitHub"), False, False, 0)
    links.pack_start(Gtk.LinkButton.new_with_label("https://aur.archlinux.org/packages/parakeet-dictation-git", "AUR"), False, False, 0)
    box.pack_start(links, False, False, 0)
    return box


PAGES = [
    ("general", _("General"), "preferences-system", general),
    ("recognition", _("Recognition"), "audio-input-microphone", recognition),
    ("output", _("Text Output"), "edit-paste", output),
    ("advanced", _("Advanced"), "configure", advanced),
    ("about", _("About"), "help-about", about),
]
```

`PAGES` is built at import time, so `pages.py` must be imported **after** `i18n.setup()` (true: `window.py` imports it inside `__init__`). Check `parakeet_dictation/__init__.py` exposes `__version__` (it does: `__version__ = "2.0.0"`).

- [ ] **Step 4: Run** section 10 → PASS; run all suites.
- [ ] **Step 5: Commit** — `settings: KDE-style window — sidebar pages, one draft, OK/Apply/Cancel/Defaults`.

---

### Task 10: Tray rewrite, app wiring, delete MainWindow / SettingsDialog

**Files:**
- Rewrite: `parakeet_dictation/ui/tray.py`
- Modify: `parakeet_dictation/app.py`, `parakeet_dictation/ui/windows.py` (delete `HotkeyCaptureButton`, `SettingsDialog`, `MainWindow` and their imports; keep `WelcomeDialog`; wrap its strings in `_()`)
- Test: `tests/test_settings.py` section 11 (GTK smoke: build `TrayIcon` against a stub controller with `AyatanaAppIndicator3` replaced by a fake recording `set_menu`, `set_title`, `set_icon_full`)

**Interfaces:**
- Consumes: `menu_model.build_menu`, `menu_model.tooltip`, `SettingsWindow`, `HotkeyManager`, `controller.toggle(hold_off=)`, `controller.pause()`, `controller.config`, `controller.profiles`, `controller.profiles_data`, `controller.is_running`, `controller.is_paused`, `audio.list_input_devices`, `models._is_model_downloaded`
- Produces: `TrayIcon(controller, hotkey_mgr)`; `.update_ui()`; `.apply_settings(cfg) -> str` (returns hotkey problem string); `.open_settings(page="general")`; `.on_binding_changed(key, binding)` (wired to `HotkeyManager.on_binding_changed`).

- [ ] **Step 1: Failing test**

```python
def s11_tray():
    print("\n[11] tray renders the menu model; actions reach the controller (GTK)")
    from types import SimpleNamespace
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    from parakeet_dictation import config as cfgmod, i18n
    from parakeet_dictation.ui import tray as traymod
    i18n.setup("en")

    class FakeIndicator:
        def __init__(self): self.menu = None; self.title = ""; self.icon = None
        def set_icon_theme_path(self, *_a): pass
        def set_icon_full(self, name, _desc): self.icon = name
        def set_status(self, *_a): pass
        def set_title(self, t): self.title = t
        def set_menu(self, m): self.menu = m

    ind = FakeIndicator()
    saved = traymod.AyatanaAppIndicator3
    traymod.AyatanaAppIndicator3 = SimpleNamespace(
        Indicator=SimpleNamespace(new=lambda *a: ind),
        IndicatorCategory=SimpleNamespace(APPLICATION_STATUS=0),
        IndicatorStatus=SimpleNamespace(ACTIVE=1))
    calls = []
    ctl = SimpleNamespace(config=cfgmod.AppConfig(), profiles={"desktop": {"name": "P3"}},
                          profiles_data={"profiles": {"desktop": {"name": "P3"}}},
                          is_running=False, is_paused=False,
                          toggle=lambda hold_off=True: calls.append(("toggle", hold_off)),
                          pause=lambda: calls.append(("pause",)),
                          set_status_callback=lambda cb: None, notify=lambda m: None,
                          apply_config=lambda c: calls.append(("apply", c)))
    mgr = SimpleNamespace(rebuild=lambda c: "")
    try:
        t = traymod.TrayIcon(ctl, mgr)
        labels = [i.get_label() for i in ind.menu.get_children() if not isinstance(i, Gtk.SeparatorMenuItem)]
        check("menu starts with Start Dictation", labels[0].startswith("Start Dictation"), str(labels))
        check("no Show Window", not any("Show Window" in l for l in labels))
        check("tooltip set", ind.title.endswith("hold Insert to talk"), ind.title)
        ind.menu.get_children()[0].activate()
        check("tray toggle stops without hold-off", calls[-1] == ("toggle", False), str(calls))
        t.on_binding_changed("hotkey_toggle", "Meta+Alt+D")
        check("KDE-side change applied to config", calls[-1][0] == "apply"
              and calls[-1][1].hotkey_toggle == "Meta+Alt+D")
    finally:
        traymod.AyatanaAppIndicator3 = saved
```

Register `11`. Run → FAIL.

- [ ] **Step 2: Implement `tray.py`**

```python
"""System tray indicator: renders ui.menu_model, owns the settings window."""

import copy
from pathlib import Path

from gi.repository import AyatanaAppIndicator3, Gtk

from ..audio import list_input_devices
from ..config import APP_ID, AppConfig
from ..models import _is_model_downloaded
from . import menu_model

_ICONS_DIR = Path(__file__).resolve().parent.parent / "data" / "icons"
_LEGACY_ICON = {"idle": "audio-input-microphone-muted",
                "listening": "audio-input-microphone",
                "paused": "audio-input-microphone-muted"}


class TrayIcon:
    def __init__(self, controller, hotkey_mgr):
        self._controller = controller
        self._hotkey_mgr = hotkey_mgr
        self._settings = None
        self._indicator = AyatanaAppIndicator3.Indicator.new(
            APP_ID, "audio-input-microphone-muted",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self._custom_icons = (_ICONS_DIR / "parakeet-mic-idle.svg").is_file()
        if self._custom_icons:
            self._indicator.set_icon_theme_path(str(_ICONS_DIR))
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        controller.set_status_callback(lambda _text: self.update_ui())
        self.update_ui()

    # -- state ---------------------------------------------------------------
    def _state(self) -> str:
        if not self._controller.is_running:
            return "idle"
        return "paused" if self._controller.is_paused else "recording"

    def _icon_name(self, state: str) -> str:
        icon_state = {"idle": "idle", "recording": "listening", "paused": "paused"}[state]
        if self._custom_icons:
            return f"parakeet-{self._controller.config.tray_icon_style}-{icon_state}"
        return _LEGACY_ICON[icon_state]

    def update_ui(self):
        state = self._state()
        cfg = self._controller.config
        self._indicator.set_icon_full(self._icon_name(state), state)
        self._indicator.set_title(menu_model.tooltip(state, cfg))
        try:
            devices = [d["name"] for d in list_input_devices()]
        except Exception:
            devices = []
        profiles = self._controller.profiles
        downloaded = {m for m in profiles if _is_model_downloaded(m, profiles)}
        items = menu_model.build_menu(state, cfg, profiles, downloaded, devices)
        self._indicator.set_menu(self._render(items))

    # -- rendering -------------------------------------------------------------
    def _render(self, items) -> Gtk.Menu:
        menu = Gtk.Menu()
        group = None
        for item in items:
            if item.kind == "separator":
                menu.append(Gtk.SeparatorMenuItem())
                continue
            if item.kind == "submenu":
                mi = Gtk.MenuItem(label=item.label)
                mi.set_submenu(self._render(item.children))
            elif item.kind == "radio":
                mi = Gtk.RadioMenuItem.new_with_label_from_widget(group, item.label)
                group = group or mi
                mi.set_active(item.active)
                mi.connect("toggled", lambda w, a=item.action: w.get_active() and self._do(a))
            else:
                mi = Gtk.MenuItem(label=f"{item.label}\t{item.accel}" if item.accel else item.label)
                mi.connect("activate", lambda _w, a=item.action: self._do(a))
            menu.append(mi)
        menu.show_all()
        return menu

    # -- actions ---------------------------------------------------------------
    def _do(self, action: str):
        cfg = self._controller.config
        if action == "toggle":
            self._controller.toggle(hold_off=False)
        elif action in ("pause", "resume"):
            self._controller.pause()
        elif action == "settings":
            self.open_settings("general")
        elif action == "about":
            self.open_settings("about")
        elif action == "download_models":
            self.open_settings("recognition")
        elif action == "quit":
            self._controller.shutdown()
            Gtk.main_quit()
            return
        elif action.startswith("mic:"):
            new = copy.deepcopy(cfg)
            new.audio_device = action[4:]
            if new.audio_device != cfg.audio_device:
                self.apply_settings(new)
            return
        elif action.startswith("model:"):
            new = copy.deepcopy(cfg)
            new.model_profile = action[6:]
            if new.model_profile != cfg.model_profile:
                self.apply_settings(new)
            return
        self.update_ui()

    def open_settings(self, page: str = "general"):
        if self._settings is None:
            from .settings import SettingsWindow
            self._settings = SettingsWindow(lambda: self._controller.config,
                                            self._controller.profiles_data,
                                            on_apply=self.apply_settings,
                                            on_models_changed=self.update_ui)
        self._settings.show_page(page)
        self._settings.present()

    def apply_settings(self, cfg: AppConfig) -> str:
        """The one place a settings change lands (window, menu, KDE)."""
        self._controller.apply_config(cfg)
        problem = self._hotkey_mgr.rebuild(cfg)
        self._controller.notify(problem)
        self.update_ui()
        return problem

    def on_binding_changed(self, key: str, binding: str):
        new = copy.deepcopy(self._controller.config)
        setattr(new, key, binding)
        self.apply_settings(new)
```

Note the accel rendering: `Gtk.MenuItem` labels carry `"\t"` + accel; Plasma's dbusmenu shows the tab-separated part right-aligned. If, during the manual check (Task 12), Plasma shows a literal gap instead, replace it with `f"{label}   ({accel})"` — do not use `Gtk.AccelLabel` (not exported over dbusmenu).

Note `apply_settings` in the controller path: `apply_config` mutates/saves; `rebuild` re-registers both shortcuts — when the change came from KDE (`on_binding_changed`) the re-registration writes the same key back, which is harmless (`SET_FLAGS` NoAutoloading).

- [ ] **Step 3: Rewire `app.py`**

- After `config = AppConfig.load(log=DIAG.log)` add `from . import i18n` (top-level import) and `i18n.setup(config.ui_language)` — before any UI module that builds strings at import time is imported. Move `from .ui.tray import TrayIcon` and `from .ui.windows import WelcomeDialog` imports inside `main()` after `i18n.setup(...)`.
- Delete `MainWindow` creation and `main_window.on_apply_settings`.
- Create `HotkeyManager(config, on_hold_press=lambda: (controller.hold_press(), tray and tray.update_ui()), on_hold_release=lambda: (controller.hold_release(), tray and tray.update_ui()), on_toggle=lambda: (controller.toggle(hold_off=shortcuts.has_modifier(controller.config.hotkey_toggle)), tray and tray.update_ui()), on_binding_changed=lambda k, b: tray and tray.on_binding_changed(k, b))`.
- `tray = TrayIcon(controller, hotkey_mgr)`.
- SIGUSR1 handler: `controller.toggle()` (hold-off stays on: the signal usually comes from a KDE shortcut). SIGUSR2: `controller.pause()`. Both then `tray.update_ui()`.
- Startup description line: `f"Hold {config.hotkey_hold or '-'} to talk · start/stop {config.hotkey_toggle or '-'}"`.
- `WelcomeDialog(profiles_data, config, tray.apply_settings)` unchanged.

- [ ] **Step 4: Shrink `windows.py`** to `WelcomeDialog` + its imports; wrap its user-visible strings in `_()` (`from ..i18n import _`). Grep that nothing imports the deleted classes: `grep -rn "MainWindow\|SettingsDialog\|HotkeyCaptureButton" parakeet_dictation tests` → only test lines already removed in Task 3.

- [ ] **Step 5: Run all suites** → PASS. **Step 6: Commit** — `tray: render the menu model, single settings window; remove the main window and old dialog`.

---

### Task 11: Translate every remaining user-visible string

**Files:**
- Modify: `parakeet_dictation/controller.py`, `insert.py`, `engine.py`, `ui/overlay.py`, `ui/windows.py` (strings only), `parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po` / `.mo`
- Test: `tests/test_settings.py` section 12

**Interfaces:**
- Consumes: `i18n._`

- [ ] **Step 1: Failing test** (catalogue completeness)

```python
def s12_catalogue_complete():
    print("\n[12] Ukrainian catalogue covers every _() string")
    import re, subprocess, tempfile
    root = HERE.parent
    with tempfile.NamedTemporaryFile(suffix=".pot") as pot:
        files = sorted(str(p) for p in (root / "parakeet_dictation").rglob("*.py"))
        subprocess.run(["xgettext", "--language=Python", "--keyword=_", "--keyword=ngettext:1,2",
                        "--from-code=UTF-8", "-o", pot.name, *files], check=True)
        ids = set(re.findall(r'^msgid "(.+)"$', Path(pot.name).read_text(), re.M))
    po = (root / "parakeet_dictation/locale/uk/LC_MESSAGES/parakeet-dictation.po").read_text()
    have = set(re.findall(r'^msgid "(.+)"\nmsgstr "(.+)"$', po, re.M))
    translated = {m for m, s in have if s}
    missing = sorted(ids - translated)
    check("no untranslated strings", not missing, "; ".join(missing[:8]))
    check("more than 100 strings", len(ids) > 100, str(len(ids)))
```

(Multi-line msgids — `msgid ""` followed by continuation lines — are fine to skip in this check; keep UI strings single-line where possible.) Register `12`. Run → FAIL with the missing list.

- [ ] **Step 2: Wrap strings** — every string that reaches the user through `notify()`, `_on_failure(...)`, `_on_error`, `overlay_error_message`, overlay-drawn text, `GLib.idle_add(self._on_partial, "Listening...")`/`"Paused"`/`"Resumed"` in `engine.py`, the `WelcomeDialog`. Find them with:

`grep -nE '(notify|_on_failure|_on_error|_on_partial|set_label|set_text|set_markup|Label\(label=)\(?\s*f?"' parakeet_dictation -r`

Use `_("... {name} ...").format(name=...)` for interpolation, never f-strings inside `_()`. Diagnostics (`DIAG.log`) strings stay untouched.

- [ ] **Step 3: Refresh and translate** — run `tools/update-translations.sh`, then fill every empty `msgstr` in the `.po` with Ukrainian (terminology: dictation = диктування, shortcut = комбінація клавіш / скорочення, clipboard = буфер обміну, tray = трей, paste = вставити, take = запис). Re-run the script to recompile the `.mo`; it must print `untranslated entries (incl. header): 1` or `0`.

- [ ] **Step 4: Run** section 12 and section 2, then all suites → PASS.
- [ ] **Step 5: Commit** — `i18n: Ukrainian translation of every user-visible string`.

---

### Task 12: Docs, packaging, this machine, visual check

**Files:**
- Modify: `README.md` (shortcuts section: Insert / Meta+Insert, both changeable; remove pynput/toggle-mode/start-stop text and the `hotkey_mode`, `hotkey_start/stop/pause` config rows; add `ui_language` row; settings window description; Ukrainian interface), `packaging/PKGBUILD` header comment, `packaging/parakeet-dictation.install` (post-install mentions Insert), `docs/reviews/2026-10-15-full-review.md` (append "Superseded by the redesign: C4 pynput crash — pynput removed")
- Create: `docs/design/2026-10-15-settings-mockups/actual/*.png` via a render script `tools/render-settings.py` (offscreen render of the real `SettingsWindow` pages, same technique as the mockup: `Gtk.OffscreenWindow`, `get_pixbuf().savev`)

- [ ] **Step 1:** Update the docs as listed; `grep -n "Meta+Z\|pynput\|hotkey_mode\|Show Window" README.md packaging/*` → no hits (except a migration note).
- [ ] **Step 2:** `tools/render-settings.py`:

```python
"""Render the real settings window pages to PNG (offscreen) for review.

Run:  .venv/bin/python tools/render-settings.py OUTDIR [uk|en]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from parakeet_dictation import i18n  # noqa: E402
from parakeet_dictation.config import AppConfig  # noqa: E402
from parakeet_dictation.models import load_model_profiles  # noqa: E402

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
i18n.setup(sys.argv[2] if len(sys.argv) > 2 else "uk")
from parakeet_dictation.ui.settings import SettingsWindow  # noqa: E402

cfg = AppConfig.load()
win = SettingsWindow(lambda: cfg, load_model_profiles(), lambda c: "", lambda: None)
child = win.get_child()
win.remove(child)
off = Gtk.OffscreenWindow()
off.set_size_request(880, 600)
off.add(child)
off.show_all()
for page in ("general", "recognition", "output", "advanced", "about"):
    win.show_page(page)
    for _ in range(50):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
    off.get_pixbuf().savev(str(out / f"settings-{page}.png"), "png", [], [])
    print(out / f"settings-{page}.png")
```

Run: `.venv/bin/python tools/render-settings.py docs/design/2026-10-15-settings-mockups/actual uk` and view each PNG; compare against the mockups; fix layout differences that are bugs (clipped text, missing widgets).

- [ ] **Step 3: This machine** — the user's config has `hotkey_hold: "Insert"` and a pynput `hotkey_toggle`; after restart it migrates to `Meta+Insert` automatically. Check `grep -n "parakeet" ~/.config/kglobalshortcutsrc` for a leftover `[services]` entry bound to `Meta+Alt+D` or `Meta+Insert`; if one exists, report it to the user instead of editing the file (KWin owns it; removal is via System Settings > Shortcuts).
- [ ] **Step 4: Restart the app once** (exact pid, `kill -TERM <pid>`, wait for exit, `setsid ./start.sh`), confirm in `~/.local/share/parakeet-dictation/diagnostics.log`: `config_migrated`, two `hold_hotkey_registered action=push-to-talk|toggle conflict=none`, `focus_script loaded=1`. Do not press keys; ask the user to test: hold Insert → dictation; Meta+Insert → start, Meta+Insert → stop; tray menu in Ukrainian (set `ui_language: uk` via Settings > General if the system is `en_US`, then restart).
- [ ] **Step 5: Run all suites** → PASS. **Step 6: Commit** — `docs: README and packaging for the two-shortcut model; rendered settings pages`.

---

## Self-Review

1. **Spec coverage:** §1 shortcuts → Tasks 1, 3, 4, 5; §2 tray menu → Tasks 6, 10; §3 settings window → Tasks 7, 8, 9; mic by name → Task 8/9 (`device_choices`, combo stores name); autostart → Task 7/9; model download refresh of tray → Task 9 (`on_models_changed`) + Task 10; §4 language incl. `ui_language` → Tasks 2, 3, 9, 11; §5 errors → Task 9 (InfoBar for save/hotkey/autostart problems; `apply_settings` returns the hotkey problem); §6 testing → `tests/test_settings.py` sections 1-12 + Task 12 manual check. Removal of the main window → Task 10. pynput removal from packaging → Task 4.
2. **Placeholders:** none; Task 11's string list is found by the given grep rather than enumerated, because it is mechanical and the completeness test enforces it.
3. **Type consistency:** `toggle(hold_off: bool)` (Task 5) used in Tasks 4/10; `HotkeyManager(config, on_hold_press, on_hold_release, on_toggle, on_binding_changed)` (Task 4) used in Task 10; `SettingsWindow(get_config, profiles_data, on_apply, on_models_changed)` (Task 9) used in Task 10 and Task 12; `build_menu(state, config, profiles, downloaded, devices)` (Task 6) used in Task 10; `SettingsDraft.commit(current)` (Task 7) used in Task 9.
4. **Review Focus:** duplicate shortcut → Task 3 (enforce) + Task 9 (`_validate`) tests; KDE-side change → Task 4 + Task 10 tests; tray change under an open window → Task 7 test; disconnected mic → Tasks 6 and 8 tests; old config → Task 3 test.
