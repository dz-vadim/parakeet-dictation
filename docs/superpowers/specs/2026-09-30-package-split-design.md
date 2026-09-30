# Package split design

**Status:** approved in conversation 2026-09-30. **Scope:** structure only — no behaviour change.

## Goal

`dictation_app.py` has grown to ~4600 lines through five successive agent passes. No one
can read it whole, and every edit is made by someone who sees only a slice of it. Split it
into a package with one clear responsibility per module, add `pyproject.toml` and a console
entry point, and separate real tests from one-off benchmark scripts. This is also the
prerequisite for an AUR package.

## Non-goals

- No change in behaviour, config keys, log events, or file locations. The four test suites
  must pass unchanged before and after the split; that is the equivalence gate.
- No engine change, no new features, no defect fixes mixed into the move. Known defects are
  fixed afterwards, each in its own commit with its own test (see below).

## Target layout

```
parakeet_dictation/
  __init__.py       version
  config.py         AppConfig (+ load/save, migrations), CONFIG_FILE, MODELS_DIR
  diagnostics.py    DiagnosticLog, DIAG
  models.py         models.json access, _download_model, _download_all_models,
                    _migrate_legacy_models, profile helpers
  audio.py          resolve_audio_device, TenVadDetector, _BlockCoalescer,
                    normalize_for_model, beeps
  engine.py         ASREngine, recognizer cache (get_recognizer, INFERENCE_LOCK, _warm_up)
  insert.py         TextTyper (clipboard save/restore, paste chords, ydotool keycodes)
  hotkeys.py        qt_key_sequence, KGlobalAccelHotkey, HotkeyManager, POSIX signal wiring
  controller.py     DictationController (take state machine, deferred stop, release tail,
                    insert modes, preview feed)
  ui/overlay.py     PillOverlay, TranscriptPreview (gtk-layer-shell + fallback)
  ui/tray.py        TrayIcon
  ui/windows.py     MainWindow, SettingsDialog, WelcomeDialog, HotkeyCaptureButton
  app.py            main() — wiring only
  data/models.json  package data
tests/              test_pipeline.py, test_hold_take.py, ptt_state.py, overlay_safety.py
bench/              ab_*.py, run_*.py, harness.py, sweep_L.py, probe_short.py,
                    compare.py, reference-uk.txt, BENCHMARK.md
```

Rules for the move:
- Cut on existing class/function boundaries; do not rename public names in the same commit.
- Module-level singletons (`DIAG`, the recognizer cache, `INFERENCE_LOCK`) live in exactly one
  module and are imported from there. Nothing may re-create them.
- GTK imports (`gi.require_version`) happen once, in `parakeet_dictation/ui/__init__.py`
  or the module that first needs them, and every other module imports from `gi.repository`
  without repeating `require_version`.
- Circular imports are a defect: `config` and `diagnostics` import nothing from the package;
  `audio`/`engine`/`insert`/`hotkeys` import only from those two; `controller` imports the
  four above; `ui/*` imports `controller` and below; `app` imports everything.

## Entry points and launchers

- `pyproject.toml`: name `parakeet-dictation`, version bumped to `2.0.0` (the interface is
  now KDE Wayland push-to-talk, not the upstream Ubuntu toggle app), dependencies taken from
  `requirements.txt`, console script `parakeet-dictation = parakeet_dictation.app:main`,
  `python -m parakeet_dictation` also works, `models.json` as package data.
- `start.sh` becomes `exec ./.venv/bin/python -m parakeet_dictation "$@"`.
- `~/.local/bin/parakeet-toggle` and any pgrep in tests match `dictation_app\.py`; they must
  be updated to match the new process (`-m parakeet_dictation`). The autostart entry runs
  `start.sh`, so it needs no change.
- `dictation_app.py` is deleted, not kept as a shim — a shim would let the old pgrep patterns
  keep "working" while pointing at nothing.

## Known defects to fix after the split (one commit + one test each)

1. `TenVadDetector.accept_waveform` indexes the float buffer from the un-prepended list, so
   the audio handed to the model drifts up to 192 samples from what the VAD scored and ~3 % of
   samples are never buffered.
2. `AppConfig.load()` swallows every exception and silently resets all settings; it must
   report the error and keep defaults only for the keys that failed.
3. `ASREngine.stop()` can return while the previous session is still decoding.
4. No single-instance lock; with push-to-talk two copies both act on the key.
5. `_type_raw`'s `FileNotFoundError` handler names `self._method` even when the missing
   binary is `wl-copy`/`ydotool`.
6. `wl-paste --no-newline` strips a trailing newline on clipboard restore.
7. The non-layer-shell overlay fallback is decorated and centred on KDE (dead code here, but
   it must not ship broken).

## Repository hygiene (separate commits)

- Upstream Ubuntu packaging (`debian/`, `build-deb.sh`), the upstream agent handover
  (`HANDOVER.md`), upstream planning notes (`planning/`) and the tracked
  `.claude/settings.local.json` are removed; git history keeps them. A `packaging/`
  directory will hold the PKGBUILD when the AUR work starts.
- `README.md` is rewritten for what the app is now: KDE Plasma Wayland, push-to-talk on a
  kglobalaccel-registered shortcut, single insertion after release, pill + preview, the
  config keys, and the ydotool/uinput prerequisite.

## Verification

- `python -m py_compile` on every module; `python -m parakeet_dictation --help` (or a dry
  start) imports cleanly with no circular-import error.
- All four suites pass before the split (baseline) and after it, unchanged except for import
  paths.
- The app restarts and registers the hotkey (`hold_hotkey_registered … conflict=none` in the
  diagnostics log), and a real push-to-talk take inserts text once.
- `git diff --stat` per commit is reviewed: the split commit moves code; defect commits are
  small and each carries a test.
