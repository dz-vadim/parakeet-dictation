"""Spontaneous A/B: same normalised coalesced segments through Whisper turbo, language=uk."""
import time, wave
from pathlib import Path
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
from faster_whisper.tokenizer import Tokenizer
from faster_whisper.transcribe import get_suppressed_tokens

m = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8", cpu_threads=12)
tok = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language="uk")
sup = get_suppressed_tokens(tok, [-1])

def decode(a, margin=128):
    f = m.feature_extractor(a); content = f.shape[-1]-1
    L = min(3000, max(128, int(np.ceil((content+margin)/128)*128)))
    enc = m.encode(pad_or_trim(f, L))
    r = m.model.generate(enc, [m.get_prompt(tok, [], without_timestamps=True)],
                         beam_size=1, max_length=m.max_length,
                         suppress_blank=True, suppress_tokens=sup)[0]
    return tok.decode(r.sequences_ids[0]).strip()

def load(p):
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768

files = sorted(Path("ab2-segs/norm-coalesced").glob("*.wav"))
decode(load(files[0]))
texts, times = [], []
for f in files:
    t0 = time.perf_counter(); texts.append(decode(load(f))); times.append(time.perf_counter()-t0)
Path("out-ab2-whisper-norm-coalesced.txt").write_text(" ".join(t for t in texts if t) + "\n")
print(f"whisper: {len(files)} сегментів, декод {sum(times):.1f}s, останній {times[-1]:.2f}s, "
      f"символів {sum(len(t) for t in texts)}")
