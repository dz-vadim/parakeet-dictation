# Parakeet Dictation

Local, offline voice dictation for KDE Plasma 6 on Wayland, tuned for Ukrainian and English.
Speech is recognised on the CPU by NVIDIA Parakeet TDT 0.6B v3 (int8) through
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx); phrases are found by
[TEN VAD](https://github.com/TEN-framework/ten-vad). Interaction is push-to-talk: the app
registers its own shortcut with kglobalaccel over D-Bus, so it is told about the key's press
*and* its release. Hold the key to speak; let go and the text is pasted once into the window
that had focus at the moment of release. While the key is held, a pill shows the microphone
level and the elapsed time, and a display-only preview above it shows what has been decoded so
far. The paste chord is chosen from the focused window's class (a resident KWin script reports
it) and pressed as keysyms through the xdg-desktop-portal RemoteDesktop session, so it works
under any keyboard layout. A tray icon gives access to start/stop, model switching and settings.

This is a fork of [danielrosehill/parakeet-dictation](https://github.com/danielrosehill/parakeet-dictation),
the upstream single-file Ubuntu toggle app. This repository is
[dz-vadim/parakeet-dictation](https://github.com/dz-vadim/parakeet-dictation), version 2.0.0.

What it is not:

- Not for X11. `xdotool` remains only as a last-resort chord transport and reaches XWayland windows only.
- Push-to-talk and focused-window detection need KDE: kglobalaccel for the hold shortcut, KWin
  scripting for the window class. On another Wayland compositor the app still runs in toggle mode
  (driven by SIGUSR1) and, with the target unknown, pastes with `paste_chord` (Ctrl+V) everywhere.
- The pynput bindings (`hotkey_toggle`, `hotkey_start`, `hotkey_stop`, `hotkey_pause`) cannot grab
  keys on Wayland outside the app's own windows; toggle and pause are driven by SIGUSR1/SIGUSR2 from
  a desktop shortcut instead.
- The `laptop` (Canary 180M, EN/ES/DE/FR) and `streaming` (Nemotron 0.6B, English only) profiles are
  still selectable, but every measurement below was made with the `desktop` Parakeet profile.

## Requirements

- KDE Plasma 6 on Wayland. Developed and used on Plasma 6.7 / KWin 6.7 on Arch Linux; the portal
  must offer `org.freedesktop.portal.RemoteDesktop` version 2 or later (`NotifyKeyboardKeysym`).
- Arch packages: `python-gobject`, `gtk3`, `libayatana-appindicator`, `gtk-layer-shell`, `portaudio`,
  `wl-clipboard`, `ydotool`, `xdg-desktop-portal-kde`. pip builds PyGObject (and pycairo) from
  source inside the venv, which needs `gobject-introspection`, `cairo` and a C toolchain (`base-devel`).
- ydotool is the fallback chord transport when the portal session is not available:
  `systemctl --user enable --now ydotool`, and put your user in the group that owns `/dev/uinput`
  (`ls -l /dev/uinput`; the stock Arch `80-uinput.rules` uses `input`, some setups use a local rule
  with a `uinput` group). Re-login afterwards.
- The venv is built with **Python 3.12**, which is what this setup was verified with. sherpa-onnx 1.13.8
  now also publishes `cp314` wheels and ten-vad is pure Python, so a 3.14 venv may work but is untested
  here. `uv venv --python 3.12` downloads 3.12 itself.
- Python packages (`requirements.txt`): `sherpa-onnx>=1.12`, `sounddevice>=0.4`, `pynput>=1.7`,
  `numpy`, `PyGObject`, `ten-vad>=1.0.6`. The model-download CLI additionally needs `requests` and `tqdm`.
- About 2 GB of RAM for the desktop model while it is loaded; 639 MB of disk for its files.
- Ubuntu and other distributions: untested since the fork.

## Install

```sh
git clone https://github.com/dz-vadim/parakeet-dictation.git
cd parakeet-dictation
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e .
uv pip install --python .venv/bin/python requests tqdm      # download CLI only
.venv/bin/python -m parakeet_dictation.models desktop        # 639 MB
```

Profiles for the downloader: `desktop` (Parakeet TDT 0.6B v3 int8, 639 MB), `laptop` (Canary 180M
Flash int8, 198 MB), `streaming` (Nemotron Streaming 0.6B int8, 631 MB), `all`. Files go to
`~/.local/share/parakeet-dictation/models/<profile>/` (`$XDG_DATA_HOME` is honoured). If no model is
present at start, a welcome dialog offers to download one from inside the app.

Run with `./start.sh` (uses `.venv`), `.venv/bin/parakeet-dictation`, or
`.venv/bin/python -m parakeet_dictation`. The first start on a desktop asks once, via the portal,
whether the app may "Remote Control" the session; approve it. The grant is persisted as a
restore token in `~/.config/parakeet-dictation/portal-restore-token` (mode 0600) and never asked
for again unless it is revoked.

Autostart, `~/.config/autostart/parakeet-dictation.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Parakeet Dictation
Comment=Local voice dictation; registers its own push-to-talk shortcut at startup
Exec=/home/you/Projects/parakeet-dictation/start.sh
Icon=audio-input-microphone
Terminal=false
```

Push-to-talk is configured in `~/.config/parakeet-dictation/config.json`; the values this README
describes are:

```json
{
  "hotkey_mode": "hold",
  "hotkey_hold": "Meta+Z",
  "insert_mode": "end_of_take",
  "num_threads": 4
}
```

## Usage

Hold **Meta+Z**, speak, release. Capture keeps running for 200 ms after the release so the last word
is not clipped, while the pill switches to *processing* at once. Decoding runs on a worker thread
the whole time the key is held; after the release the app waits for the decoder to drain, joins the
decoded phrases with spaces, strips filler words (`um`, `uh`, `ehm`, ...) and pastes once. Text is
inserted only after release: on KWin any injected keystroke is reported by kglobalaccel as a release
of the held shortcut (Ctrl, V, Ctrl+V and Shift+Insert were each tried), so a mid-hold paste would
end the take. That is what `insert_mode = "end_of_take"` is for; `per_segment` belongs to toggle mode.

The pill (bottom centre by default, click-through, on the layer-shell overlay layer so panels keep
their place):

| State | Looks like | Meaning |
|---|---|---|
| preparing | spinner, "Preparing engine..." | shown only if the start takes longer than 600 ms |
| listening | red dot, 12-bar level meter, clock | microphone open; bars come from the captured RMS and brighten while the VAD sees speech |
| paused | amber dot, dim meter | paused with SIGUSR2 / tray |
| processing | red dot, spinner, clock frozen | key released, decoder draining |
| success | green tick, final time | text was pasted; hides after 0.5 s |
| error | wider rounded rectangle with a message | hides after 3 s; the message is also on stderr |

The pill refuses to show *listening* unless the engine really has a capture stream open, and a
watchdog hides it if capture stops. The preview panel above it holds the phrases already decoded
(append-only, two rows plus one faded) and, refreshed about every second, a provisional guess for
the phrase still being spoken. The panel never inserts anything; it disappears with the pill.

Where the text goes: to the window focused when the key was released, even if focus moved while
the last block was decoding. The text is staged on both the clipboard and the primary selection,
then the chord is pressed:

| Focused window class | Chord |
|---|---|
| `paste_overrides` entry | as configured |
| terminals (`terminal_window_classes`: kitty, Alacritty, konsole, yakuake, foot, wezterm, ghostty, xterm, ...) | `terminal_paste_chord`, Ctrl+Shift+V |
| `no_ctrl_v_classes` (Emacs) | Shift+Insert |
| anything else, or unknown | `paste_chord`, Ctrl+V |

Trailing newlines are always removed (a Return would submit a form); for terminals internal
newlines become spaces too. The previous clipboard and primary contents are put back 0.5 s after
the paste unless `keep_on_clipboard` is set. If no transport could press the chord, one Shift+Insert
retry is made; after that the text stays on the clipboard and the pill says
"Text is on the clipboard - press Ctrl+V".

Beeps: rising 880 Hz on start, falling 440 Hz on stop, a double 660 Hz on pause. `night_mode`
silences them between `night_start` and `night_end` (22:00-09:00 by default).

Changing the shortcut: set `hotkey_hold`, or use Settings > Hotkeys > "Hold key". The syntax is
KDE/Qt: modifiers `Meta` (also `Super`, `Win`), `Alt`, `Ctrl`, `Shift`, joined with `+` to a single
character, `F1`-`F35`, or a named key (`Space`, `Insert`, `Return`, `Escape`, `Tab`, `Home`, `End`,
`PageUp`, `PageDown`, `Left`, `Right`, `Up`, `Down`, ...). The action is listed in System Settings >
Shortcuts as "Parakeet Dictation" / "Push-to-talk dictation". If another component already owns the
key, kglobalaccel accepts the registration but keeps delivering the key to the first
registrant; the app shows an error pill and logs `hold_hotkey_conflict` (see Troubleshooting).

Toggle mode: `hotkey_mode = "toggle"` (or `"start_stop"`). On Wayland the pynput bindings do not
work outside the app's own windows, so bind a KDE custom shortcut to a command that sends the
signals: SIGUSR1 toggles, SIGUSR2 pauses. A minimal launcher, also usable as the shortcut target:

```sh
#!/usr/bin/env bash
pid=$(pgrep -f '[p]ython.* -m parakeet_dictation( |$)' | head -1)
if [ -z "$pid" ]; then
    setsid "$HOME/Projects/parakeet-dictation/start.sh" >/dev/null 2>&1 &
    exit 0
fi
kill -USR1 "$pid"
```

Tray menu: Show Window (start/stop/pause buttons, microphone selector, streaming switch, status
line), Start/Stop Dictation, Pause, a status line, Model (switch between downloaded profiles), Settings
(tabs Models, Hotkeys, General, About), Quit.

## Configuration

`~/.config/parakeet-dictation/config.json`; missing keys take the defaults below, unknown keys are
ignored. Saving from the Settings dialog rewrites the file.

| Key | Type | Default | Meaning |
|---|---|---|---|
| **Model** | | | |
| `model_profile` | str | `"desktop"` | `desktop`, `laptop` or `streaming` (see `parakeet_dictation/data/models.json`) |
| `num_threads` | int | `min(cpu_count, 4)` | ONNX Runtime intra-op threads; 4 is the measured setting (see below) |
| `language` | str | `"en"` | source and target language for the Canary (`laptop`) profile; Parakeet takes none |
| **VAD, coalescing, normalisation** | | | |
| `vad_threshold` | float | `0.5` | TEN VAD speech probability threshold |
| `vad_min_silence` | float | `0.8` | seconds of silence that end a phrase (clamped to 0.1-5.0) |
| `coalesce_target_s` | float | `4.0` | consecutive phrases are grouped into blocks of at least this many seconds before decoding; `0` decodes every phrase alone |
| `normalize` | bool | `true` | boost quiet blocks for the model only (never attenuates, never above +20 dB) |
| `normalize_target_dbfs` | float | `-18.0` | RMS level a block is lifted towards |
| **Audio** | | | |
| `audio_device` | str | `""` | input device name substring or index; empty = system default |
| `beep_volume` | float | `0.5` | start/stop/pause tone volume |
| `night_mode` | bool | `true` | suppress beeps between the hours below |
| `night_start` | int | `22` | hour beeps go quiet |
| `night_end` | int | `9` | hour beeps resume |
| **Hotkeys** | | | |
| `hotkey_mode` | str | `"hold"` | `hold` (push-to-talk via kglobalaccel), `toggle` or `start_stop` |
| `hotkey_hold` | str | `"Meta+Z"` | push-to-talk key, KDE/Qt syntax |
| `hotkey_toggle` | str | `"<ctrl>+0"` | pynput binding for toggle mode |
| `hotkey_start` | str | `"<ctrl>+9"` | pynput binding for start_stop mode |
| `hotkey_stop` | str | `"<ctrl>+8"` | pynput binding for start_stop mode |
| `hotkey_pause` | str | `"<ctrl>+<alt>+0"` | pynput binding for pause; empty disables |
| **Insertion** | | | |
| `insert_mode` | str | `"end_of_take"` | `end_of_take` pastes once after release (forced in hold mode, logged as `config_forced`); `per_segment` pastes each phrase as it decodes |
| `typer` | str | `"clipboard"` | `clipboard` (stage + chord), `ydotool` (same, chord forced through ydotool), `wtype` (types the text directly; needs the virtual-keyboard protocol, labelled GNOME/Sway only in Settings) |
| `paste_transport` | str | `"auto"` | how the chord is pressed: `portal`, `ydotool`, or `auto` (portal when its session is ready, else ydotool) |
| `paste_chord` | str | `"ctrl+v"` | chord for unknown and unlisted windows; `ctrl+v`, `ctrl+shift+v` or `shift+insert` |
| `terminal_paste_chord` | str | `"ctrl+shift+v"` | chord for `terminal_window_classes` |
| `terminal_window_classes` | list | kitty, Alacritty, konsole, yakuake, foot, wezterm, ghostty, xterm, st, contour, gnome-terminal-server, terminator, Tilix, ... | KWin `resourceClass` values, matched case-insensitively |
| `no_ctrl_v_classes` | list | `["emacs", "Emacs"]` | classes that get Shift+Insert |
| `paste_overrides` | dict | `{}` | `{window class: chord}`, wins over the tables |
| `keep_on_clipboard` | bool | `false` | leave the dictated text on the clipboard instead of restoring the previous contents |
| `focus_script` | bool | `true` | load the KWin focus script; off, the target is always unknown |
| `filter_fillers` | bool | `true` | drop `um`, `uh`, `ehm`, `hmm`, `er`, `ah`, `erm`, `hm` |
| `partial_overwrite` | bool | `true` | streaming profile only: type partials and retype on revision (ignored in `end_of_take`) |
| **Overlay and preview** | | | |
| `overlay` | bool | `true` | show the pill |
| `overlay_position` | str | `"bottom"` | `bottom` or `top` centre |
| `preview` | bool | `true` | show the transcript panel above the pill |
| `preview_interval_s` | float | `1.0` | seconds between hypothesis passes over the open phrase; `0` disables the pass |
| `preview_window_s` | float | `15.0` | at most this much of the open phrase is re-decoded per pass |
| **Diagnostics** | | | |
| `diagnostics` | bool | `true` | write `~/.local/share/parakeet-dictation/diagnostics.log` |

## How it works

1. kglobalaccel delivers the press; the controller opens a 16 kHz mono PortAudio stream (100 ms
   chunks). One recognizer per process is loaded at app start and kept warm, so the mic opens
   without waiting for the ~1.6 s model load.
2. TEN VAD (hop 256 samples = 16 ms) marks speech. A phrase closes after `vad_min_silence` (0.8 s) of
   silence; phrases under 0.25 s are dropped, and 30 s is the forced maximum.
3. Consecutive phrases, including the pause audio between them (up to 10 s), are coalesced into
   blocks of at least `coalesce_target_s` (4 s).
4. Each block is loudness-normalised for the model only (boost only, at most +20 dB, towards
   -18 dBFS; nothing below -55 dBFS is lifted; the peak is kept under -1 dBFS), padded with 0.5 s
   of zeros so the transducer emits its final token, and decoded on a worker thread through a
   bounded queue under a single inference lock. Blocks under 0.3 s are discarded.
5. In parallel, about once a second, the tail of the still-open block (up to 15 s) is decoded as a
   hypothesis for the preview panel. It is drawn, never inserted.
6. Release: the pill shows *processing*; capture runs 200 ms more; the VAD and coalescer are flushed;
   the controller waits for the decoder to drain; the phrases are joined and pasted once.
7. Insertion: focus snapshot taken at release, chord chosen, text staged with `wl-copy` on both
   selections, 60 ms settle, chord pressed via the portal (ydotool as fallback), previous clipboard
   restored 0.5 s later.
8. Everything logs one `key=value` line per event to the diagnostics log (5 MB cap, trimmed to whole
   lines; never transcript text or device names).

Three measured facts behind the defaults:

- **Coalescing.** On 120 s of the user's read-aloud Ukrainian (118 reference words, same audio, same
  engine), the 18 VAD segments decoded one by one scored 24.6 % WER in 10.0 s of decode; the same
  segments coalesced into blocks of at least 4 s scored 19.5 % in 8.8 s. Longer context keeps the
  multilingual model from drifting into the wrong orthography mid-phrase (`parakeet_dictation/config.py`,
  `parakeet_dictation/audio.py`). The engine comparison that kept Parakeet over Whisper turbo and
  Canary on latency and memory is in [bench/BENCHMARK.md](bench/BENCHMARK.md).
- **Boost-only normalisation.** Natural dictation from a normal distance sits at -37 to -42 dBFS,
  where the model returned half-English word salad; lifting blocks towards -18 dBFS fixed that.
  Attenuating the loud read-aloud take (-13 dBFS) cost about two errors (19.5 % to 21.2 % WER), so
  the gain is clamped at 0 dB and blocks at or above the target reach the model exactly as captured.
- **Four threads.** ONNX Runtime's intra-op pool spins between calls, so the preview cadence is paid
  in idle CPU proportional to the thread count. At 8 threads a 1 s preview cadence cost about 11 cores
  during a take and made the release-to-text decode 1237 ms; at 4 threads the same cadence costs
  about 3 cores and the final decode is 148 ms
  ([docs/measurements/2026-09-30-preview-cost.md](docs/measurements/2026-09-30-preview-cost.md)).
  2 threads is the battery option.

## Troubleshooting

The log is `~/.local/share/parakeet-dictation/diagnostics.log`, one event per line:
`ts=... event=<name> key=value ...`. Things to grep for:

- `event=hold_hotkey_conflict owner=<component>`: the key is also bound elsewhere, and kglobalaccel
  hands it to whichever component registered first. The app's action is listed in System Settings but
  never fires. A typical owner is a leftover launcher entry such as
  `parakeet-dictation-toggle.desktop` on the same key from an earlier SIGUSR1 setup; remove that
  binding in System Settings > Shortcuts and restart the app. `event=hold_hotkey_failed` means the
  registration itself failed (no kglobalaccel, or an unparseable `hotkey_hold`).
- `event=hold_hotkey_registered ... conflict=none`: the healthy line, once per start.
- `event=paste ... ok=0` followed by `event=paste_failed`: no transport could press the chord.
  `transport=none` means neither a portal session nor `ydotool` was available. Check
  `event=portal_session ready=0 stage=...` (`interface`: no RemoteDesktop v2 portal; `start code=1`:
  the prompt was declined, in which case a saved token is forgotten and the prompt returns next start)
  and `systemctl --user status ydotool` plus membership of the group that owns `/dev/uinput`.
- `event=focus_script loaded=0 reason=...`: KWin scripting was not reachable, so every target is
  unknown and `paste_chord` is used everywhere. Ctrl+V into a terminal is harmful (it sends VLNEXT);
  because the text sits on both selections, `"paste_chord": "shift+insert"` is correct in every app.
- `event=audio_overflow count=N`: PortAudio dropped samples because the capture thread was starved.
  The measurement above shows 8 threads plus a 1 s preview saturating the CPU; lower `num_threads`
  to 4 or raise `preview_interval_s`.
- `event=take_end outcome=no_speech` or many `event=segment_discarded`: nothing reached the model.
  Check the microphone level: `ffmpeg -f pulse -i default -t 5 -af volumedetect -f null - 2>&1 | grep _volume`
  while speaking. `event=normalize rms_in_db=...` shows what each block measured; -37 to -42 dBFS is
  normal and gets boosted, anything under -55 dBFS is treated as noise and never lifted, so raise the
  input gain in the system sound settings if the mean sits down there.
- The pill saying "Nothing to stop - press and hold to dictate": a release arrived with no take
  running, usually because the press went to another owner of the key (see the conflict entry).
- A second copy refuses to start: the app owns `org.kde.parakeet.Dictation` on the
  session bus, and a second launch prints "already running", logs `already_running
  owner_pid=<pid>` and exits 0. If you see two instances anyway, the session bus is
  unavailable (the log says so at start).

Why the text is pasted and never typed: on Plasma up to 6.7, Chromium and Electron windows drop
characters typed through the portal that are not on the active keyboard layout, which is what
Cyrillic is under a Latin layout; `ydotool type` indexes its ASCII table out of bounds on Cyrillic;
`wtype` needs the virtual-keyboard protocol (the Settings dialog labels it GNOME/Sway only). The
clipboard plus a chord pressed as keysyms works in every app regardless of layout.

## Development

Tests (each prints one `[PASS]`/`[FAIL]` line per check and exits non-zero on failure):

| Suite | Checks | Needs |
|---|---|---|
| `.venv/bin/python tests/ptt_state.py` | 43 | nothing (stub engine); covers the push-to-talk state machine |
| `.venv/bin/python tests/overlay_safety.py` | 22 | a display; pill refusal gate, watchdog, stale meter, preview panel |
| `.venv/bin/python tests/test_pipeline.py` | 23 | the desktop model, `bench/segs/*.wav` (4 or more), `bench/real_uk_norm.wav` |
| `.venv/bin/python tests/test_hold_take.py` | 204 | the desktop model, `bench/staging/s90.wav`, `bench/segs/00.wav`, `bench/segs/01.wav`; real-time fake microphone, takes a few minutes |

The two model suites drive the real engine, VAD, coalescer, controller and `TextTyper`; only the
microphone and the external binaries (`wl-copy`, `wl-paste`, `ydotool`) are replaced. The fixture
recordings are the user's own voice and are not in the repository.

`bench/` holds the engine benchmark scripts and [bench/BENCHMARK.md](bench/BENCHMARK.md)
(Parakeet vs faster-whisper, Canary and a Ukrainian FastConformer, all on one real recording).
Recordings, cut segments, transcripts (`out-*.txt`, `dictated*.txt`), `results.json`, downloaded
models and scratch venvs under `bench/` are gitignored; they must never be committed.

Design and measurement notes:
[docs/superpowers/specs/2026-09-30-package-split-design.md](docs/superpowers/specs/2026-09-30-package-split-design.md)
(module layout, import layering, the known defects still open),
[docs/measurements/2026-09-30-preview-cost.md](docs/measurements/2026-09-30-preview-cost.md).

Package layout: `config` and `diagnostics` import nothing from the package; `audio`, `engine`,
`insert`, `hotkeys`, `focus` import only those; `controller` sits above them; `ui/` above the
controller; `app.py` is wiring only. `parakeet_dictation/data/focus.js` is the KWin script,
`parakeet_dictation/data/models.json` the profile table.

## Credits and licence

Upstream: [Daniel Rosehill, parakeet-dictation](https://github.com/danielrosehill/parakeet-dictation),
whose README states the MIT licence; this tree carries no LICENSE file of its own. This fork narrows
the scope to KDE Plasma Wayland push-to-talk with Ukrainian and English dictation; the pipeline,
insertion path, overlay and tests in this repository were measured and verified on one user's voice
and desktop. Models: NVIDIA Parakeet TDT 0.6B v3 via the sherpa-onnx int8 export; TEN VAD.
