# Code review, 2026-09-30 (effort: high, scope: parakeet_dictation/ and tests/ at 0ce7ddd)

Findings, most severe first. Each is fixed in its own commit referencing this list.

1. audio.py `_emit_segment`: the min-speech floor compares `len(self._buffer)`, which on every
   silence-close already holds 0.8 s of trailing silence, so both floors are dead. A single VAD
   blip becomes a ~0.8 s near-silent block, gets +20 dB and decodes to hallucinated words.
   Compare `self._speech_samples` instead.
2. controller.py `_finish_take`: no try/finally around the flush/insert; an exception in the
   typer (OSError/E2BIG, PermissionError — not caught by `_type_raw`) leaves the gesture at
   STOPPING forever and dictation is dead until restart.
3. controller.py `apply_config`: old/new config is the same mutated object, so `preload()`
   never fires after a model switch; the next take pays the model load while the user speaks.
4. hotkeys.py `unregister`: calls `Gio.DBusConnection.unsubscribe` (does not exist; it is
   `signal_unsubscribe`), swallowed by `except Exception` — every `rebuild()` leaks handlers, so
   after N settings saves each key press fires N+1 times.
5. engine.py recognizer cache key lacks `language`; Canary profiles never rebuild on language change.
6. engine.py `_run_streaming`: no end-of-session flush; the tail after the last endpoint is lost.
7. controller.py `hold_press`: a re-press during the STOPPING window (200 ms tail + drain) is
   dropped as a key repeat; the queued-start support in the engine is unreachable, so a fast
   re-press loses the first second of the next sentence.
8. models.py CLI `download_file`: not atomic (no `.part`), truncated downloads pass the
   `exists()`/size checks and fail later inside sherpa-onnx.
9. engine.py preview worker: "no new audio" compares the windowed size, so after 15 s of
   continuous speech the hypothesis freezes.
10. audio.py `_BlockCoalescer.add` (disabled path) does not bump `closed`; the preview's
    staleness guard is inert, duplicating committed words on the panel.
11. tests/test_defects.py defect 4 runs the real `main()` against the user's real config.
12. tests/ptt_state.py, tests/overlay_safety.py hard-code `/home/dz/Projects/parakeet-dictation`.
13. focus.py `snapshot()` can do synchronous D-Bus round trips (0.5–6 s) on the GTK main
    thread at key release; move rechecks to the bus watcher / a timer, make snapshot() cache-only.
14. insert.py `_clipboard_paste` runs on the GTK main thread (wl-paste 2 s timeouts, spawns,
    sleeps, chord); streaming partials spawn a process per backspaced character.

Cleanup-only (fold into the pass where cheap): `prepare_for_target` newline collapsing is
unreachable after `_sanitize` and keys `terminal=` on the chord instead of the class; terminal
and no-Ctrl-V class lists duplicated between config.py and insert.py; `DATA_DIR` duplicated in
diagnostics.py; layer-shell plumbing duplicated between TranscriptPreview and PillOverlay;
`_on_partial("Listening...")` drives tray/window updates at 10 Hz; stale docstrings still call
per_segment the default.

## Outcome

All 14 findings fixed in commits 42501b5..02009ab plus the cleanup commit dae0821; the
tests live in `tests/test_review.py` (one section per finding).

## Follow-ups noted during the fix pass, not done

- A daemon preload thread still alive at process exit (quit within ~2 s of a model-changing
  settings save) can crash inside onnxruntime static destructors — seen once in a test harness;
  the app path is pre-existing and untouched.
- `prepare_for_target`'s newline collapsing is unreachable because `TextTyper._sanitize`
  strips every newline first (the "never inject Enter" invariant). Kept as the net for future
  callers; deleting it or making it the single policy is a product decision.
- Layer-shell window plumbing is duplicated between `TranscriptPreview` and `PillOverlay`.
- `_on_partial("Listening...")` drives tray/window updates at 10 Hz during speech.
- `DATA_DIR` is computed in both `config.py` and `diagnostics.py` because the layering rule
  forbids either importing the other.
