"""Segment the recording exactly as the app does (TEN VAD, hop 256, 0.25s silence),
then transcribe each segment with Parakeet — mirroring the live dictation path."""
import time, wave
from pathlib import Path
import numpy as np, sherpa_onnx
from ten_vad import TenVad

HOP, THRESH, MIN_SIL = 256, 0.5, int(0.25 * 16000)
SEGDIR = Path("segs"); SEGDIR.mkdir(exist_ok=True)
for old in SEGDIR.glob("*.wav"):
    old.unlink()

with wave.open("real_uk_norm.wav") as w:
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

vad = TenVad(hop_size=HOP, threshold=THRESH)
segments, cur, silence = [], [], 0
for i in range(0, len(pcm) - HOP, HOP):
    chunk = pcm[i:i+HOP]
    prob, _ = vad.process(chunk)
    if prob >= THRESH:
        cur.append(chunk); silence = 0
    elif cur:
        cur.append(chunk); silence += HOP
        if silence >= MIN_SIL:
            segments.append(np.concatenate(cur)); cur, silence = [], 0
if cur:
    segments.append(np.concatenate(cur))

segments = [s for s in segments if len(s) > 16000 * 0.3]   # drop <0.3s blips
print(f"сегментів: {len(segments)}  (тривалості: {', '.join(f'{len(s)/16000:.1f}' for s in segments)})")
for n, s in enumerate(segments):
    with wave.open(str(SEGDIR/f"{n:02d}.wav"), "w") as o:
        o.setnchannels(1); o.setsampwidth(2); o.setframerate(16000); o.writeframes(s.tobytes())

MD = Path.home()/".local/share/parakeet-dictation/models/desktop"
rec = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=str(MD/"encoder.int8.onnx"), decoder=str(MD/"decoder.int8.onnx"),
    joiner=str(MD/"joiner.int8.onnx"), tokens=str(MD/"tokens.txt"),
    num_threads=8, sample_rate=16000, feature_dim=128, provider="cpu",
    model_type="nemo_transducer", decoding_method="greedy_search")

texts, times = [], []
for n, s in enumerate(segments):
    audio = s.astype(np.float32)/32768
    t0 = time.perf_counter()
    st = rec.create_stream(); st.accept_waveform(16000, audio); rec.decode_stream(st)
    times.append(time.perf_counter()-t0)
    texts.append(st.result.text)
    print(f"  [{n:02d}] {len(s)/16000:4.1f}s -> {times[-1]:4.2f}s  {st.result.text[:70]}")

Path("out-parakeet.txt").write_text(" ".join(texts) + "\n")
print(f"\nмедіанна затримка на сегмент: {sorted(times)[len(times)//2]:.2f}s")
