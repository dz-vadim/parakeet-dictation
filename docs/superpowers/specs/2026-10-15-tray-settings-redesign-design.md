# Tray menu and Settings redesign

Date: 2026-10-15
Status: approved by the user 2026-10-15

## Goal

Make the tray menu and the settings look like a finished KDE application
before the project is promoted (it is on GitHub and the AUR since
2026-10-15). Agreed with the user:

- an honest, short tray menu: only working actions, the real shortcut, no
  greyed-out filler;
- a settings window laid out like KDE System Settings;
- everyday options separate from technical ones;
- Ukrainian interface (plus English), picked from the system locale;
- informed by MacParakeet, Handy and Superwhisper (push-to-talk first, a
  short General page, status shown by the icon, not by menu rows).

Non-goals: no new toolkit (stays GTK3, decided by the user; Plasma already
paints GTK with Breeze), no change to recognition, insertion or the overlay
beyond translated strings, no history of dictated text (privacy decision of
2026-10-15: the app never stores what was said).

## 1. Shortcuts: two modes, both through KDE

pynput is removed. On KDE Wayland it cannot grab keys globally, so the
Toggle/Start/Stop/Pause fields in today's Hotkeys tab do nothing.

The app's kglobalaccel component (`hotkeys.KGlobalAccelHotkey`) registers
two actions instead of one:

| action id      | config key       | default       | behaviour                    |
|----------------|------------------|---------------|------------------------------|
| `push-to-talk` | `hotkey_hold`    | `Insert`      | press starts, release stops  |
| `toggle`       | `hotkey_toggle`  | `Meta+Insert` | press starts / stops         |

Both are always active (no `hotkey_mode` switch any more) and both show up
in System Settings > Shortcuts under "Parakeet Dictation", where the user
can also change them. An empty binding disables that action. The existing
conflict report ("X is already held by Y") covers both.

Defaults are the same for everyone (decided 2026-10-15): a single key for
push-to-talk has no modifier that can still be held when the paste chord
goes out, so the take can paste almost at once. Both bindings stay user-
configurable (Settings > General, or System Settings > Shortcuts).

Paste hold-off: `paste_min_delay_ms` (default 350) now applies only when the
shortcut that ended the take contains a modifier (Meta+Insert, Meta+Z, …);
a bare key such as Insert ends the take with no extra wait. The config key
keeps its meaning for modifier shortcuts.

Pause has no shortcut: it is a tray item while recording, and SIGUSR2 stays
for scripts. SIGUSR1 (toggle) stays, so `parakeet-toggle` keeps working.

Insertion mode: a push-to-talk take always inserts at the end of the take
(a chord pressed while the key is held ends the take — the reason
`enforce()` forces it today). `insert_mode` (`end_of_take` / `per_segment`)
now applies to toggle takes only; the controller decides per take instead
of `enforce()` rewriting the config.

Config migration (in `AppConfig.load`, logged):

- `hotkey_start`, `hotkey_stop`, `hotkey_pause`, `hotkey_mode` are dropped;
- a `hotkey_toggle` in pynput syntax (contains `<`) becomes `Meta+Insert`;
- a `hotkey_hold` still at the old default `Meta+Z` becomes `Insert`
  (a user-chosen binding is kept);
- this machine: the hand-made `[services]` Meta+Alt+D entry for
  `parakeet-dictation-toggle.desktop` is no longer needed once the toggle
  action exists, so the plan removes it (with a backup) as a one-off step
  outside the code.

## 2. Tray menu

```
Start Dictation              Meta+Insert    (while recording: Stop Dictation)
Pause                                       (only while recording; Resume when paused)
────────────────────────
Microphone            ▸   ◉ System default / ○ <each input device>
Model                 ▸   ◉ <each downloaded model> / Download More…
────────────────────────
Settings…
About
Quit
```

- No status row and no disabled items. State is the icon colour (grey
  idle, green listening, red paused — already shipped) plus the indicator
  title, which Plasma shows as the tooltip: "Parakeet Dictation — ready ·
  hold Insert to talk" / "— listening" / "— paused".
