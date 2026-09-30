"""Why the truncated-mel trick breaks on short clips: decode every VAD segment on its own at
L = content+128 and flag repetition collapse, against the segment's speech duration. Run with .venv-fw."""
import re
import sys

from harness import BLOCKDIR, block_files, load_wav
from run_fw_trunc import Trunc

t = Trunc(sys.argv[1] if len(sys.argv) > 1 else "large-v3-turbo")


def looped(s):
    """A crude repetition detector: any 3+ word phrase repeated 3+ times."""
    w = s.split()
    for n in range(3, 9):
        for i in range(len(w) - 3 * n):
            if w[i:i + n] == w[i + n:i + 2 * n] == w[i + 2 * n:i + 3 * n]:
                return True
    return False


print(f"{'seg':8} {'secs':>5} {'frames':>7} {'L':>5} {'loop':>5}  text")
for p in block_files():
    for sp in sorted((BLOCKDIR / "segs").glob(f"{p.stem}-*.wav")):
        a = load_wav(sp)
        for margin, floor in [(128, 128), (128, 768), (None, 3000)]:
            txt, L = t(a, None if margin else 3000, margin, floor)
            print(f"{sp.stem:8} {len(a)/16000:5.1f} {t.frames(a):7} {L:5} "
                  f"{'LOOP' if looped(txt) else '':>5}  {txt[:90]}")
