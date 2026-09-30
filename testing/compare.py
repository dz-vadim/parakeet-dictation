"""Compare dictated text against the reference, per block.

WER here counts word-level edits (Levenshtein over word lists) — the standard
measure, so numbers are comparable to published benchmarks.
"""
import re, sys, unicodedata
from pathlib import Path

def blocks(text):
    """Split the reference/hypothesis into [n] blocks; text before any [n] is block 0."""
    out, cur, key = {}, [], "0"
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = re.match(r"\[(\d+)\]", line)
        if m:
            if cur:
                out[key] = " ".join(cur)
            key, cur = m.group(1), []
            continue
        if line.strip():
            cur.append(line.strip())
    if cur:
        out[key] = " ".join(cur)
    return out

def norm(s):
    """Lowercase, drop punctuation, normalise apostrophes — so WER measures words,
    not the model's punctuation choices (those get scored separately)."""
    s = unicodedata.normalize("NFC", s.lower())
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"[^\w'\s]", " ", s)
    return s.split()

def wer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0, 0, 0
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
    return d[len(r)][len(h)] / len(r) * 100, d[len(r)][len(h)], len(r)

def punct_rate(s):
    """How much punctuation the engine produced per 100 words — the thing
    Ukrainian-only CTC models cannot do at all."""
    words = max(len(norm(s)), 1)
    return len(re.findall(r"[.,!?—;:]", s)) / words * 100

LABELS = {"1": "короткі фрази", "2": "укр. літери", "3": "змішана термінологія",
          "4": "числа й пунктуація", "5": "довге речення", "6": "англійська"}

if __name__ == "__main__":
    ref_path = Path(sys.argv[1] if len(sys.argv) > 1 else "reference-uk.txt")
    hyp_path = Path(sys.argv[2] if len(sys.argv) > 2 else "dictated.txt")
    ref, hyp = blocks(ref_path.read_text()), blocks(hyp_path.read_text())

    print(f"{'блок':22} {'WER':>7} {'помилок':>9} {'пунктуація':>12}")
    print("-" * 54)
    tot_e = tot_w = 0
    for k in sorted(ref, key=lambda x: int(x)):
        if k not in hyp:
            print(f"{LABELS.get(k, k):22} {'—':>7} {'(немає)':>9}")
            continue
        w, e, n = wer(ref[k], hyp[k])
        tot_e += e
        tot_w += n
        print(f"{LABELS.get(k, k):22} {w:6.1f}% {e:>4}/{n:<4} {punct_rate(hyp[k]):9.1f}/100сл")
    if tot_w:
        print("-" * 54)
        print(f"{'РАЗОМ':22} {tot_e / tot_w * 100:6.1f}% {tot_e:>4}/{tot_w:<4}")