- The shortcut shown is the configured `hotkey_toggle`; when it is empty
  the label has no shortcut. Shortcuts are rendered in KDE notation.
- Radio items (`Gtk.RadioMenuItem`), not "✓" prefixes. Models that are not
  downloaded are not listed; "Download More…" opens Settings > Recognition.
- "Show Window" and the main window go away (they duplicated the microphone
  and model choice). `WelcomeDialog` stays for the first start.
- "Tray Icon" moves into Settings > Text Output.
- Menu contents are built by a pure function (`menu_model(state, config,
  models, devices) -> list[Item]`) so they are testable without a display;
  `TrayIcon` only turns that list into GTK items.

## 3. Settings window

A single `Gtk.Window` (one instance; the menu presents the existing one),
modelled on KDE System Settings:

```
┌────────────────────┬──────────────────────────────────────────┐
│ General            │ <page: groups with bold headings>         │
│ Recognition        │                                           │
│ Text Output        │                                           │
│ Advanced           │                                           │
│ About              │                                           │
├────────────────────┴──────────────────────────────────────────┤
│ [Defaults]                          [OK] [Apply] [Cancel]     │
└───────────────────────────────────────────────────────────────┘
```

Sidebar: `Gtk.ListBox` with a Breeze icon and a label per page, driving a
`Gtk.Stack`. Pages are scrollable, built from one helper that makes a
heading plus a two-column grid (right-aligned labels, controls on the
right), so every page has the same rhythm.

Pages and their options:

- **General**
  - Shortcuts: "Hold to talk" `hotkey_hold`, "Start / stop" `hotkey_toggle`
    (shortcut buttons, see below), a link-style button "Open KDE shortcut
    settings" (`systemsettings kcm_keys`).
  - Microphone: device combo (stored by **name**, not PortAudio index).
  - Sounds: "Beep at start and end" + volume slider; "Quiet hours" with
    from/to hours (`night_*`).
  - Interface language: "System default" / "Українська" / "English"
    (`ui_language`: `system` | `uk` | `en`, default `system`); takes effect
    after a restart of the app, which the page says.
  - System: "Start at login" (writes/removes
    `~/.config/autostart/parakeet-dictation.desktop`).
- **Recognition**
  - Model list as today (name, size, languages, Download / Use / Active),
    language combo, "Show live preview" (`preview`).
  - "Remove filler words" (`filter_fillers`).
- **Text Output**
  - "When to insert text": "When dictation ends" (default, `end_of_take`) /
    "After each phrase" (`per_segment`); a note says push-to-talk always
    inserts when the key is released.
  - "Keep dictated text on the clipboard" (`keep_on_clipboard`) with a one-
    line explanation.
  - On-screen indicator: show (`overlay`), position (`overlay_position`).
  - Tray icon style (`tray_icon_style`) as three icon radio buttons.
- **Advanced**
  - Performance: CPU threads, "Pause that ends a phrase" (`vad_min_silence`),
  - Pasting: delay after key release (`paste_min_delay_ms`), method
    (`paste_transport`), terminal window classes (editable list).
  - Diagnostics: "Write a diagnostics log" (`diagnostics`) + "Open log
    folder"; text explaining it holds timings and counts, never text or
    audio.
- **About**: name, version, one-line description, licence (MIT), credits
  (upstream author, Parakeet CC-BY-4.0, TEN VAD licence), links to GitHub
  and the AUR page.

Behaviour:

- The window edits a **draft** copy of the config. Apply sends the draft
  through the existing single path (`TrayIcon.apply_settings` →
  `controller.apply_config`, hotkey rebuild, conflict report) and greys out
  Apply until something changes again. OK = Apply + close. Cancel/close
  discards the draft. Defaults resets the *current page's* keys to
  `AppConfig()` values (KDE semantics), still only in the draft.
- After Apply, widgets are re-read from the applied config, so anything
  `enforce()` coerced is visible.
