# Full review, 2026-10-15 (scope: parakeet_dictation/, tests/, packaging/, docs at da6b9cf)

Every finding below was checked against the code before it was fixed. Each behavioural fix
landed with a regression section in `tests/test_review.py` (sections #16-#33, titled with the
finding id), written first and seen to fail on the commit before. Out of scope for this round,
because a UI rework is coming separately: `ui/tray.py` and `ui/windows.py` (tray menu, settings
dialog, hotkey capture, microphone combo).

| Id | File(s) | Status | Commit | Notes |
|---|---|---|---|---|
| C1 | controller.py | fixed | 6019f82 | A press while STARTING with a deferred stop cancels it (`deferred_stop_cancelled`). Test #16. |
| C2 | insert.py | fixed | 5063cd0 | wl-copy `TimeoutExpired` caught with `OSError`; reported as "Insertion failed: wl-copy timed out". A half-staged (or possibly staged) clipboard gets the user's selections written back at once instead of being dropped. Test #17. |
| C3 | insert.py | fixed | 44c6357 | `PortalKeyboard.press` releases the keys it pressed, closes the session, and reports the paste as done once V/Insert went down; ydotool only when no paste can have happened (modifiers only, or nothing, sent). Test #18. |
| C4 | hotkeys.py, config.py | fixed | 2b51d57 | Unparseable pynput bindings are dropped with a status message; `enforce()` resets `hotkey_toggle/start/stop/pause` to default (`config_forced reason=unparseable_binding`). Test #19. |
| C5 | controller.py | fixed | 63bfb0b | `stop()` stamps `_release_t`, so `paste_min_delay_ms` applies to toggle / SIGUSR1 / tray stops. Test #20. |
| C6 | hotkeys.py | fixed | c097380 | Watches `org.kde.kglobalaccel`; on reappearance after a loss, drops its subscriptions and re-registers (`hold_hotkey_reregistered`). Test #21. |
| C7 | insert.py, controller.py, app.py | fixed | 20d14d7 | `TextTyper.flush_restore()` (waits out the remaining delay, restores synchronously) called from `controller.shutdown()`; SIGINT and SIGTERM both via `GLib.unix_signal_add`. Test #22. |
| C8 | insert.py, focus.py, diagnostics.py | fixed | deae88b | wl-copy staging on stdin, `wtype -` on stdin; token via 0600 temp + `os.replace`; no `focus` line per window switch; log 0600 in a 0700 dir (trims keep 0600). The per-take `focus_snapshot` line keeps its `target=` class (it is the paste target). Test #23; test_defects #10 and test_review #14 updated. |
| C9 | controller.py | fixed | 81eaff7 | Text arriving while idle is dropped (`late_text_dropped`); `start()` clears the take buffers. Test #24. |
| E1 | engine.py, controller.py, insert.py | fixed | 1f261a3 | VAD / coalescer flush + submit moved into the finally (own try); streaming final decode in its finally. Additionally the controller no longer discards that text on the error: it goes to the clipboard via new `TextTyper.copy_only()` (no chord, the key may be held) and the pill says so. Test #25. |
| E2 | audio.py | fixed | 1effb66 | Name exact, then case-insensitive substring, then legacy index (must be an input device); otherwise `device_missing` (no name logged) and the default. The settings dialog still stores the index (UI rework). Test #26. |
| E3 | data/models.json, models.py | fixed | 540cf8a | URLs pinned to the current HF commit of each repo; sha256 + size per file (LFS sha256 from `X-Linked-Etag`; tokens.txt hashed after download, git blob id matched the etag). Verified before rename. The installed desktop profile matches all four digests. Test #30. |
| E4 | models.py | fixed | bcd8665 | `mkstemp` in the destination dir + process-wide download lock. Test #31. |
| E5 | models.py, app.py | fixed | 7186282 | `migrate_legacy_models()` is a no-op without `APP_DIR/pyproject.toml`, moves only profile-id dirs, and runs once in `app.main()` after the instance lock, inside try/except. Test #32. |
| E6 | audio.py | fixed | 10b5df9 | `np.clip` before the int16 conversion. Test #27. |
| E7 | audio.py | fixed | 10b5df9 | Beeps go through `_play()` (try/except, `beep_failed`). Test #27. |
| E8 | config.py, engine.py | partly fixed | cb3209b | Clamps (`coalesce_target_s` [0, 30], `preview_window_s` [1, 30]) in `enforce()` and in the engine. **Skipped:** passing the ndarray instead of `.tolist()`: sherpa-onnx 1.13.5's binding is `accept_waveform(sample_rate, waveform: Sequence[float])`, so an ndarray goes through the same element-wise conversion; measured on 30 s of audio, 26 ms with `.tolist()` vs 30 ms with the array. No gain, so left as is. Test #28. |
| F1 | config.py, controller.py | fixed | b4262b6 | Temp file in the same dir, fsync, `os.replace`; `OSError` reported (`config_error reason=save_failed`), `save()` returns False. Test #29. |
| F2 | config.py, diagnostics.py | fixed | b4262b6 | `CONFIG_DIR` from `$XDG_CONFIG_HOME` (absolute only, per spec); `$XDG_DATA_HOME` gets the same rule. Test #29. |
| F3 | config.py, app.py | fixed | cb3209b | Enums (hotkey_mode, insert_mode, overlay_position, paste_transport, typer, tray_icon_style, both chords) and ranges (num_threads, night hours, VAD threshold/silence, beep volume, normalise target, paste delay, preview interval; NaN rejected) reset to default with `config_forced`. Test #28. |
| U2 | ui/overlay.py | fixed | d2ba053 | Error pill grows to the text (min 348, max 640 or screen width minus margins), then two lines, then ellipsis; display time 3 s + 60 ms per character past 60, capped at 8 s. Test #33. |
| P1 | packaging/PKGBUILD, .SRCINFO | fixed | 0c65490 | History unshallowed first (73 commits, upstream tags v1.2.0/v1.3.0 present). `pkgver()` always `__version__.r<count>.g<hash>`. |
| P2 | packaging/* | fixed | 621ee17 | License dir `$pkgname`; `portaudio` dropped (python-sounddevice depends on it); `python-wheel` dropped from both PKGBUILDs (setuptools 1:84.0.0 ships `bdist_wheel`); one-line `post_upgrade`; icon installed as `hicolor/scalable/apps/parakeet-dictation.svg`, `Icon=parakeet-dictation`. |
| P3 | packaging/deps/python-ten-vad-bin | fixed | ec2a402 | 1.0.6.8 -> 1.0.6.9; sdist sha256 `0c809363...191e` equals PyPI's digest; test-built with makepkg in a scratch copy. |
| P4 | packaging/PKGBUILD (copy only) | done | none | Built from a scratch copy with `source=git+file://<checkout>`; see below. |
| P5 | pyproject.toml, README.md | fixed | 6b14478 | `pycairo`, `requests`, `tqdm` (plain dependency: the download CLI is part of the documented setup); `readme = "README.md"`; upstream links canonical `danielrosehill/Parakeet-Type-Ubuntu` (the old name 301-redirects there). |
| P6 | README.md, requirements.txt | fixed | 846704f | Arch section, Privacy section, licence line, credits, `paste_min_delay_ms` / `tray_icon_style` rows; requirements.txt matches pyproject. |
| P7 | .gitignore, docs/reviews/2026-09-30-code-review.md | fixed | ae856aa | `.venv*/`; home path replaced by `<checkout>`. |
| P8 | docs/reviews/2026-10-15-full-review.md | fixed | (this file) | |

## Test results after the last code commit

| Suite | Checks |
|---|---|
| tests/test_defects.py | 124 pass |
| tests/test_hold_take.py | 210 pass |
| tests/test_pipeline.py | 23 pass |
| tests/test_review.py | 248 pass (98 before this review) |
| tests/ptt_state.py | 43 pass |
| tests/overlay_safety.py | 22 pass |

All six suites were run after every code commit; the packaging and documentation commits change
no file a test reads.

## Packaging builds

Both built with `makepkg -f -d` in copies under `/tmp/claude-1000/` (no install, no sudo).
`python-build` and `python-installer` are not installed on the build machine, so they were put on
`PYTHONPATH` from a scratch `--target` directory and the dependency check skipped; everything else
came from the system.

- `python-ten-vad-bin-1.0.6.9-1-x86_64.pkg.tar.zst`: the bindings, `ten_vad/lib/Linux/x64/libten_vad.so`,
  LICENSE and NOTICES.
- `parakeet-dictation-git-2.0.0.r97.gae856aa-1-any.pkg.tar.zst` (before this file was committed):
  package, both executables, the desktop entry with `Icon=parakeet-dictation`, the hicolor icon,
  README, and the licence under `/usr/share/licenses/parakeet-dictation-git/`.

## Open points for the maintainer

- Neither `parakeet-dictation-git` nor `python-ten-vad-bin` is on the AUR (the RPC returns nothing
  for either), so the README's `yay -S parakeet-dictation-git` is written as "once it is published";
  until then the README builds both from `packaging/`.
- The settings dialog still stores the microphone as a PortAudio index; E2 resolves names first,
  but storing the name is part of the UI rework.
- The `pkgver=` line in packaging/PKGBUILD names the commit it was last regenerated on; makepkg
  rewrites it on every build, as usual for a -git package.
