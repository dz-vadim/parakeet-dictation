"""Candidate 4: Parakeet TDT 0.6B v3 (sherpa-onnx) two ways -- (a) VAD-segmented as the live app
does, (b) each block decoded whole in one call with 0.5 s of zeros appended. Run with the APP venv."""
import sys, time
from pathlib import Path

import numpy as np
import sherpa_onnx
from ten_vad import TenVad

import harness
from harness import BLOCKDIR, block_files, load_wav, median3, peak_rss_mb, record, write_blocked

MD = Path.home() / ".local/share/parakeet-dictation/models/desktop"
HOP, THRESH, MIN_SIL = 256, 0.5, int(0.25 * 16000)   # exactly the live app's VAD settings
TAIL = np.zeros(8000, np.float32)                     # transducers need trailing frames to flush


def recognizer():
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(MD / "encoder.int8.onnx"), decoder=str(MD / "decoder.int8.onnx"),
        joiner=str(MD / "joiner.int8.onnx"), tokens=str(MD / "tokens.txt"),
        num_threads=8, sample_rate=16000, feature_dim=128, provider="cpu",
        model_type="nemo_transducer", decoding_method="greedy_search")


def decode(rec, audio, tail=True):
    a = np.concatenate([audio, TAIL]) if tail else audio
    st = rec.create_stream(); st.accept_waveform(16000, a); rec.decode_stream(st)
    return st.result.text.strip()


def app_segments(audio):
    """Reproduce the live app's segmentation: TEN VAD, hop 256, 0.25 s silence, drop <0.3 s blips."""
    pcm = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    vad = TenVad(hop_size=HOP, threshold=THRESH)
    segs, cur, sil = [], [], 0
    for i in range(0, len(pcm) - HOP, HOP):
        chunk = pcm[i:i + HOP]
        prob, _ = vad.process(chunk)
        if prob >= THRESH:
            cur.append(chunk); sil = 0
        elif cur:
            cur.append(chunk); sil += HOP
            if sil >= MIN_SIL:
                segs.append(np.concatenate(cur)); cur, sil = [], 0
    if cur:
        segs.append(np.concatenate(cur))
    return [s.astype(np.float32) / 32768.0 for s in segs if len(s) > 16000 * 0.3]


def main():
    rec = recognizer()
    blocks = block_files()
    if not blocks:
        sys.exit("no blocks/ -- run prep_blocks.py first")
    short = harness.short_clip()
    take = harness.take_audio()

    decode(rec, short)                                    # warm up

    seg_texts, whole_texts, nsegs = [], [], []
    for p in blocks:
        a = load_wav(p)
        segs = app_segments(a)
        nsegs.append(len(segs))
        seg_texts.append(" ".join(t for t in (decode(rec, s) for s in segs) if t))
        whole_texts.append(decode(rec, a))

    write_blocked("parakeet-vad", seg_texts)
    write_blocked("parakeet-whole", whole_texts)

    # whole take in ONE call -- the thing the app never does today
    t_take, one = median3(lambda: decode(rec, take))
    (Path(__file__).parent / "out-parakeet-onecall-take.txt").write_text(one + "\n", encoding="utf-8")

    t_short, _ = median3(lambda: decode(rec, short))
    # per-segment latency the user actually feels in the app
    seg1 = app_segments(load_wav(blocks[0]))[0]
    t_seg, _ = median3(lambda: decode(rec, seg1))

    for name in ("parakeet-vad", "parakeet-whole"):
        record(name, scores=harness.score_blocked(name),
               t_short_s=round(t_short, 3), t_take_s=round(t_take, 2),
               t_first_seg_s=round(t_seg, 3),
               size_mb=round(harness.dir_size_mb(MD), 1), peak_rss_mb=round(peak_rss_mb(), 1),
               segments_per_block=nsegs, short_clip_s=round(len(short) / 16000, 2),
               take_s=round(len(take) / 16000, 1))
    print(f"short {t_short:.2f}s  take(one call) {t_take:.2f}s  segs/block {nsegs}")
    print("VAD   :", " | ".join(seg_texts))
    print("WHOLE :", " | ".join(whole_texts))
    print("ONECALL take:", one[:300])
    print("scores vad  :", harness.score_blocked("parakeet-vad")["all"])
    print("scores whole:", harness.score_blocked("parakeet-whole")["all"])


if __name__ == "__main__":
    main()
