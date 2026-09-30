"""Render reference-uk.txt to a synthetic espeak-ng take (2 s gaps between blocks). SPEED AND
PIPELINE VALIDATION ONLY -- the robotic voice is out of distribution, never quote its WER."""
import subprocess, tempfile
from pathlib import Path

import numpy as np

import compare
from harness import HERE, load_wav, save_wav

blocks = compare.blocks((HERE / "reference-uk.txt").read_text(encoding="utf-8"))
parts = []
with tempfile.TemporaryDirectory() as td:
    for k in sorted(blocks, key=int):
        text = blocks[k].split("—")[-1] if k == "0" else blocks[k]
        # strip the block's own ALL-CAPS heading line if compare kept it
        wav = Path(td) / f"{k}.wav"
        voice = "en-us" if k == "6" else "uk"
        subprocess.run(["espeak-ng", "-v", voice, "-s", "150", "-w", str(wav), text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                        "-ar", "16000", "-ac", "1", str(wav.with_suffix(".16k.wav"))], check=True)
        parts.append(load_wav(wav.with_suffix(".16k.wav")))
        parts.append(np.zeros(2 * 16000, np.float32))

take = np.concatenate(parts)
save_wav(HERE / "synth-take.wav", take)
print(f"synth-take.wav: {len(take)/16000:.1f}s from {len(blocks)} blocks")
