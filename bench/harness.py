"""Shared helpers for the ASR benchmark: wav loading, block wavs, timing, blocked output writing."""
import json, os, re, resource, statistics, time, wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BLOCKDIR = HERE / "blocks"


def load_wav(path):
    """Read a 16 kHz mono wav (tolerating arecord's bogus streaming header) as float32 in [-1,1]."""
    raw = Path(path).read_bytes()
    i = raw.find(b"data")
    off = (i + 8) if i > 0 else 44
    n = ((len(raw) - off) // 2) * 2
    return np.frombuffer(raw[off:off + n], dtype=np.int16).astype(np.float32) / 32768.0


def save_wav(path, audio):
    pcm = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm.tobytes())


def seg_files(block):
    """The live app's VAD segments for one block, as cut by prep_segs.py."""
    return sorted((BLOCKDIR / "segs").glob(f"{block}-*.wav"))


def block_files():
    """The six per-block wavs produced by prep_blocks.py, in reference order."""
    return sorted(BLOCKDIR.glob("[0-9].wav"))


def take_audio():
    """The whole take, concatenated from the block wavs (so it matches what we score)."""
    return np.concatenate([load_wav(p) for p in block_files()])


def short_clip():
    """~3 s clip for the short-phrase latency number: the first VAD segment of block 1."""
    return load_wav(BLOCKDIR / "short.wav")


def median3(fn, n=3):
    """Run fn n times (caller warms up first) and return (median_seconds, last_result)."""
    ts, res = [], None
    for _ in range(n):
        t0 = time.perf_counter(); res = fn(); ts.append(time.perf_counter() - t0)
    return statistics.median(ts), res


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def write_blocked(name, texts):
    """Write out-<name>.txt with [1]..[6] markers so compare.py can score it per block."""
    p = HERE / f"out-{name}.txt"
    p.write_text("".join(f"[{i+1}]\n{t.strip()}\n\n" for i, t in enumerate(texts)), encoding="utf-8")
    return p


def record(name, **fields):
    """Append one candidate's measurements to results.json."""
    p = HERE / "results.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    data[name] = fields
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def dir_size_mb(*paths):
    tot = 0
    for path in paths:
        path = Path(path)
        if path.is_dir():
            tot += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        elif path.exists():
            tot += path.stat().st_size
    return tot / 1e6


def score_blocked(name):
    """Per-block + overall WER and punctuation rate for out-<name>.txt."""
    import compare
    ref = compare.blocks((HERE / "reference-uk.txt").read_text(encoding="utf-8"))
    hyp = compare.blocks((HERE / f"out-{name}.txt").read_text(encoding="utf-8"))
    per, te, tw = {}, 0, 0
    for k in sorted(ref, key=int):
        w, e, n = compare.wer(ref[k], hyp.get(k, ""))
        per[k] = {"wer": round(w, 1), "edits": e, "words": n,
                  "punct": round(compare.punct_rate(hyp.get(k, "")), 1)}
        te += e; tw += n
    full = " ".join(hyp.get(k, "") for k in sorted(hyp, key=int))
    per["all"] = {"wer": round(te / tw * 100, 1), "edits": te, "words": tw,
                  "punct": round(compare.punct_rate(full), 1),
                  "caps": bool(re.search(r"[A-ZА-ЯЄІЇҐ]", full))}
    # block 6 is the English control sentence: a Ukrainian-only model fails it by construction,
    # so report the Ukrainian-only total too rather than letting block 6 inflate the comparison
    e6 = sum(per[k]["edits"] for k in per if k not in ("all", "6"))
    w6 = sum(per[k]["words"] for k in per if k not in ("all", "6"))
    per["all_no6"] = {"wer": round(e6 / w6 * 100, 1), "edits": e6, "words": w6}
    return per
