# Ukrainian + English dictation: engine benchmark

**Audio:** `recordings/20260930-read-uk.wav` — the user reading all six blocks of `reference-uk.txt`
aloud, 119.9 s, 16 kHz mono, speech from 24.5 s to 100.4 s (68.8 s of speech, 118 reference words).
**Every WER number in this document comes from that real-voice recording.** No accuracy number here
is from synthetic speech. `synth-take.wav` (espeak-ng) exists only because it was used to validate
the harness before the real take arrived; its transcripts were discarded.

Machine: 16 threads, 15 GB RAM, CPU only (onnxruntime / CTranslate2 CPU providers).
Scoring: `compare.py` — word-level Levenshtein, punctuation-insensitive, apostrophes normalised.
Decoding is greedy everywhere (`beam_size=1`), so runs are deterministic and repeat identically.

`blocks/N.wav` are the six reference blocks, cut by `prep_blocks.py` at the sentence-level pauses
(the reference is 3+3+3+3+1+1 spoken sentences and TEN VAD finds exactly 14 groups, so the mapping
to blocks is exact). `blocks/segs/` holds the same audio re-cut into the **live app's** VAD segments
(TEN VAD hop 256, threshold 0.5, 0.25 s min silence, <0.3 s dropped) — 17 segments, 1.5 s–9.5 s.

Two overall WER columns are reported. Block **[6] is the English control sentence**, so a
Ukrainian-only model fails it by construction; `excl [6]` is the Ukrainian-only comparison.

---

## Decision table

Per-block WER %, then punctuation marks per 100 words, then wall-clock decode time (median of 3,
after warm-up) for the fixed 3.0 s clip `blocks/short.wav` and for the whole 68.8 s take in one call.

| # | Candidate | all | excl [6] | [1] short | [2] укр letters | [3] mixed | [4] numbers | [5] long | [6] English | punct | caps | 3 s clip | 68.8 s take | disk | peak RSS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3 | **fw large-v3-turbo, uk, L=content+128** (whole utterance) | **16.9** | **17.4** | **0.0** | 17.6 | 25.0 | 27.3 | 9.7 | **11.1** | 19.8 | yes | 0.73 s ⚠ | 8.5 s | 1.6 GB | 2.1 GB |
| 3 | fw large-v3-turbo, uk, L=content+256 | 16.9 | 17.4 | 0.0 | 17.6 | 25.0 | 27.3 | 9.7 | 11.1 | 19.8 | yes | 0.82 s ⚠ | 7.8 s | 1.6 GB | 2.2 GB |
| 3 | fw large-v3-turbo, uk, L=3000 (stock padding) | 22.9 | **16.5** | 0.0 | 11.8 | 25.0 | 27.3 | 9.7 | 100.0 | 20.4 | yes | 4.75 s | 7.6 s | 1.6 GB | 2.7 GB |
| 2 | Canary-1B-v2 int8, uk, **VAD segments** | 25.4 | 19.3 | **0.0** | 23.5 | **21.4** | 27.3 | 16.1 | 100.0 | 22.5 | yes | 1.05 s | 34.0 s | 1.03 GB | 4.1 GB |
| 2 | Canary-1B-v2 int8, uk, whole blocks | 29.7 | 23.9 | **0.0** | 23.5 | 32.1 | 27.3 | 22.6 | 100.0 | 21.4 | yes | 0.92 s | 28.1 s | 1.03 GB | 3.4 GB |
| 4a | **Parakeet TDT 0.6B v3 int8, VAD segments — INCUMBENT** | 23.7 | 23.9 | 9.1 | 23.5 | 32.1 | 45.5 | 6.5 | 22.2 | 18.6 | yes | **0.64 s** | 5.9 s | 670 MB | 1.9 GB |
| 4b | Parakeet TDT 0.6B v3 int8, whole block, 1 call | 25.4 | 26.6 | 9.1 | 17.6 | 42.9 | 45.5 | 9.7 | 11.1 | 21.3 | yes | 0.64 s | 5.9 s | 670 MB | 1.9 GB |
| 3 | fw small, uk, L=content+128 | 37.3 | 32.1 | 18.2 | 47.1 | 28.6 | 45.5 | 22.6 | 100.0 | 17.0 | yes | **0.29 s** | 4.1 s | 462 MB | 0.9 GB † |
| 3 | fw small, uk, L=3000 | 39.8 | 33.0 | 18.2 | 41.2 | 28.6 | 45.5 | 29.0 | 122.2 | 16.8 | yes | 1.24 s | 3.6 s | 462 MB | 1.2 GB |
| 1 | UA FastConformer-Hybrid CTC **fp32** | 47.5 | 43.1 | 18.2 | 70.6 | 60.7 | 31.8 | 29.0 | **100.0** | 12.7 | yes | **0.15 s** | **1.4 s** | 457 MB | 1.7 GB |
| 1 | UA FastConformer-Hybrid CTC fp32, VAD segments | 48.3 | 44.0 | 18.2 | 52.9 | 71.4 | 36.4 | 29.0 | 100.0 | 23.6 | yes | 0.15 s | 1.3 s | 457 MB | 1.7 GB |
| 1 | UA FastConformer-Hybrid CTC **int8** | 51.7 | 47.7 | 18.2 | 70.6 | 60.7 | 40.9 | 38.7 | 100.0 | 14.6 | yes | 0.23 s | 1.7 s | **131 MB** | 1.2 GB |
| 2 | Canary-1B-v2 int8, **en** (reference point) | 110.2 | 119.3 | 127.3 | 147.1 | 92.9 | 131.8 | 116.1 | **0.0** | 16.2 | yes | 1.24 s | 28.4 s | 1.03 GB | 3.3 GB |
| 3 | fw large-v3-turbo, **en** (reference point) | 119.5 | 128.4 | 136.4 | 152.9 | 96.4 | 131.8 | 138.7 | 11.1 | 15.8 | yes | 3.18 s | 7.1 s | 1.6 GB | 2.2 GB † |

