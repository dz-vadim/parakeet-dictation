"""Candidate 3: faster-whisper with language="uk" and a TRUNCATED mel -- bypasses transcribe()'s
unconditional pad_or_trim(...,3000) so CTranslate2 sees only L frames. Run with .venv-fw.
Usage: run_fw_trunc.py [model:L ...]   e.g. small:auto small:768 large-v3-turbo:auto"""
import sys
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
from faster_whisper.tokenizer import Tokenizer
from faster_whisper.transcribe import get_suppressed_tokens

import harness
from harness import HERE, block_files, load_wav, median3, peak_rss_mb, record, write_blocked

CACHE = Path.home() / ".cache/huggingface/hub"
REPO = {"small": "models--Systran--faster-whisper-small",
        "large-v3-turbo": "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"}


class Trunc:
    """faster-whisper decode on a mel truncated to L frames instead of the hardcoded 3000."""

    def __init__(self, mid, threads=12, lang="uk"):
        self.mid = mid
        self.lang = lang
        self.m = WhisperModel(mid, device="cpu", compute_type="int8", cpu_threads=threads)
        self.tok = Tokenizer(self.m.hf_tokenizer, self.m.model.is_multilingual,
                             task="transcribe", language=lang)
        self.suppress = get_suppressed_tokens(self.tok, [-1])

    def frames(self, audio):
        return self.m.feature_extractor(audio).shape[-1] - 1

    def __call__(self, audio, L=None, margin=None, floor=128):
        feats = self.m.feature_extractor(audio)
        content = feats.shape[-1] - 1
        if margin is not None:                        # content + margin, rounded up to /128
            L = int(np.ceil((content + margin) / 128) * 128)
        elif L is None:                               # default: content + ~500
            L = int(np.ceil((content + 500) / 128) * 128)
        # `floor` matters: below about 768 frames the decoder collapses into repetition loops
        # regardless of how much headroom there is above the content length.
        L = min(3000, max(int(floor), int(L)))
        enc = self.m.encode(pad_or_trim(feats, L))
        prompt = self.m.get_prompt(self.tok, [], without_timestamps=True)
        res = self.m.model.generate(enc, [prompt], beam_size=1, max_length=self.m.max_length,
                                    suppress_blank=True, suppress_tokens=self.suppress)[0]
        return self.tok.decode(res.sequences_ids[0]).strip(), L


def bench(mid, Lspec, lang="uk", vad=False, floor=128):
    t = Trunc(mid, lang=lang)
    name = f"fw-{mid}-{lang}-L{Lspec}" + (f"-f{floor}" if floor != 128 else "") + ("-vad" if vad else "")
    blocks = block_files()
    short, take = harness.short_clip(), harness.take_audio()
    # "auto" = content+500; "mN" = content+N (the margin form); a bare number = a fixed L
    L, margin = None, None
    if Lspec.startswith("m"):
        margin = int(Lspec[1:])
    elif Lspec != "auto":
        L = int(Lspec)
    t(short, L, margin, floor)                                         # warm up

    texts, Ls = [], []
    if vad:   # feed the live app's VAD segments, i.e. the path the app would actually take
        for p in blocks:
            parts = []
            for sp in harness.seg_files(p.stem):
                txt, used = t(load_wav(sp), L, margin, floor); parts.append(txt); Ls.append(used)
            texts.append(" ".join(x for x in parts if x))
    else:
        for p in blocks:
            txt, used = t(load_wav(p), L, margin, floor); texts.append(txt); Ls.append(used)
    write_blocked(name, texts)

    t_short, _ = median3(lambda: t(short, L, margin, floor))
    t_take, _ = median3(lambda: t(take, L, margin, floor))
    sc = harness.score_blocked(name)
    record(name, scores=sc, t_short_s=round(t_short, 3), t_take_s=round(t_take, 2),
           size_mb=round(harness.dir_size_mb(CACHE / REPO[mid]), 1),
           peak_rss_mb=round(peak_rss_mb(), 1), L_used=Ls,
           short_frames=t.frames(short), take_frames=t.frames(take),
           short_clip_s=round(len(short) / 16000, 2), take_s=round(len(take) / 16000, 1))
    print(f"\n=== {name}: WER {sc['all']['wer']}%  short {t_short:.2f}s  take {t_take:.2f}s  "
          f"punct {sc['all']['punct']}/100w  L per block {Ls}")
    for i, x in enumerate(texts, 1):
        print(f"  [{i}] {x}")


if __name__ == "__main__":
    for spec in (sys.argv[1:] or ["small:auto", "small:768", "small:3000",
                                  "large-v3-turbo:auto", "large-v3-turbo:768",
                                  "large-v3-turbo:3000"]):
        parts = spec.split(":")
        mid, Lspec = parts[0], parts[1]
        lang = parts[2] if len(parts) > 2 else "uk"
        vad = "vad" in parts[3:]
        floor = next((int(x[1:]) for x in parts[3:] if x.startswith("f")), 128)
        try:
            bench(mid, Lspec, lang, vad, floor)
        except Exception as e:
            print(f"\n=== {spec}: FAILED -- {type(e).__name__}: {str(e)[:300]}")
            record(f"fw-{mid}-L{Lspec}", failed=f"{type(e).__name__}: {str(e)[:300]}")
