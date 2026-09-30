"""Cut each blocks/N.wav into the live app's VAD segments (TEN VAD hop 256, threshold 0.5,
0.25 s min silence, <0.3 s dropped) as blocks/segs/N-MM.wav, so engines in other venvs can be
fed the same segmentation the app uses without needing ten_vad installed. Run with the APP venv."""
from pathlib import Path

import numpy as np

from harness import BLOCKDIR, block_files, load_wav, save_wav
from run_parakeet import app_segments

out = BLOCKDIR / "segs"
out.mkdir(exist_ok=True)
for old in out.glob("*.wav"):
    old.unlink()
for p in block_files():
    segs = app_segments(load_wav(p))
    for n, s in enumerate(segs):
        save_wav(out / f"{p.stem}-{n:02d}.wav", s)
    print(f"block {p.stem}: {len(segs)} segments "
          f"({', '.join(f'{len(s)/16000:.1f}' for s in segs)})")
