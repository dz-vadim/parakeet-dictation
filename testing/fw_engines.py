"""Run the VAD segments through Whisper with the language pinned, for comparison
against Parakeet's auto-detection. Usage: fw_engines.py segs/ [model ...]"""
import sys, time, statistics
from pathlib import Path
from faster_whisper import WhisperModel
from compare import blocks, wer, punct_rate

REF = " ".join(blocks(Path("reference-uk.txt").read_text()).values())
segdir = Path(sys.argv[1] if len(sys.argv) > 1 else "segs")
models = sys.argv[2:] or ["small", "turbo"]
segs = sorted(segdir.glob("*.wav"))
print(f"сегментів: {len(segs)}")

for mid in models:
    m = WhisperModel(mid, device="cpu", compute_type="int8", cpu_threads=12)
    list(m.transcribe(str(segs[0]), language="uk", beam_size=1)[0])   # warm up
    texts, times = [], []
    for s in segs:
        t0 = time.perf_counter()
        parts, _ = m.transcribe(str(s), language="uk", beam_size=1,
                                condition_on_previous_text=False)
        txt = " ".join(p.text.strip() for p in parts)
        times.append(time.perf_counter() - t0)
        texts.append(txt)
    full = " ".join(t for t in texts if t)
    w, e, n = wer(REF, full)
    Path(f"out-whisper-{mid}-uk.txt").write_text(full + "\n")
    print(f"\n--- whisper {mid}, language=uk")
    print(f"    WER {w:.1f}%  ({e}/{n})  медіанна затримка {statistics.median(times):.2f}s/сегмент"
          f"  пунктуація {punct_rate(full):.1f}/100сл")
    print("    " + full[:400])
