"""A/B step 2: decode both segmentations with Whisper large-v3-turbo, language pinned to uk,
mel truncated to content+128 (the setting the benchmark found best). Run with .venv-fw."""
import json, time, wave
from pathlib import Path
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
from faster_whisper.tokenizer import Tokenizer
from faster_whisper.transcribe import get_suppressed_tokens

m = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8", cpu_threads=12)
tok = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language="uk")
suppress = get_suppressed_tokens(tok, [-1])

def decode(audio, margin=128, floor=128):
    feats = m.feature_extractor(audio)
    content = feats.shape[-1] - 1
    L = int(np.ceil((content + margin) / 128) * 128)
    L = min(3000, max(floor, L))
    enc = m.encode(pad_or_trim(feats, L))
    prompt = m.get_prompt(tok, [], without_timestamps=True)
    res = m.model.generate(enc, [prompt], beam_size=1, max_length=m.max_length,
                           suppress_blank=True, suppress_tokens=suppress)[0]
    return tok.decode(res.sequences_ids[0]).strip()

def load(p):
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768

decode(load("ab-segs/raw/00.wav"))   # warm up

out = {}
for name in ("raw", "coalesced"):
    texts, times = [], []
    for f in sorted(Path(f"ab-segs/{name}").glob("*.wav")):
        a = load(f)
        t0 = time.perf_counter(); txt = decode(a); times.append(time.perf_counter()-t0)
        texts.append(txt)
    Path(f"out-ab-whisper-{name}.txt").write_text(" ".join(t for t in texts if t) + "\n")
    out[name] = {"times": times, "n": len(times)}
    print(f"whisper/{name}: сума {sum(times):.2f}s, останній сегмент {times[-1]:.2f}s")
Path("ab-whisper-times.json").write_text(json.dumps(out))
