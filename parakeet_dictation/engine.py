"""ASR engine: the recognizer cache and the offline / streaming capture loops."""

import math
import queue
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from gi.repository import GLib

from .audio import (SAMPLE_RATE, TenVadDetector, _BlockCoalescer, normalize_for_model,
                    play_beep_pause, play_beep_start, play_beep_stop, resolve_audio_device)
from .config import MODELS_DIR, AppConfig
from .diagnostics import DIAG

# Offline (VAD-segmented) path sizing.
MIN_SEGMENT_SAMPLES = 4800   # 0.3 s — shorter segments decode to garbage
TAIL_PAD_SAMPLES = 8000      # 0.5 s of real zeros appended before decoding

# Grace stop() allows on top of ASREngine.DRAIN_TIMEOUT_S for the capture
# thread's own teardown (stream close, the drain bookkeeping, the beep).
STOP_GRACE_S = 5.0


# ---------------------------------------------------------------------------
# Recognizer cache
#
# Loading the 640 MB Parakeet model costs ~1.6 s.  Doing that on every
# dictation start meant the microphone was not open yet while it loaded, so the
# first words of a take were physically lost.  One recognizer is kept alive for
# the current (kind, profile, threads) triple and rebuilt only when that triple
# changes; a single slot bounds RAM (~2 GB per loaded model).
#
# A sherpa-onnx recognizer is not safe for concurrent decoding, so *every*
# inference call site in this file holds INFERENCE_LOCK.
# ---------------------------------------------------------------------------

INFERENCE_LOCK = threading.Lock()

_cache_lock = threading.Lock()
_cached_key = None
_cached_recognizer = None


def get_recognizer(kind: str, profile_name: str, num_threads: int, build):
    """Return the cached recognizer for this key, building it at most once.

    `build` is called with the cache lock held so two starts in quick
    succession cannot load the model twice.  Returns (recognizer, load_ms);
    load_ms is 0.0 on a cache hit.
    """
    global _cached_key, _cached_recognizer
    key = (kind, profile_name, num_threads)
    with _cache_lock:
        if _cached_key == key and _cached_recognizer is not None:
            return _cached_recognizer, 0.0
        # Drop the previous model before loading the next one.
        _cached_key = None
        _cached_recognizer = None
        t0 = time.perf_counter()
        recognizer = build()
        load_ms = (time.perf_counter() - t0) * 1000
        warmup_ms = _warm_up(kind, recognizer)
        _cached_recognizer = recognizer
        _cached_key = key
    DIAG.log("recognizer_loaded", kind=kind, profile=profile_name,
             threads=num_threads, load_ms=load_ms, warmup_ms=warmup_ms)
    return recognizer, load_ms


def segment_too_short(n_samples: int) -> bool:
    """Whether a segment is below the length floor the model can handle.

    Under 0.3 s the recognizer returns garbage or raises.  Length only — no
    loudness test, because quiet-but-real speech must still be decoded.
    """
    return n_samples < MIN_SEGMENT_SAMPLES


def _warm_up(kind: str, recognizer) -> float:
    """Decode half a second of silence so the first real take is not the slow one.

    The first decode after a load pays ONNX Runtime's lazy graph and arena
    initialisation; spending it here keeps it off the user's first segment.
    """
    t0 = time.perf_counter()
    try:
        silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
        with INFERENCE_LOCK:
            stream = recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, silence)
            if kind == "online":
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
            else:
                recognizer.decode_stream(stream)
    except Exception:
        return -1.0  # a failed warm-up must never block dictation
    return (time.perf_counter() - t0) * 1000


# ---------------------------------------------------------------------------
# ASR Engine — supports offline (VAD-segmented) and streaming modes
# ---------------------------------------------------------------------------

