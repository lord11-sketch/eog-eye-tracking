#!/usr/bin/env python3
"""How much does each method's output depend on sub-millisecond timestamp jitter?

The original scripts timestamped each sample with the host wall clock on
arrival.  This script replays recorded runs while adding a uniform 0-1 ms
perturbation to every timestamp (well inside normal USB/OS jitter) and reports
how much the output changes.  Methods whose decisions compare elapsed times
against thresholds that are multiples of the 20 ms sample period (the blink
detector: 100 ms, 160 ms, 200 ms, 400 ms) are expected to be sensitive.

    python analysis/timing_sensitivity.py --data data/recorded --out analysis_output

All values are recomputed by offline replay.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eog import io as eio                     # noqa: E402
from eog import metrics as M                  # noqa: E402
from eog import replay as rp                  # noqa: E402
from eog.methods import COMPARISON_ORDER, METHODS  # noqa: E402
from eog.pipeline import ChannelPipeline      # noqa: E402


def run(rec, spec, times):
    ch = sorted(rec.channels)[0]
    pipe = ChannelPipeline(spec, ch, 50.0)
    for x, t in zip(rec.channels[ch], times):
        pipe.step(x, t)
    return pipe


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data/recorded")
    ap.add_argument("--out", default="analysis_output")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--jitter-ms", type=float, default=1.0)
    args = ap.parse_args(argv)

    recordings = [eio.read_recording(f) for f in rp.find_run_csvs([args.data])]
    recordings = [r for r in recordings if r.info and r.info["test_num"] == 1]
    rows = []
    for key in COMPARISON_ORDER:
        spec = METHODS[key]
        agreements, blink_spreads = [], []
        for rec in recordings:
            reference = run(rec, spec, rec.t_s)
            blinks = []
            for seed in range(args.trials):
                rng = random.Random(seed)
                jittered = [t + rng.uniform(0.0, args.jitter_ms / 1000.0) for t in rec.t_s]
                trial = run(rec, spec, jittered)
                same = sum(a == b for a, b in zip(trial.directions, reference.directions))
                agreements.append(100.0 * same / len(reference.directions))
                blinks.append(trial.filter.counters().get("blink_count"))
            if blinks[0] is not None:
                blink_spreads.append(max(blinks) - min(blinks))
        rows.append({
            "method": key,
            "recordings": len(recordings),
            "trials_per_recording": args.trials,
            "jitter_ms": args.jitter_ms,
            "direction_agreement_mean_pct": M.mean(agreements),
            "direction_agreement_min_pct": min(agreements),
            "blink_count_spread_mean": M.mean(blink_spreads) if blink_spreads else None,
            "blink_count_spread_max": max(blink_spreads) if blink_spreads else None,
        })

    print(f"Direction output agreement with the un-jittered replay "
          f"(0-{args.jitter_ms:g} ms uniform jitter, {args.trials} trials x {len(recordings)} recordings)\n")
    print(f"{'method':14s} {'mean %':>8s} {'min %':>8s}   blink-count spread (mean / max)")
    for r in rows:
        spread = "-" if r["blink_count_spread_mean"] is None else \
            f"{r['blink_count_spread_mean']:.1f} / {r['blink_count_spread_max']}"
        print(f"{r['method']:14s} {r['direction_agreement_mean_pct']:8.2f} "
              f"{r['direction_agreement_min_pct']:8.2f}   {spread}")

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "timing_sensitivity.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in r.items()})
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
