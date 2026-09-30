"""Spontaneous-speech A/B: coalesced-to-4s segments, Parakeet, raw vs loudness-normalised."""
import sys, time, wave
from pathlib import Path
import numpy as np, sherpa_onnx
from ten_vad import TenVad

HOP, TH, MIN_SIL, MIN_SEG, TARGET = 256, 0.5, int(0.8*16000), 4800, 4.0

def segments(pcm, coalesce):
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
    if not coalesce: return spans
    out, cur = [], None
    for a, b in spans:
        cur = (cur[0], b) if cur else (a, b)
        if (cur[1]-cur[0])/16000 >= TARGET: out.append(cur); cur = None
    if cur: out.append(cur)
    return out

MD = Path.home()/".local/share/parakeet-dictation/models/desktop"
rec = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=str(MD/"encoder.int8.onnx"), decoder=str(MD/"decoder.int8.onnx"),
    joiner=str(MD/"joiner.int8.onnx"), tokens=str(MD/"tokens.txt"),
    num_threads=8, sample_rate=16000, feature_dim=128, provider="cpu",
    model_type="nemo_transducer", decoding_method="greedy_search")

for src, coalesce, tag in (("staging/spont.wav", False, "raw-vad"),
                           ("staging/spont.wav", True,  "raw-coalesced"),
                           ("staging/spont_norm.wav", True, "norm-coalesced")):
    with wave.open(src) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    ss = segments(pcm, coalesce)
    d = Path(f"ab2-segs/{tag}"); d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.wav"): f.unlink()
    texts, times = [], []
    for n, (a, b) in enumerate(ss):
        with wave.open(str(d/f"{n:02d}.wav"), "w") as o:
            o.setnchannels(1); o.setsampwidth(2); o.setframerate(16000)
            o.writeframes(pcm[a:b].tobytes())
        au = np.concatenate([pcm[a:b].astype(np.float32)/32768, np.zeros(8000, np.float32)])
        t0 = time.perf_counter()
        s = rec.create_stream(); s.accept_waveform(16000, au); rec.decode_stream(s)
        times.append(time.perf_counter()-t0); texts.append(s.result.text.strip())
    Path(f"out-ab2-parakeet-{tag}.txt").write_text(" ".join(t for t in texts if t) + "\n")
    print(f"{tag}: {len(ss)} сегментів, мовлення {sum(b-a for a,b in ss)/16000:.0f}s, "
          f"декод {sum(times):.1f}s, символів {sum(len(t) for t in texts)}")
