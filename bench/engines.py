"""Run the same recording through each engine and score it against the reference.

Reference words are flattened across blocks — engine output has no block markers,
so overall WER is the comparable number here.
"""
import re, sys, time
from pathlib import Path
from compare import blocks, wer, punct_rate

REF = " ".join(blocks(Path("reference-uk.txt").read_text()).values())
WAVS = sys.argv[1:] or ["real_uk.wav"]
results = []

def score(name, wav, text, elapsed, dur):
    w, e, n = wer(REF, text)
    results.append((name, wav, w, elapsed, dur / elapsed, punct_rate(text)))
    Path(f"out-{name}-{Path(wav).stem}.txt").write_text(text + "\n")
    print(f"\n--- {name} / {wav} — {elapsed:.1f}s ({dur/elapsed:.1f}x realtime), WER {w:.1f}%")
    print(text[:300])

import wave, numpy as np, sherpa_onnx
MD = Path.home()/".local/share/parakeet-dictation/models/desktop"
rec = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=str(MD/"encoder.int8.onnx"), decoder=str(MD/"decoder.int8.onnx"),
    joiner=str(MD/"joiner.int8.onnx"), tokens=str(MD/"tokens.txt"),
    num_threads=8, sample_rate=16000, feature_dim=128, provider="cpu",
    model_type="nemo_transducer", decoding_method="greedy_search")
for wav in WAVS:
    with wave.open(wav) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768
    dur = len(audio)/16000
    t0 = time.perf_counter()
    s = rec.create_stream(); s.accept_waveform(16000, audio); rec.decode_stream(s)
    score("parakeet-auto", wav, s.result.text, time.perf_counter()-t0, dur)

print("\n" + "="*70)
print(f"{'двигун':22} {'файл':18} {'WER':>7} {'час':>7} {'швидкість':>10}")
for name, wav, w, el, rt, pr in results:
    print(f"{name:22} {Path(wav).stem:18} {w:6.1f}% {el:6.1f}s {rt:8.1f}x")
