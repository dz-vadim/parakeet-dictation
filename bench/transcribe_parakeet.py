"""Transcribe a wav with the Parakeet model the dictation app uses."""
import sys, time, wave, numpy as np, sherpa_onnx
from pathlib import Path

MD = Path.home()/".local/share/parakeet-dictation/models/desktop"
rec = sherpa_onnx.OfflineRecognizer.from_transducer(
    encoder=str(MD/"encoder.int8.onnx"), decoder=str(MD/"decoder.int8.onnx"),
    joiner=str(MD/"joiner.int8.onnx"), tokens=str(MD/"tokens.txt"),
    num_threads=8, sample_rate=16000, feature_dim=128, provider="cpu",
    model_type="nemo_transducer", decoding_method="greedy_search")

with wave.open(sys.argv[1]) as w:
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768
t0 = time.perf_counter()
s = rec.create_stream(); s.accept_waveform(16000, audio); rec.decode_stream(s)
print(f"[{time.perf_counter()-t0:.1f}s для {len(audio)/16000:.0f}s аудіо]")
print(repr(s.result.text))