class ASREngine:
    """One capture session at a time, with an explicit stop contract.

    stop() returns only once the session's capture has stopped AND its decode
    queue has drained — bounded by DRAIN_TIMEOUT_S: past that the session is
    abandoned (`drain_timeout` in the log), and anything the decoder still
    produces for it is dropped rather than delivered late into whatever take
    comes next.  A start() that arrives while a stop is still draining is
    queued: its thread joins the previous session before opening the
    microphone, so two sessions never hold the stream or the decoder at once.
    """

    # How long a stopping session may wait for its decode queue.  Minutes of
    # backlog would be needed to hit it; a stuck ONNX call is what it is for.
    DRAIN_TIMEOUT_S = 60.0

    def __init__(self, config: AppConfig, profile: dict, on_text, on_partial, on_error,
                 on_partial_type=None, on_commit_partial=None,
                 on_capture_start=None, on_level=None, on_preview=None):
        self._config = config
        self._profile = profile
        self._on_text = on_text
        self._on_partial = on_partial
        self._on_error = on_error
        self._on_partial_type = on_partial_type or (lambda t: None)
        self._on_commit_partial = on_commit_partial or (lambda t: None)
        self._on_capture_start = on_capture_start or (lambda: None)
        self._on_level = on_level or (lambda rms, speech: None)
        # Running hypothesis of the phrase still being spoken.  DRAWN, never
        # inserted — the preview pass is the only text source in the engine
        # whose output is not allowed anywhere near the typer.
        self._on_preview = on_preview
        self._running = False
        self._paused = False
        self._thread = None
        # One Event per session, created by start() and handed to the run
        # thread: a session only ever reads its own, so a stop meant for the
        # previous session cannot be cleared by the next one starting.
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = NOT paused
        self._pause_event.set()
        # Set while the newest session has no decodes outstanding.  stop()
        # already waits for this; wait_drained() is the belt to that brace.
        self._drained = threading.Event()
        self._drained.set()

    def _get_model_dir(self) -> Path:
        return MODELS_DIR / self._config.model_profile

    def _ensure_models(self):
        model_dir = self._get_model_dir()
        profile_files = self._profile.get("files", {})
        missing = []
        for key, info in profile_files.items():
            fp = model_dir / info["filename"]
            if not fp.exists():
                missing.append(info["filename"])
        if missing:
            raise FileNotFoundError(
                f"Missing model files: {', '.join(missing)}\n"
                f"Run: python -m parakeet_dictation.models {self._config.model_profile}"
            )

    def _build_offline_recognizer(self):
        import sherpa_onnx
        model_dir = self._get_model_dir()
        files = self._profile["files"]
        decoder_type = self._profile.get("decoder_type", "transducer")

        if decoder_type == "transducer":
            return sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(model_dir / files["encoder"]["filename"]),
                decoder=str(model_dir / files["decoder"]["filename"]),
                joiner=str(model_dir / files["joiner"]["filename"]),
                tokens=str(model_dir / files["tokens"]["filename"]),
                num_threads=self._config.num_threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=self._profile.get("feature_dim", 128),
                provider="cpu",
                model_type=self._profile.get("model_type", "nemo_transducer"),
                decoding_method="greedy_search",
            )
        elif decoder_type == "canary":
            return sherpa_onnx.OfflineRecognizer.from_nemo_canary(
                encoder=str(model_dir / files["encoder"]["filename"]),
                decoder=str(model_dir / files["decoder"]["filename"]),
                tokens=str(model_dir / files["tokens"]["filename"]),
                src_lang=self._config.language,
                tgt_lang=self._config.language,
                num_threads=self._config.num_threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=self._profile.get("feature_dim", 128),
                provider="cpu",
                decoding_method="greedy_search",
            )
        else:
            return sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                model=str(model_dir / files["model"]["filename"]),
                tokens=str(model_dir / files["tokens"]["filename"]),
                num_threads=self._config.num_threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=self._profile.get("feature_dim", 128),
                provider="cpu",
                decoding_method="greedy_search",
            )

    def _build_online_recognizer(self):
        import sherpa_onnx
        model_dir = self._get_model_dir()
        files = self._profile["files"]
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(model_dir / files["encoder"]["filename"]),
            decoder=str(model_dir / files["decoder"]["filename"]),
            joiner=str(model_dir / files["joiner"]["filename"]),
            tokens=str(model_dir / files["tokens"]["filename"]),
            num_threads=self._config.num_threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=self._profile.get("feature_dim", 128),
            provider="cpu",
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=1.2,
            rule3_min_utterance_length=300,
        )

    def _acquire_offline_recognizer(self):
        return get_recognizer("offline", self._config.model_profile,
                              self._config.num_threads,
                              self._build_offline_recognizer)

    def _acquire_online_recognizer(self):
        return get_recognizer("online", self._config.model_profile,
                              self._config.num_threads,
                              self._build_online_recognizer)

    def preload(self):
        """Build and warm the recognizer ahead of the first dictation start."""
        self._ensure_models()
        if self._profile.get("streaming", False):
            self._acquire_online_recognizer()
        else:
            self._acquire_offline_recognizer()

    def _build_vad(self):
        return TenVadDetector(
            threshold=self._config.vad_threshold,
            # Clamped: a config edit to 0 would cut a segment every hop.
            min_silence_duration=min(max(self._config.vad_min_silence, 0.1), 5.0),
            min_speech_duration=0.25,
            max_speech_duration=30.0,
            sample_rate=SAMPLE_RATE,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self):
        """Begin a session.  Never blocks: a start that lands while the
        previous session is still draining is queued behind it (see _run)."""
        previous = self._thread
        if previous is not None and previous.is_alive():
            if not self._stop_event.is_set():
                return              # alive and not stopping: a duplicate start
        else:
            previous = None
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._pause_event.set()
        self._paused = False
        self._drained.clear()
        self._thread = threading.Thread(target=self._run, args=(stop_event, previous),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the current session and return once it has ENDED: capture
        closed and the decode queue drained, or DRAIN_TIMEOUT_S elapsed and
        the session abandoned.  Safe to call from the main loop for the
        shutdown and settings paths; the controller's take path calls it off
        the loop so the pill keeps animating.
        """
        # The flag is set unconditionally: `_running` only goes true once the
        # run thread is past the model check, so gating on it dropped stops
        # that arrived during start-up and left the stream open.
        thread = self._thread
        self._stop_event.set()
        self._pause_event.set()
        if thread is not None and thread is not threading.current_thread():
            bound = self.DRAIN_TIMEOUT_S + STOP_GRACE_S
            thread.join(timeout=bound)
            if thread.is_alive():
                DIAG.log("drain_timeout", stage="stop", waited_s=bound)
        if self._thread is thread:
            # No newer session was queued behind this one meanwhile.
            self._running = False
            self._paused = False

    def wait_drained(self, timeout: float = 60.0) -> bool:
        """Block until the newest session's decode queue has emptied."""
        return self._drained.wait(timeout)

    def _settle(self):
        """This session is over.  Its flags are released only if no newer
        session has been queued behind it — that one's end releases them."""
        if self._thread is threading.current_thread():
            self._running = False
            self._drained.set()

    def pause(self):
        if not self._running:
            return
        if self._paused:
            self._paused = False
            self._pause_event.set()
            play_beep_pause(self._config.beep_volume)
            GLib.idle_add(self._on_partial, "Resumed")
        else:
            self._paused = True
            self._pause_event.clear()
            play_beep_pause(self._config.beep_volume)
            GLib.idle_add(self._on_partial, "Paused")

    def _publish_level(self, audio, speech: bool):
        """Feed the overlay meter from the samples already being captured.

        Two sub-windows per 100 ms block is the ~20 Hz the meter redraws at.
        Deliberately reuses this stream: a second InputStream on the same
        device would fight the capture loop for it.
        """
        flat = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not flat.size:
            return
        half = max(flat.size // 2, 1)
        for start in range(0, flat.size, half):
            window = flat[start:start + half]
            if not window.size:
                continue
            rms = float(np.sqrt(np.mean(window * window)))
            if not math.isfinite(rms):
                rms = 0.0
            GLib.idle_add(self._on_level, rms, speech)

    def _run(self, stop_event=None, previous=None):
        if stop_event is None:
            stop_event = self._stop_event
        if previous is not None:
            # Queued behind a session that is still draining: the microphone
            # and the decoder are handed over, never shared.
            t0 = time.monotonic()
            previous.join(timeout=self.DRAIN_TIMEOUT_S + STOP_GRACE_S)
            DIAG.log("session_queued", waited_ms=(time.monotonic() - t0) * 1000,
                     previous_ended=not previous.is_alive())
        if stop_event.is_set():
            # Pressed and released while the previous session drained: there
            # is nothing to capture any more, so nothing is opened.
            DIAG.log("session_skipped", reason="stopped_before_open")
            self._settle()
            return
        try:
            self._ensure_models()
        except Exception as e:
            self._settle()        # nothing will ever drain — free the waiters
            GLib.idle_add(self._on_error, str(e))
            return

        is_streaming = self._profile.get("streaming", False)
        self._running = True

        try:
            if is_streaming:
                self._run_streaming(stop_event)
            else:
                self._run_offline(stop_event)
        except Exception as e:
            GLib.idle_add(self._on_error, str(e))
        finally:
            # After _run_offline's own finally, so the decode worker has been
            # joined (or abandoned) and every on_text callback is already
            # queued: this is the instant stop() is waiting for.
            self._settle()
            play_beep_stop(self._config.beep_volume)

    def _run_offline(self, stop_event=None):
        if stop_event is None:
            stop_event = self._stop_event
        vad = self._build_vad()
        coalescer = _BlockCoalescer(self._config.coalesce_target_s)
        # Held around every mutation of the VAD and the coalescer, so the
        # preview thread can snapshot the still-open audio without racing the
        # capture thread that is extending it.
        live_lock = threading.Lock()
        chunk_duration = 0.1
        samples_per_chunk = int(SAMPLE_RATE * chunk_duration)

        # Bounded so a stalled decoder cannot grow the backlog without limit.
        # 16 segments is minutes of speech — far more than the model can fall
        # behind in practice — and a full queue blocks rather than drops audio.
        pending: queue.Queue = queue.Queue(maxsize=16)
        stats = {"segments": 0, "too_short": 0, "overflow": 0,
                 "queue_full": 0, "decode_ms": 0.0, "flushed": 0,
                 "preview_passes": 0, "preview_skipped": 0, "preview_ms": 0.0}
        worker = threading.Thread(target=self._decode_worker,
                                  args=(pending, stats), daemon=True)
        worker.start()

        previewer = None
        if self._on_preview and self._config.preview \
                and self._config.preview_interval_s > 0:
            previewer = threading.Thread(
                target=self._preview_worker,
                args=(vad, coalescer, live_lock, stats, stop_event), daemon=True)
            previewer.start()

        def submit(samples, reason="silence", parts=1):
            """Queue a finished block for the decoder thread.

            `reason` says whether the VAD cut this on silence or the
            end-of-take flush closed it, so a swallowed tail is visible in the
            log instead of being indistinguishable from "nothing was said".
            `parts` is how many VAD segments the block coalesced.
            """
            if segment_too_short(len(samples)):
                stats["too_short"] += 1
                DIAG.log("segment_discarded", reason="too_short", source=reason,
                         dur_ms=len(samples) / SAMPLE_RATE * 1000)
                return
            stats["segments"] += 1
            if pending.full():
                stats["queue_full"] += 1
                DIAG.log("decode_queue_full", depth=pending.qsize())
            pending.put((samples, reason, parts))

        def drain(reason: str):
            """Move closed segments into blocks and queue whatever is ready.

            Under the lock only for the bookkeeping: submit() can block on a
            full queue, and blocking there with the lock held would stall the
            preview thread behind the decoder.
            """
            with live_lock:
                ready = []
                while not vad.empty():
                    ready.extend(coalescer.add(vad.front))
                    vad.pop()
            for block, parts in ready:
                submit(block, reason, parts)

        device = resolve_audio_device(self._config.audio_device)
        t_open = time.perf_counter()
        try:
            with sd.InputStream(
                device=device, channels=1, dtype="float32", samplerate=SAMPLE_RATE,
                blocksize=samples_per_chunk,
            ) as stream:
                DIAG.log("session_start", mode="offline",
                         profile=self._config.model_profile,
                         threads=self._config.num_threads,
                         stream_open_ms=(time.perf_counter() - t_open) * 1000)
                # Capture is live from here — this, not start(), is the moment
                # a deferred stop becomes applicable.
                GLib.idle_add(self._on_capture_start)
                play_beep_start(self._config.beep_volume)
                GLib.idle_add(self._on_partial, "")

                last_overflow_log = 0.0
                while not stop_event.is_set():
                    self._pause_event.wait(timeout=0.1)
                    if stop_event.is_set():
                        break
                    if self._paused:
                        continue

                    audio, overflowed = stream.read(samples_per_chunk)
                    if overflowed:
                        # `overflowed` reports samples PortAudio dropped
                        # *before* this read; the block just read is valid
                        # speech, so keep it and only count the event.
                        stats["overflow"] += 1
                        now = time.monotonic()
                        if now - last_overflow_log > 5.0:
                            last_overflow_log = now
                            DIAG.log("audio_overflow", count=stats["overflow"])
                    samples = audio.reshape(-1).tolist()
                    with live_lock:
                        vad.accept_waveform(samples)
                        speech = vad.is_speech_detected()
                    self._publish_level(audio, speech)
                    if speech:
                        GLib.idle_add(self._on_partial, "Listening...")

                    drain("silence")

                # Close whatever the VAD still holds.  A release mid-phrase
                # leaves the entire phrase pending with no silence to cut it,
                # so this is the only thing that saves the user's last words;
                # the decoder is drained in the finally block below, so they
                # still reach the document.
                with live_lock:
                    stats["flushed"] = int(bool(vad.flush()))
                    ready = []
                    while not vad.empty():
                        ready.extend(coalescer.add(vad.front))
                        vad.pop()
                    # Whatever is still accumulating goes as-is: a block that
                    # never reached the target is still the words the user just
                    # said, and only MIN_SEGMENT_SAMPLES may drop it.
                    ready.extend(coalescer.flush())
                for block, parts in ready:
                    submit(block, "flush", parts)
        finally:
            # The preview is a hypothesis about audio that has now been closed
            # and queued: drop it before the committed text arrives, so the
            # panel cannot end the take showing a guess next to the real thing.
            if previewer is not None:
                stop_event.set()            # the pass is timer-driven; wake it
                previewer.join(timeout=2)
                if previewer.is_alive():
                    # Still inside a decode.  It checks the stop flag before it
                    # delivers, so nothing stale can reach the panel; waiting
                    # any longer here would only delay the take's insertion.
                    DIAG.log("preview_join_timeout")
                if self._on_preview:
                    GLib.idle_add(self._on_preview, "")
            # The sentinel must be sent even if the input stream raised, or the
            # decoder thread waits on an empty queue for the life of the process.
            try:
                pending.put(None, timeout=5)
            except queue.Full:
                pass
            worker.join(timeout=self.DRAIN_TIMEOUT_S)
            if worker.is_alive():
                # Still inside a decode (or behind a backlog).  Waiting longer
                # would hold the next take hostage, so this session is
                # abandoned: the worker sees the flag and drops whatever it
                # still produces instead of delivering it into another take.
                stats["abandoned"] = True
                DIAG.log("drain_timeout", stage="session", depth=pending.qsize(),
                         timeout_s=self.DRAIN_TIMEOUT_S)
            DIAG.log("session_stop", mode="offline", segments=stats["segments"],
                     discarded_short=stats["too_short"], overflow=stats["overflow"],
                     queue_full=stats["queue_full"], flushed=stats["flushed"],
                     decode_ms=stats["decode_ms"],
                     preview_passes=stats["preview_passes"],
                     preview_skipped=stats["preview_skipped"],
                     preview_ms=stats["preview_ms"])

    def _decode_worker(self, pending: "queue.Queue", stats: dict):
        """Decode finished segments off the capture thread.

        Decoding inline in the capture loop meant nothing read the input stream
        for the 0.2-0.6 s a decode takes, which is what produced the overflows
        in the first place.  One worker over a FIFO queue keeps emitted text in
        segment order, and GLib.idle_add preserves that order on delivery.
        """
        try:
            recognizer, _ = self._acquire_offline_recognizer()
        except Exception as e:
            GLib.idle_add(self._on_error, str(e))
            # Keep draining so the capture thread never blocks on a full queue.
            while pending.get() is not None:
                pass
            return

        while True:
            item = pending.get()
            if item is None:
                return
            samples, reason, parts = item
            if stats.get("abandoned"):
                # The session gave up waiting for this queue: its take is
                # over, so the audio is dropped undecoded rather than decoded
                # into a document the user has moved on from.
                DIAG.log("late_segment_dropped",
                         dur_ms=len(samples) / SAMPLE_RATE * 1000, reason=reason)
                continue
            try:
                text, decode_ms = self._decode_segment(
                    recognizer, samples,
                    normalize=self._config.normalize,
                    target_dbfs=self._config.normalize_target_dbfs)
            except Exception as e:
                DIAG.log("decode_error", err=type(e).__name__)
                continue
            if stats.get("abandoned"):
                # The bound elapsed during THIS decode.
                DIAG.log("late_text_dropped", chars=len(text), decode_ms=decode_ms)
                continue
            stats["decode_ms"] += decode_ms
            DIAG.log("segment", dur_ms=len(samples) / SAMPLE_RATE * 1000,
                     decode_ms=decode_ms, chars=len(text), reason=reason,
                     segments=parts)
            if text:
                GLib.idle_add(self._on_text, text)

    @staticmethod
    def _prepare_audio(samples, normalize=True, target_dbfs=-18.0):
        """Build the float32 buffer the recognizer is handed.  (audio, info).

        sherpa-onnx / Parakeet path ONLY.  NVIDIA's TDT transducer needs
        trailing encoder frames before it emits its final token, and the decode
        is bounded to the audio handed in, so without real appended samples the
        last word of a segment is dropped even though it is clearly audible.
        This must NEVER be applied to a Whisper engine: trailing silence there
        makes Whisper hallucinate extra text.

        The normalisation gain lands on this copy and nowhere else — see
        normalize_for_model.
        """
        audio, info = (normalize_for_model(samples, target_dbfs) if normalize
                       else (np.asarray(samples, dtype=np.float32).reshape(-1), None))
        return np.concatenate([
            audio, np.zeros(TAIL_PAD_SAMPLES, dtype=np.float32),
        ]), info

    @staticmethod
    def _decode_prepared(recognizer, audio) -> str:
        """Run one decode.  The caller must already hold INFERENCE_LOCK."""
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()

    @staticmethod
    def _decode_segment(recognizer, samples, normalize=True, target_dbfs=-18.0):
        """Decode one block with sherpa-onnx.  Returns (text, decode_ms)."""
        audio, info = ASREngine._prepare_audio(samples, normalize, target_dbfs)
        t0 = time.perf_counter()
        with INFERENCE_LOCK:
            text = ASREngine._decode_prepared(recognizer, audio)
        decode_ms = (time.perf_counter() - t0) * 1000
        if info is not None:
            DIAG.log("normalize", **info)
        return text, decode_ms

    def _preview_worker(self, vad, coalescer, live_lock, stats, stop_event=None):
        """Decode the still-open audio on a timer so the panel shows something.

        DISPLAY ONLY.  What this returns is a hypothesis about audio that has
        not been committed yet; it goes to the preview panel and nowhere else,
        and the document still gets text only from closed blocks.

        Three rules keep it out of the real decode's way:
          * single flight — one thread, one pass at a time, and a tick that
            arrives while a pass is running is dropped, never queued,
          * INFERENCE_LOCK is taken non-blocking and the tick is skipped if the
            decoder holds it, so a guess never queues in front of a real block,
          * the window is bounded, so the cost of a tick does not grow with the
            length of the take.

        It is not free, though: a block that closes while a pass is in flight
        waits for that pass to finish.  Measured on this machine at the 1 s
        default, the final block's decode went from ~0.27 s to ~0.7-0.9 s, and
        the take's CPU rose by several cores' worth (ONNX Runtime keeps its
        thread pool spinning between calls).  A 2-3 s cadence costs a fraction
        of that — see `preview_interval_s`.
        """
        if stop_event is None:
            stop_event = self._stop_event
        interval = max(float(self._config.preview_interval_s), 0.2)
        window = int(max(float(self._config.preview_window_s), 1.0) * SAMPLE_RATE)
        try:
            # Blocks behind the model load on a cold first take; the take may
            # be over by the time it returns, hence the check straight after.
            recognizer, _ = self._acquire_offline_recognizer()
        except Exception:
            return          # a failed preview must never take the take down
        if stop_event.is_set():
            return
        last_len = 0
        showing = False
        while not stop_event.wait(interval):
            if self._paused:
                continue
            with live_lock:
                seq = coalescer.closed
                parts = [coalescer.tail(window), vad.open_tail(window)]
            audio = np.concatenate(parts)
            if audio.size > window:
                audio = audio[-window:]
            if audio.size < MIN_SEGMENT_SAMPLES:
                # Nothing open: the last hypothesis (if any) has been committed
                # or was too short to ever be one.
                if showing:
                    showing = False
                    GLib.idle_add(self._on_preview, "")
                last_len = 0
                continue
            if audio.size == last_len:
                continue    # no new audio since the last pass — same answer
            if not INFERENCE_LOCK.acquire(blocking=False):
                stats["preview_skipped"] += 1
                DIAG.log("preview_pass", dur_ms=audio.size / SAMPLE_RATE * 1000,
                         decode_ms=0.0, skipped=1)
                continue
            try:
                prepared, _info = self._prepare_audio(
                    audio, self._config.normalize,
                    self._config.normalize_target_dbfs)
                t0 = time.perf_counter()
                text = self._decode_prepared(recognizer, prepared)
            except Exception as e:
                DIAG.log("preview_error", err=type(e).__name__)
                continue
            finally:
                INFERENCE_LOCK.release()
            if stop_event.is_set():
                return      # the take ended mid-pass: this guess is history
            decode_ms = (time.perf_counter() - t0) * 1000
            last_len = audio.size
            stats["preview_passes"] += 1
            stats["preview_ms"] += decode_ms
            stale = int(coalescer.closed != seq)
            DIAG.log("preview_pass", dur_ms=audio.size / SAMPLE_RATE * 1000,
                     decode_ms=decode_ms, skipped=0, stale=stale,
                     chars=len(text))
            if stale:
                # A block closed while this was decoding: its committed text is
                # already on the way, and showing this too would double the
                # words on the panel.
                continue
            if not text:
                # The model returns nothing for some mid-word cuts.  Blanking
                # the panel on that would make the guess flicker in and out, so
                # the previous one stands until a better guess or the real
                # decode replaces it.
                continue
            showing = True
            GLib.idle_add(self._on_preview, text)

    def _run_streaming(self, stop_event=None):
        if stop_event is None:
            stop_event = self._stop_event
        recognizer, _ = self._acquire_online_recognizer()
        with INFERENCE_LOCK:
            stream = recognizer.create_stream()
        chunk_duration = 0.1
        samples_per_chunk = int(SAMPLE_RATE * chunk_duration)
        partial_overwrite = self._config.partial_overwrite
        overflow = 0
        speaking = False  # last decode produced text — the streaming path's
                          # only speech signal, there is no VAD here

        device = resolve_audio_device(self._config.audio_device)
        with sd.InputStream(
            device=device, channels=1, dtype="float32", samplerate=SAMPLE_RATE,
            blocksize=samples_per_chunk,
        ) as mic:
            DIAG.log("session_start", mode="streaming",
                     profile=self._config.model_profile,
                     threads=self._config.num_threads)
            GLib.idle_add(self._on_capture_start)
            play_beep_start(self._config.beep_volume)
            GLib.idle_add(self._on_partial, "")

            last_overflow_log = 0.0
            while not stop_event.is_set():
                self._pause_event.wait(timeout=0.1)
                if stop_event.is_set():
                    break
                if self._paused:
                    continue

                audio, overflowed = mic.read(samples_per_chunk)
                if overflowed:
                    # Samples were lost before this read — this block is good.
                    overflow += 1
                    now = time.monotonic()
                    if now - last_overflow_log > 5.0:
                        last_overflow_log = now
                        DIAG.log("audio_overflow", count=overflow)
                samples = audio.reshape(-1).tolist()
                self._publish_level(audio, speaking)
                stream.accept_waveform(SAMPLE_RATE, samples)

                with INFERENCE_LOCK:
                    while recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    partial = recognizer.get_result(stream).strip()
                    at_endpoint = recognizer.is_endpoint(stream)

                speaking = bool(partial)
                if partial:
                    GLib.idle_add(self._on_partial, partial)
                    if partial_overwrite:
                        GLib.idle_add(self._on_partial_type, partial)

                if at_endpoint:
                    if partial:
                        if partial_overwrite:
                            GLib.idle_add(self._on_commit_partial, partial)
                        else:
                            GLib.idle_add(self._on_text, partial)
                    with INFERENCE_LOCK:
                        recognizer.reset(stream)

        DIAG.log("session_stop", mode="streaming", overflow=overflow)
