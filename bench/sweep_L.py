"""How much mel padding above the real content length faster-whisper actually needs: decode each
block at L = content_frames + margin (rounded up to /128) and report WER + time per margin.
This is the honest version of the "quality degrades below L=768" claim -- a FIXED small L simply
truncates the audio, so we vary the MARGIN above content instead. Run with .venv-fw.
Usage: sweep_L.py [model]"""
import statistics
import sys
import time

import numpy as np

import harness
from harness import block_files, load_wav
from run_fw_trunc import Trunc

mid = sys.argv[1] if len(sys.argv) > 1 else "large-v3-turbo"
t = Trunc(mid)
blocks = [load_wav(p) for p in block_files()]
short = harness.short_clip()
t(short)                                                                # warm up


def L_for(audio, margin):
    if margin is None:
        return 3000
    return min(3000, int(np.ceil((t.frames(audio) + margin) / 128) * 128))


def median3(audio, L):
    ts = []
    for _ in range(3):
        t0 = time.perf_counter(); t(audio, L); ts.append(time.perf_counter() - t0)
    return statistics.median(ts)


print(f"{mid}, language=uk   block content frames: {[t.frames(a) for a in blocks]}, "
      f"short ({len(short)/16000:.1f}s): {t.frames(short)}")
print(f"{'margin':>7} {'WER all':>8} {'WER no6':>8} {'short s':>8} {'blocks s':>9}  L per block")
for margin in [0, 128, 256, 512, 1024, 2048, None]:
    texts, Ls, times = [], [], []
    for a in blocks:
        L = L_for(a, margin)
        t0 = time.perf_counter(); txt, used = t(a, L); times.append(time.perf_counter() - t0)
        texts.append(txt); Ls.append(used)
    name = f"fw-{mid}-uk-margin{'full' if margin is None else margin}"
    harness.write_blocked(name, texts)
    sc = harness.score_blocked(name)
    st = median3(short, L_for(short, margin))
    print(f"{str(margin) if margin is not None else 'full':>7} {sc['all']['wer']:7.1f}% "
          f"{sc['all_no6']['wer']:7.1f}% {st:8.2f} {sum(times):9.2f}  {Ls}")
    harness.record(name, scores=sc, t_short_s=round(st, 3), t_blocks_s=round(sum(times), 2),
                   L_used=Ls, margin=margin, size_mb=None, peak_rss_mb=round(harness.peak_rss_mb(), 1))
