"""A/B step 1: build two segmentations of the real recording and decode both with Parakeet.

  raw       - what the app does today: TEN VAD boundaries at the CURRENT 0.8 s silence
  coalesced - the proposal: merge consecutive segments until each block is >= 4 s,
              taking the contiguous audio span so natural gaps are preserved
"""
import json, time, wave
from pathlib import Path
import numpy as np, sherpa_onnx
from ten_vad import TenVad

SRC = "recordings/20260930-read-uk.wav"
HOP, TH, MIN_SIL, MIN_SEG, TARGET = 256, 0.5, int(0.8*16000), 4800, 4.0

with wave.open(SRC) as w:
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

vad = TenVad(hop_size=HOP, threshold=TH)
spans, start, sil = [], None, 0
for i in range(0, len(pcm)-HOP, HOP):
    prob, _ = vad.process(pcm[i:i+HOP])
    if prob >= TH:
        if start is None: start = i
        sil = 0
    elif start is not None:
        sil += HOP
        if sil >= MIN_SIL:
            spans.append((start, i)); start, sil = None, 0
if start is not None: spans.append((start, len(pcm)))
spans = [(a, b) for a, b in spans if b-a >= MIN_SEG]

merged, cur = [], None
for a, b in spans:
    cur = (cur[0], b) if cur else (a, b)
    if (cur[1]-cur[0])/16000 >= TARGET:
        merged.append(cur); cur = None
if cur: merged.append(cur)

for name, ss in (("raw", spans), ("coalesced", merged)):
    d = Path(f"ab-segs/{name}"); d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.wav"): f.unlink()
    for n, (a, b) in enumerate(ss):
        with wave.open(str(d/f"{n:02d}.wav"), "w") as o:
            o.setnchannels(1); o.setsampwidth(2); o.setframerate(16000)
            o.writeframes(pcm[a:b].tobytes())
    print(f"{name}: {len(ss)} сегментів, тривалості "
          f"{', '.join(f'{(b-a)/16000:.1f}' for a, b in ss)}")

MD = Path.home()/".local/share/parakeet-dictation/models/desktop"
rec = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=str(MD/"encoder.int8.onnx"), decoder=str(MD/"decoder.int8.onnx"),
    joiner=str(MD/"joiner.int8.onnx"), tokens=str(MD/"tokens.txt"),
    num_threads=8, sample_rate=16000, feature_dim=128, provider="cpu",
    model_type="nemo_transducer", decoding_method="greedy_search")

out = {}
for name in ("raw", "coalesced"):
    texts, times = [], []
    for f in sorted(Path(f"ab-segs/{name}").glob("*.wav")):
        with wave.open(str(f)) as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768
        a = np.concatenate([a, np.zeros(8000, np.float32)])   # trailing pad the TDT needs
        t0 = time.perf_counter()
        s = rec.create_stream(); s.accept_waveform(16000, a); rec.decode_stream(s)
        times.append(time.perf_counter()-t0); texts.append(s.result.text.strip())
    Path(f"out-ab-parakeet-{name}.txt").write_text(" ".join(t for t in texts if t) + "\n")
    out[name] = {"times": times, "n": len(times)}
    print(f"parakeet/{name}: сума {sum(times):.2f}s, останній сегмент {times[-1]:.2f}s")
Path("ab-parakeet-times.json").write_text(json.dumps(out))