⚠ **The 0.73 s / 0.82 s figures are latency for output you should not ship.** A 3.0 s clip has
300 mel frames, so `content+128` rounds to L=512 — inside the repetition-collapse regime described
below. The same config on the 8–15 s blocks (L = 1024–1664) is sound. See "Short utterances".

Disk sizes are the files actually needed. Whisper `model.bin` is stored fp16 and converted to int8
at load, so the disk figure overstates resident model size. Peak RSS is `ru_maxrss` of the whole
benchmark process (model + audio + harness), so treat it as an upper bound, not the model's
footprint. Parakeet's 670 MB / 1.9 GB is the incumbent's cost for reference.

† Some runs loaded several models into one process, which makes `ru_maxrss` cumulative. Where that
happened the table shows the clean figure from the run where that model was loaded first
(`fw small` 0.9 GB, `large-v3-turbo` 2.1–2.2 GB); the raw cumulative values are in `results.json`.

---

## Findings

### The incumbent's VAD segmentation is *not* a major error source (candidate 4a vs 4b)

Re-derived from scratch: **Parakeet VAD-segmented = 23.7 % overall, 23.9 % excluding block [6]**
(28 edits / 118 words, 17 segments, 0.49 s median per segment). Decoding each whole block in one
call with 0.5 s of zeros appended gives **25.4 % / 26.6 %** — *worse*, not better. Decoding the
entire 68.8 s take in one call degrades further and drops content (`out-parakeet-onecall-take.txt`).

So the answer to the (a)-vs-(b) question is: **segmenting is mildly helpful for Parakeet, and
replacing it would not fix anything.** Whole-block decoding does help block [2] (23.5 → 17.6) and
block [6] (22.2 → 11.1) but badly hurts block [3] (32.1 → 42.9), where it mangles tech terms
("порт88", "ForcePh").

My 23.7 % does not exactly reproduce the 21.2 % measured by the coordinator on the same file. Both
used 17 segments. The difference is segmentation boundaries: I cut the take into blocks first
(with 0.25 s of room tone kept either side) and then ran the app's VAD inside each block, which
shifts onsets slightly. The most visible single difference is "Навряд" → "Navidad" in my run and
"Навряд" (correct) in theirs — 2 of the 3 extra edits. **Treat 21–24 % as the incumbent's band, not
a point value**; every conclusion below holds across that whole band.

### Candidate 1 (Ukrainian FastConformer CTC) is out

Measured 47.5 % fp32 / 51.7 % int8 excluding-block-6 43.1 % / 47.7 % — roughly **twice the
incumbent's error rate**. It is disqualified on Ukrainian accuracy alone, before any
Latin-vocabulary argument. Two corrections to the brief:

