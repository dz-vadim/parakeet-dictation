"""Candidates 1 and 2 via onnx-asr: the Ukrainian FastConformer-Hybrid CTC (int8 and fp32) and
Canary-1B-v2 int8 with the language pinned. Run with .venv-onnxasr. Usage: run_onnxasr.py [name ...]"""
import sys, time
from pathlib import Path

import numpy as np
import onnx_asr

import harness
from harness import HERE, block_files, load_wav, median3, peak_rss_mb, record, write_blocked

MODELS = HERE / "models"


def run(name, model, size_paths, vad=False, **kw):
    blocks = block_files()
    short, take = harness.short_clip(), harness.take_audio()
    model.recognize(short, sample_rate=16000, **kw)          # warm up

    if vad:   # feed the live app's VAD segments instead of whole blocks
        texts = []
        for p in blocks:
            parts = [model.recognize(load_wav(s), sample_rate=16000, **kw).strip()
                     for s in harness.seg_files(p.stem)]
            texts.append(" ".join(t for t in parts if t))
    else:
        texts = [model.recognize(load_wav(p), sample_rate=16000, **kw).strip() for p in blocks]
    write_blocked(name, texts)

    t_short, _ = median3(lambda: model.recognize(short, sample_rate=16000, **kw))
    t_take, _ = median3(lambda: model.recognize(take, sample_rate=16000, **kw))
    sc = harness.score_blocked(name)
    record(name, scores=sc, t_short_s=round(t_short, 3), t_take_s=round(t_take, 2),
           size_mb=round(harness.dir_size_mb(*size_paths), 1), peak_rss_mb=round(peak_rss_mb(), 1),
           short_clip_s=round(len(short) / 16000, 2), take_s=round(len(take) / 16000, 1),
           opts=dict(kw, vad=vad))
    print(f"\n=== {name}: WER {sc['all']['wer']}%  short {t_short:.2f}s  take {t_take:.2f}s  "
          f"punct {sc['all']['punct']}/100w  caps {sc['all']['caps']}")
    for i, t in enumerate(texts, 1):
        print(f"  [{i}] {t}")


UA = MODELS / "ua-fastconformer"
CA = MODELS / "canary-1b-v2"

BUILDERS = {
    # Ukrainian-only CTC. int8 first; ORT may refuse it (ConvInteger), then fp32.
    "ua-ctc-int8":  lambda: (onnx_asr.load_model("nemo-conformer-ctc", UA, quantization="int8"),
                             [UA / "model.int8.onnx", UA / "vocab.txt"], {}),
    "ua-ctc-fp32":  lambda: (onnx_asr.load_model("nemo-conformer-ctc", UA),
                             [UA / "model.onnx", UA / "vocab.txt"], {}),
    "canary-uk":    lambda: (onnx_asr.load_model("nemo-conformer-aed", CA, quantization="int8"),
                             [CA / "encoder-model.int8.onnx", CA / "decoder-model.int8.onnx",
                              CA / "vocab.txt"], {"language": "uk"}),
    "canary-en":    lambda: (onnx_asr.load_model("nemo-conformer-aed", CA, quantization="int8"),
                             [CA / "encoder-model.int8.onnx", CA / "decoder-model.int8.onnx",
                              CA / "vocab.txt"], {"language": "en"}),
    # same models, but fed the live app's VAD segments rather than whole blocks
    "ua-ctc-fp32-vad": lambda: (onnx_asr.load_model("nemo-conformer-ctc", UA),
                                [UA / "model.onnx", UA / "vocab.txt"], {"vad": True}),
    "canary-uk-vad": lambda: (onnx_asr.load_model("nemo-conformer-aed", CA, quantization="int8"),
                              [CA / "encoder-model.int8.onnx", CA / "decoder-model.int8.onnx",
                               CA / "vocab.txt"], {"language": "uk", "vad": True}),
}

if __name__ == "__main__":
    for name in (sys.argv[1:] or list(BUILDERS)):
        try:
            model, paths, kw = BUILDERS[name]()
            run(name, model, paths, **kw)
        except Exception as e:
            print(f"\n=== {name}: FAILED -- {type(e).__name__}: {str(e)[:400]}")
            record(name, failed=f"{type(e).__name__}: {str(e)[:400]}")
