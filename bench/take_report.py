"""One line per take, built from diagnostics.log — timings and counts only.

The log never holds audio, transcripts or device names, so neither does this
report.  Columns:

  held     press -> release of the push-to-talk key (ms)
  open     press -> microphone open (ms); speech before this is not captured
  speech   microphone open -> first speech the VAD heard (ms); -1 = none
  head     level of the first 100 ms block (dBFS); loud with no speech is the
           microphone's power-up transient
  paste    release -> paste chord sent (ms)
  chars    characters inserted
  result   take outcome, plus paste/segment problems seen in the take

Flags: EARLY = speech began within WARMUP_MS of the microphone opening, where
the power-up transient overlaps the first word; DROP = a segment was thrown
away as too short; NOPASTE = text was decoded but no paste went out.

Run:  .venv/bin/python bench/take_report.py [N newest takes]
"""

import os
import sys
from datetime import datetime
from pathlib import Path

LOG = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) \
    / "parakeet-dictation" / "diagnostics.log"
WARMUP_MS = 800


def parse(line):
    fields = {}
    for tok in line.split():
        key, sep, value = tok.partition("=")
        if sep:
            fields[key] = value
    return fields


def ts(f):
    return datetime.fromisoformat(f["ts"]).timestamp()


def takes(lines):
    take = None
    for line in lines:
        f = parse(line)
        ev = f.get("event")
        if ev == "gesture" and f.get("frm") == "idle" and f.get("to") == "starting":
            take = {"t0": ts(f), "flags": [], "chars": 0, "paste_ok": None}
        if take is None:
            continue
        if ev == "hold_release" and "release" not in take:
            take["release"] = ts(f)
        elif ev == "session_start":
            take["open"] = (ts(f) - take["t0"]) * 1000
        elif ev == "session_stop":
            take["speech"] = int(f["first_speech_ms"]) if "first_speech_ms" in f else None
            take["head"] = f.get("head_db", "?")
        elif ev == "segment_discarded":
            take["flags"].append("DROP")
        elif ev == "take_insert":
            take["chars"] += int(f.get("chars", 0))
        elif ev == "paste":
            take["paste"] = ts(f)
            take["paste_ok"] = f.get("ok") == "1"
            if not take["paste_ok"]:
                take["flags"].append("PASTEFAIL")
        elif ev == "paste_failed":
            take["flags"].append("PASTEFAIL")
        elif ev == "take_end":
            take["outcome"] = f.get("outcome", "?")
            yield take
            take = None


def ms(a, b):
    return f"{(b - a) * 1000:.0f}" if a is not None and b is not None else "-"


def main():
    rows = list(takes(LOG.read_text(errors="replace").splitlines()))
    if len(sys.argv) > 1:
        rows = rows[-int(sys.argv[1]):]
    print(f"{'time':19} {'held':>6} {'open':>5} {'speech':>6} {'head':>6} "
          f"{'paste':>5} {'chars':>5}  result")
    for t in rows:
        flags = list(dict.fromkeys(t["flags"]))
        speech = t.get("speech")
        if speech is not None and 0 <= speech < WARMUP_MS:
            flags.append("EARLY")
        if t["chars"] and t["paste_ok"] is None:
            flags.append("NOPASTE")
        print(f"{datetime.fromtimestamp(t['t0']):%Y-%m-%d %H:%M:%S} "
              f"{ms(t['t0'], t.get('release')):>6} "
              f"{t.get('open', 0):5.0f} "
              f"{'-' if speech is None else speech:>6} "
              f"{t.get('head', '-'):>6} "
              f"{ms(t.get('release'), t.get('paste')):>5} "
              f"{t['chars']:>5}  {t.get('outcome', '?')} {' '.join(flags)}")


if __name__ == "__main__":
    main()