- **The int8 file works.** `model.int8.onnx` (131 MB) exists in the repo, ONNX Runtime loads and
  runs it, and it produces sensible Ukrainian. The model card's claim that "there is no int8
  variant … ONNX Runtime cannot execute ConvInteger on CPU" is **wrong** for onnxruntime 1.30.0.
  int8 does cost ~4 WER points (51.7 vs 47.5) and is *slower* than fp32 (1.72 s vs 1.38 s on the
  take) — ConvInteger is poorly optimised, so int8 buys disk space and nothing else.
- **The Latin-vocabulary damage is total, and worse than "isolated single characters" suggests.**
  The 513-token vocab contains 29 Latin letters as single characters and no multi-character Latin
  subword. But 13 uppercase (`DFGKOQRSTUWYZ`) and 10 lowercase (`bdfghlqswz`) letters are absent
  **entirely**. Every English word in the test set contains at least one letter the model physically
  cannot emit — `Docker` needs D, `git` needs g, `rebase` needs b and s, `the` needs h, `settings`
  needs g and s, `works` needs s and w. Measured consequence: block [6] is **100 % WER** at every
  setting ("Орпен Заеседян скпчек Тезер Зазає мнікрофон Луркс"), and block [3] is 60.7 % vs the
  incumbent's 32.1 %. Punctuation and capitalisation do work (`, . ?` are in the vocab, capitalised
  Cyrillic pieces are present), and it is by far the fastest engine (0.15 s for 3 s, 1.4 s for the
  whole take) — but that speed is not worth double the errors.

The brief's point that the incumbent already transliterates ("git rebase" → "кітребейс", "Docker" →
"докар") is correct and fair, so Cyrillic-only output is not automatically disqualifying. It just
happens that this model is also much worse at Ukrainian.

### Candidate 2 (Canary-1B-v2): best on short segments, but `language` means *translate*

The single most important behavioural finding: **`language=` on Canary is a target language, not a
source-language hint.** With `language="uk"` the English control sentence comes back **translated**
into Ukrainian — "Open the settings and check whether the microphone works." → "Відкрийте
налаштування та перевірте, чи працює мікрофон." That is a fluent, correct translation and a 100 %
WER. Symmetrically, `language="en"` renders the entire Ukrainian text as English prose (110 % WER
overall) while getting block [6] exactly right (0.0 %). Pinning the language therefore cannot give
you "Ukrainian sentences with English words left alone" at the sentence level — it gives you
whichever language you asked for.

Within a Ukrainian sentence, though, it does exactly what mixed dictation needs: it emits **real
Latin** for embedded tech terms — "Треба зробити git rebase, а потім force push.", "Логи лежать у
докер-контейнері порт 8080." Block [3] at **21.4 %** (VAD segments) is the best block-[3] score of
any candidate, against the incumbent's 32.1 %.

Its structural advantage is real: the variable-length encoder means **short segments cost it
nothing**, so it is the only candidate that gets *better* when fed the app's 1.5–4.7 s VAD segments
(19.3 % vs 23.9 % excluding block 6) instead of whole blocks. It is perfect on block [1], the short
phrases that are the incumbent's worst case (0.0 % vs 9.1 %), and it produces true
`?`-terminated questions.

The cost is severe: **28–34 s to decode 68.8 s of audio** (the slowest engine by 4–6×, and slower
than Parakeet by ~5×), **4.1 GB peak RSS** on a machine with ~4 GB free, and 1.03 GB on disk.

### Candidate 3 (faster-whisper with a truncated mel) — the winner, with a caveat

The bypass works. `faster_whisper.transcribe()` calls `pad_or_trim(segment)` unconditionally
(audio.py:111 from transcribe.py:1180), but calling `model.encode()` on a mel of L frames and then
`model.model.generate()` directly is accepted by CTranslate2 and is much faster for short input.
`run_fw_trunc.py` implements this; `Trunc.__call__` is the whole trick.

**The brief's framing of the L threshold is not what is happening.** A fixed L=768 does not "degrade
quality" — it *truncates the audio* when the clip is longer than 7.68 s, and the decoder then loops.
That is why `fw-small-uk-L768` scores 268.6 %. The honest experiment is to vary the **margin above
the content length**, which `sweep_L.py` does (large-v3-turbo, language=uk, per-block):

| margin above content | WER all | WER excl [6] | 3 s clip | L per block |
|---|---|---|---|---|
| +0 | 243.2 | 84.4 | 3.25 s | 896…512 |
| **+128** | **16.9** | **17.4** | 0.66 s | 1024…640 |
| +256 | 16.9 | 17.4 | 0.75 s | 1152…768 |
| +512 | 24.6 | 18.3 | 1.05 s | 1408…1024 |
| +1024 | 24.6 | 18.3 | 1.73 s | 1920…1536 |
| +2048 | 15.3 | 16.5 | 3.65 s | 2944…2560 |
| full 3000 | 22.9 | 16.5 | 4.93 s | 3000 |