- Model download keeps working while the window is open; when it finishes
  the tray menu is rebuilt (fixes today's stale "(not downloaded)").
- Shortcut button: click → "Press a shortcut…" → captures a combination
  with at least one modifier or a single non-modifier key from an allow-
  list (Insert, Pause, ScrollLock, Menu, F1–F24); ignores lone modifier
  presses and keeps listening; Escape cancels; Backspace clears (disables
  the action). It produces KDE syntax ("Meta+Alt+D") validated with
  `hotkeys.qt_key_sequence`, so an unregisterable binding can't be saved.

Code layout: `parakeet_dictation/ui/settings/` package — `window.py`
(sidebar, stack, buttons, draft lifecycle), `pages.py` (page builders),
`widgets.py` (section helper, `ShortcutButton`, device combo), `model.py`
(draft/dirty/defaults-per-page logic, no GTK). `ui/windows.py` keeps only
`WelcomeDialog`; `SettingsDialog`, `HotkeyCaptureButton` and `MainWindow`
are deleted.

## 4. Language

- `parakeet_dictation/i18n.py`: `gettext` domain `parakeet-dictation`,
  `_()` and `ngettext()`, locale dir inside the package
  (`parakeet_dictation/locale/<lang>/LC_MESSAGES/parakeet-dictation.mo`),
  so it works from a checkout, a wheel and the AUR package alike.
- Source strings stay English. Translated: tray menu and title, settings,
  welcome dialog, overlay status texts and the notices the controller shows
  (`notify`, insertion failures). Diagnostics log stays English and
  untranslated.
- `locale/uk/LC_MESSAGES/parakeet-dictation.po` is committed together with
  its compiled `.mo` (no build-time `msgfmt`, so neither the AUR package
  nor a venv needs gettext tools). `tools/update-translations.sh` extracts
  strings (`xgettext`), merges (`msgmerge`) and compiles (`msgfmt`); a test
  fails if the `.mo` is older than the `.po` or a `.po` entry is fuzzy.
- `ui_language` overrides the locale: `uk`/`en` load that catalogue
  regardless of `LANG` (the author's machine runs `en_US` but wants
  Ukrainian); `system` follows `LANGUAGE`/`LC_MESSAGES`/`LANG`.
- Shortcut names stay in KDE notation (Meta, Alt, Ctrl), as KDE itself does
  in the Ukrainian locale.

## 5. Error handling

- Settings Apply failing to save (read-only home, full disk) — `save()`
  already reports instead of raising; the window shows that message in an
  inline `Gtk.InfoBar` and keeps the draft.
- A shortcut conflict after Apply is shown in the same InfoBar on the
  General page (plus the existing overlay notice).
- Autostart file write errors → InfoBar, checkbox reverts.
- Missing input device by name → combo shows "<name> (not connected)" and
  dictation falls back to the default device (backend already does this).

## 6. Testing

- No-display tests (new `tests/test_settings.py`): menu model per state
  (idle/recording/paused, empty toggle binding, no downloaded models);
  draft dirty/Apply/Cancel/Defaults-per-page; config migration of old
  hotkey keys; shortcut capture key-event → KDE string table (incl. lone
  modifiers, Escape, Backspace, F-keys, Insert); autostart file
  write/remove in a temp `XDG_CONFIG_HOME`; translations load for `uk` and
  fall back to English.
- Existing six suites must keep passing; tests that exercise pynput or
  `hotkey_mode` are updated or removed with the feature.
- Manual check on Plasma (user): menu in both languages, each settings
  page, both shortcuts via System Settings > Shortcuts, Start at login.

## Decisions (user, 2026-10-15)

1. Defaults for everyone: hold `Insert`, start/stop `Meta+Insert`, both
   configurable.
2. `insert_mode` keeps `end_of_take` as the default and is a visible option
   on the Text Output page (applies to start/stop takes).
3. The main window ("Show Window": status, Start button, microphone and
   model combos, a broken streaming checkbox) is removed with nothing in
   its place.
4. An interface-language option is added (General page).

Mockups (rendered with GTK3 + Breeze-Dark):
`docs/design/2026-10-15-settings-mockups/index.html`.
