"""Cut a read-aloud take into the six reference blocks. The user's inter-block pauses are not
reliably longer than the inter-sentence ones, so we find sentence-level VAD groups and fold them
into blocks with an explicit layout (reference-uk.txt = 3+3+3+3+1+1 spoken sentences).
Also writes blocks/short.wav, a fixed 3 s clip, for the latency measurement.
Usage: prep_blocks.py TAKE.wav [layout=3,3,3,3,1,1] [gap=0.8]"""
import sys
from pathlib import Path

import numpy as np
from ten_vad import TenVad

from harness import BLOCKDIR, load_wav, save_wav

HOP = 256


def speech_mask(audio, threshold=0.5):
    pcm = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    vad = TenVad(hop_size=HOP, threshold=threshold)
    return np.array([vad.process(pcm[i:i + HOP])[0] for i in range(0, len(pcm) - HOP, HOP)]) >= threshold


def group(mask, gap_s, min_s=0.4):
    """Speech regions in frames, merging anything separated by less than gap_s of silence."""
    gapf, out, start, gap = int(gap_s * 16000 / HOP), [], None, 0
    for i, v in enumerate(mask):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > gapf:
                out.append((start, i - gap)); start, gap = None, 0
    if start is not None:
        out.append((start, len(mask)))
    return [(a, b) for a, b in out if (b - a) * HOP / 16000 >= min_s]


def main():
    take = Path(sys.argv[1])
    layout = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "3,3,3,3,1,1").split(",")]
    gap = float(sys.argv[3]) if len(sys.argv) > 3 else 0.8
    audio = load_wav(take)
    sents = group(speech_mask(audio), gap, min_s=1.0)
    print(f"{take.name}: {len(audio)/16000:.1f}s, {len(sents)} sentence groups at gap {gap}s "
          f"(layout wants {sum(layout)})")
    if len(sents) != sum(layout):
        for gg in np.arange(0.4, 3.01, 0.05):
            print(f"  gap {gg:.2f}s -> {len(group(speech_mask(audio), gg, 1.0))} groups")
        sys.exit("sentence-group count does not match the layout")

    BLOCKDIR.mkdir(exist_ok=True)
    for old in BLOCKDIR.glob("*.wav"):
        old.unlink()
    i = 0
    for n, k in enumerate(layout, 1):
        a = sents[i][0]; b = sents[i + k - 1][1]; i += k
        # keep 0.25 s of room tone each side so models see a clean onset/offset
        lo = max(0, int(a * HOP - 0.25 * 16000)); hi = min(len(audio), int(b * HOP + 0.25 * 16000))
        save_wav(BLOCKDIR / f"{n}.wav", audio[lo:hi])
        print(f"  block {n}: {a*HOP/16000:7.1f}-{b*HOP/16000:7.1f}s  ({(hi-lo)/16000:5.1f}s, "
              f"{k} sentence{'s' if k > 1 else ''})")

    # fixed 3.0 s window of block 1 from its first speech frame -> comparable latency numbers
    b1 = load_wav(BLOCKDIR / "1.wav")
    a = group(speech_mask(b1), 0.25, min_s=0.3)[0][0]
    lo = max(0, int(a * HOP - 2400))
    save_wav(BLOCKDIR / "short.wav", b1[lo:lo + 3 * 16000])
    print(f"  short.wav: {len(b1[lo:lo+3*16000])/16000:.1f}s")


if __name__ == "__main__":
    main()