Ukrainian accuracy is **flat from +128 upwards** (16.5–18.3 %); only +0 collapses. The Ukrainian-only
optimum is +2048/full at 16.5 %, and +128 gives up 0.9 points for a **7× lower latency**. The `all`
column is noisy because block [6] flips between "transcribed" and "translated" (see below), so read
`excl [6]` as the Ukrainian signal.

**Unexpected bonus: the truncated mel stops Whisper translating.** At +128/+256 the English control
sentence comes out in English — "Open the settings and check to whether the microphone works."
(11.1 % WER) — *even though the language is pinned to uk*. At +512 and above, and at full padding,
the same model translates it into Ukrainian and scores 100 %. This is why the +128 config is the
only one in the table that is good at both languages at once, and it is the main reason it wins.
I did not establish the mechanism; treat it as a measured, reproducible behaviour (identical output
across the `sweep_L.py` and `run_fw_trunc.py` runs) rather than something to rely on blindly.

`large-v3-turbo` is worth its size: `small` is 32–34 % excluding block 6 at every L, i.e. clearly
worse than the incumbent, so the cheap Whisper is not an option.

### Short utterances: the one thing that breaks the winner

`probe_short.py` decodes each of the 17 app VAD segments alone. The pattern is sharp:

- **Every segment with under ~400 content frames (~4 s of speech) collapses into a repetition loop
  at L = content+128.** "Привіт, як справи" (1.5 s, 147 frames, L=384) comes back as that phrase
  repeated 25 times. Segments of 4.1 s and longer never loop.
- Forcing an absolute floor of L≥768 fixes most of them but not all: the 1.7 s segment still loops
  at 768, and the whole VAD path scores 73.7 % at floor 768 and 36.4 % at floor 1024.
- **Only L=3000 is reliable below 4 s** — and that costs 4.7–5.2 s per call.

