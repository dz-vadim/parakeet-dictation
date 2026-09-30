# Live-preview pass: CPU cost vs cadence and thread count

Measured 2026-09-30 on the user's laptop (i5-1350P, 12 cores / 16 threads, Parakeet TDT
0.6B v3 int8 via sherpa-onnx). Method: silent 12 s push-to-talk takes driven by SIGUSR1
with a scratch window focused; CPU time of the app process sampled from `/proc/<pid>/stat`
before and after; "last decode" is the flush block decoded after release, i.e. the part
the user waits for. Background load from other work on the machine was 3–7, so absolute
numbers are inflated; rows within a run share that load and are comparable.

| preview cadence | recognizer threads | CPU per 12 s take | passes | last decode |
|---|---|---|---|---|
| 1.0 s | 8 | **+139 s** (~11 cores) | 8 | **1237 ms** |
| 3.0 s | 8 | +56 s | 2 | 168 ms |
| off   | 8 | +23.5 s | 0 | 574 ms* |
| 1.0 s | 4 | +34 s (~3 cores) | 4 | 148 ms |
| 2.0 s | 4 | +39 s | 5 | 159 ms |
| off   | 4 | +8.9 s | 0 | 546 ms* |
| 1.0 s | 2 | +24 s (~2 cores) | 9 | 329 ms |

\* the flush block on a silent take is timing noise between runs; the 1237 ms row is not.

## Reading

Each preview decode itself is 65–230 ms. The cost is ONNX Runtime's intra-op thread pool
spinning between calls: it scales with the thread count, not with the amount of audio
decoded — the idle "off" rows show it too (23.5 s at 8 threads vs 8.9 s at 4 with no
preview at all). At 8 threads a 1 s cadence saturates the CPU and makes the decode the
user waits for seven times slower.

## Decision

`num_threads = 4` with the 1 s cadence: text appears within about a second of speaking,
the take costs ~3 cores while the key is held, and the release-to-text decode is
unaffected (148 ms). Short blocks do not parallelise well, so halving the threads costs
little on the real decodes; long blocks decode while the key is still held. 2 threads is
the battery option (~2 cores, 329 ms). A proper fix would disable ORT spinning
(`session.intra_op.allow_spinning=0`), which sherpa-onnx does not expose.
