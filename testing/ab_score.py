"""A/B step 3: score the four combinations against the reference and show what each costs."""
import json, re
from pathlib import Path
from compare import blocks, wer, punct_rate, norm

REF = " ".join(blocks(Path("reference-uk.txt").read_text()).values())

def loops(text):
    """Count runs where the same 4-word window repeats 3+ times — Whisper's collapse mode."""
    w = norm(text); hits = 0
    for i in range(len(w)-12):
        g = w[i:i+4]
        if w[i+4:i+8] == g and w[i+8:i+12] == g: hits += 1
    return hits

def latin_words(text):
    """Words carrying Latin letters — in this reference only ~10 should (the English block)."""
    return sum(1 for t in text.split() if re.search(r'[A-Za-z]', t))

pk = json.loads(Path("ab-parakeet-times.json").read_text())
wh = json.loads(Path("ab-whisper-times.json").read_text())

print(f"{'варіант':34} {'WER':>7} {'сегм':>5} {'сума':>7} {'останній':>9} {'лат.слів':>9} {'зриви':>6}")
print("-"*82)
rows = []
for eng, times in (("parakeet", pk), ("whisper", wh)):
    for seg in ("raw", "coalesced"):
        txt = Path(f"out-ab-{eng}-{seg}.txt").read_text()
        w_, e, n = wer(REF, txt)
        t = times[seg]["times"]
        label = f"{eng} / {'VAD як зараз' if seg=='raw' else 'злиті до 4с'}"
        print(f"{label:34} {w_:6.1f}% {len(t):5} {sum(t):6.1f}s {t[-1]:8.2f}s {latin_words(txt):9} {loops(txt):6}")
        rows.append((label, w_, sum(t), t[-1]))
print()
base = [r for r in rows if r[0].startswith("parakeet / VAD")][0]
for label, w_, tot, last in rows:
    if label == base[0]: continue
    print(f"{label:34} WER {w_-base[1]:+5.1f} пункта, час {tot-base[2]:+6.1f}s, "
          f"очікування після відпускання {last-base[3]:+5.2f}s")