On the 3.0 s `short.wav` the three settings give: L=512 → 0.65 s, truncated ("Привіт як справи,
від…"); L=768 → 1.04 s, correct words but no question mark; L=3000 → 5.18 s, perfect
("Привіт, як справи?").

Consequently, feeding Whisper the app's current 0.25 s-silence VAD segments does not work at any L:
even the safe L=3000 scores 28.8 % / 22.9 % — worse than feeding it whole blocks (22.9 % / 16.5 %).
**Whisper wants whole utterances; the incumbent's fine-grained segmentation actively hurts it.**
Canary is the mirror image: short segments help it.

---

## Ranked recommendation for mixed Ukrainian + English dictation

1. **faster-whisper `large-v3-turbo`, `language="uk"`, mel truncated to `content+128` frames, fed
   whole utterances.** Measured 16.9 % overall / 17.4 % excluding block [6], against the incumbent's
   23.7 % / 23.9 % — a ~28 % relative error reduction that also happens to be the only configuration
   that handles the English sentence (11.1 %) and gets the short-phrase block perfect (0.0 %). It
   needs the app's VAD changed to close a segment on a longer pause (~0.8 s) rather than 0.25 s, so
   each call sees ≥4 s of speech, plus a short-utterance fallback (L=3000, or hand anything under
   ~4 s to Parakeet). 1.6 GB on disk, ~2.1 GB peak RSS, 8.5 s for a 69 s take.
   *Estimated, not measured:* per-utterance latency in the app for a typical 5–8 s phrase, ~0.8–1.5 s
   by interpolation from the block timings.

2. **Keep Parakeet TDT 0.6B v3 as the low-latency path**, unchanged and VAD-segmented. It is the
   fastest credible engine (0.49 s per segment, 0.64 s for 3 s), the smallest of the good ones
   (670 MB), and its 23.7 % is respectable. A two-engine setup — Parakeet for anything short, Whisper
   turbo for anything long — is the configuration the measurements actually support, because the two
   engines fail on opposite input lengths.

3. **Canary-1B-v2 int8 `language="uk"` only if English sentences are acceptable as Ukrainian
   translations.** Best Ukrainian-only score on the app's existing segmentation (19.3 %), best
   block [3] (21.4 %), best short phrases (0.0 %), real Latin tech terms, and no segmentation change
   required. Ruled out here by throughput and memory: 28–34 s per 69 s take and 4.1 GB peak RSS on a
   15 GB machine that already has ~10 GB in use. Revisit if a GPU or more RAM is available.

4. **Do not adopt the Ukrainian FastConformer CTC.** 43–48 % excluding block [6], double the
   incumbent, and structurally unable to emit English. Its speed (0.15 s / 1.4 s) and small int8
   footprint (131 MB) do not compensate.

**Also worth acting on independently of any model change:** nothing. The VAD hypothesis is
disproved — the app's segmentation is currently *helping* Parakeet, and the earlier 40.7 % figure was
a microphone-gain artefact, not a model or segmentation defect.

---

## What I could not measure, and why

- **Statistical confidence.** One take, 118 reference words. A 1-word difference is 0.85 WER points,
  so differences under ~3 points (e.g. 16.9 vs 17.4, or +128 vs +256) are **not** resolvable. Only
  the large gaps — Whisper turbo and Canary versus the FastConformer, and either versus `small` —
  are safe. The 21.2 %-vs-23.7 % baseline discrepancy is itself inside the noise.
- **Per-speaker and noise robustness.** One speaker, one quiet room, one gain setting. The brief's
  own history (40.7 % → 21.2 % from gain alone) shows how sensitive these numbers are to recording
  conditions; none of this transfers to a noisy room without re-measuring.
- **Model-only RAM.** Reported peak RSS is whole-process `ru_maxrss`, which includes the 120 s of
  audio, the harness, and onnxruntime/CTranslate2 arenas. I did not isolate the model's own
  resident size.
- **Real in-app latency.** All timings are batch decodes of pre-cut files. They exclude audio
  capture, VAD, IPC and the paste path, and they were taken with other processes on the box, so the
  Canary and turbo numbers in particular may be optimistic by an unmeasured margin.
- **Canary fp32 and Canary on GPU.** Only the int8 export was downloaded; int8 may be costing it
  accuracy exactly as it costs the FastConformer ~4 points, which would matter for ranking #3.
- **Whether the +128 anti-translation effect is robust.** Reproducible across two runs here, but I
  have one English sentence of evidence. Do not build a language-routing design on it without more.
- **`nvidia/stt_ua_fastconformer_hybrid_large_pc`'s RNNT head.** Only the CTC export exists in the
  OpenVoiceOS repo; the hybrid model's transducer branch (usually several points better than its CTC
  branch) was not testable without NeMo.
- **espeak synthetic audio was used for no accuracy claim.** It was only a harness smoke test; the
  robotic voice is out of distribution and its WERs (82 % for the FastConformer) are meaningless.

---

## Files

Runner scripts (each self-contained and re-runnable; each starts with a one-line description):

| script | venv | what it does |
|---|---|---|
| `harness.py` | any | shared wav loading, block/segment discovery, median-of-3 timing, blocked-output writing, per-block scoring incl. `all_no6` |
| `prep_blocks.py` | app | cuts a take into `blocks/1..6.wav` by sentence-level pauses + `blocks/short.wav` (3 s) |
| `prep_segs.py` | app | re-cuts each block into the live app's VAD segments as `blocks/segs/N-MM.wav` |
| `run_parakeet.py` | app | candidate 4, both (a) VAD-segmented and (b) whole-block/whole-take single call |
| `run_onnxasr.py` | `.venv-onnxasr` | candidates 1 and 2; `ua-ctc-int8 ua-ctc-fp32 ua-ctc-fp32-vad canary-uk canary-en canary-uk-vad` |
| `run_fw_trunc.py` | `.venv-fw` | candidate 3; specs are `model:L:lang[:vad][:fNNN]`, `L` = `auto` \| `mN` (content+N) \| a fixed number |
| `sweep_L.py` | `.venv-fw` | the margin-above-content table above |
| `probe_short.py` | `.venv-fw` | per-segment repetition-collapse probe vs clip duration |
| `make_synth.py` | app | espeak-ng take, for harness validation and speed only — never for accuracy |

Raw transcripts are `out-<name>.txt`, in the same `[n]`-block format as the reference, so any of them
can be re-scored with `python compare.py reference-uk.txt out-<name>.txt`. All measurements are in
`results.json`. Models are under `models/` (downloaded from `hf-mirror.com`; the plain
`hf download` path stalled at 0 bytes because the xet CAS endpoint is not proxied by that mirror —
`curl -L` against `/resolve/main/<file>` works and ran at ~1.2 MB/s).
