"""Offline replay of recorded runs.

Three operations:

* ``replay``  - run one method over the raw column of a recorded CSV, exactly
  as a live run would process it (cold start, calibration window first).
* ``compare`` - run every method over the *same* recordings, so that method
  differences are not confounded with recording differences.
* ``verify``  - check that this package reproduces what the original scripts
  wrote: each recorded file is re-processed by the method that produced it,
  with the filter state seeded from the file's first row, and the output is
  compared sample-by-sample with the recorded columns.
"""

from __future__ import annotations

import glob
import json
import math
import os
from typing import Dict, Iterable, List, Optional, Sequence

from . import metrics as M
from .detector import DirectionDetector
from .io import Recording, estimate_fs, read_recording
from .methods import COMPARISON_ORDER, METHODS, MethodSpec, method_for_recorded_file
from .pipeline import AXIS_OF_CHANNEL, ChannelPipeline
from .units import AdcConfig


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_run_csvs(paths: Iterable[str]) -> List[str]:
    """Expand files / directories into a sorted list of ``test_*.csv`` paths."""
    found: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            found.extend(glob.glob(os.path.join(p, "**", "test_*.csv"), recursive=True))
        else:
            found.extend(glob.glob(p) or [p])
    return sorted(set(found))


def session_name(path: str) -> str:
    return os.path.basename(os.path.dirname(os.path.abspath(path)))


# ---------------------------------------------------------------------------
# Cold-start replay
# ---------------------------------------------------------------------------

def sample_times(rec: Recording, clock: str, fs: float) -> List[float]:
    """Timestamps handed to the filter.

    ``recorded``: the file's ``Time_ms`` (host arrival time for the original
    recordings, firmware time for v3 runs).
    ``samples``: ``t0 + n / fs`` - a jitter-free sample-count clock.
    """
    if clock == "recorded":
        return list(rec.t_s)
    if clock == "samples":
        t0 = rec.t_s[0] if rec.t_s else 0.0
        return [t0 + i / fs for i in range(len(rec))]
    raise ValueError("clock must be 'recorded' or 'samples'")


def replay(rec: Recording, spec: MethodSpec, channel: Optional[str] = None, *,
           fs: Optional[float] = None, clock: str = "recorded", rate_compensate: bool = True,
           calibration_s: float = 3.0, invert: bool = True, on_row=None) -> ChannelPipeline:
    """Run ``spec`` over one channel of ``rec`` from a cold start.

    The first ``calibration_s`` seconds of the file are consumed by calibration
    (the samples the original run used for calibration were never written to
    disk), so the replay output is that much shorter than the input.
    """
    if channel is None:
        channel = sorted(rec.channels)[0]
    if channel not in rec.channels:
        raise ValueError(f"{rec.path} has no Raw_{channel} column")
    if fs is None:
        fs = estimate_fs(rec.t_s)
    pipe = ChannelPipeline(spec, channel, fs, rate_compensate=rate_compensate, invert=invert,
                           calibration_s=calibration_s)
    for raw, t in zip(rec.channels[channel], sample_times(rec, clock, fs)):
        row = pipe.step(raw, t)
        if row is not None and on_row is not None:
            on_row(row)
    return pipe


def compare(recordings: Sequence[Recording], specs: Sequence[MethodSpec], *,
            clock: str = "recorded", fs: Optional[float] = None, rate_compensate: bool = True,
            adc: AdcConfig = AdcConfig()) -> List[dict]:
    """Run every spec on every channel of every recording.  One flat dict per
    (recording, channel, method)."""
    rows: List[dict] = []
    for rec in recordings:
        for channel in sorted(rec.channels):
            for spec in specs:
                pipe = replay(rec, spec, channel, fs=fs, clock=clock, rate_compensate=rate_compensate)
                s = pipe.summary(adc)
                rows.append({
                    "session": session_name(rec.path),
                    "source_file": os.path.basename(rec.path),
                    "channel": channel,
                    "method": spec.key,
                    "variant": variant_label(spec),
                    "test_num": spec.test_num,
                    "samples": s["samples"],
                    "duration_s": s["duration_s"],
                    "centre_pct": s["direction"]["centre_pct"],
                    "negative_pct": s["direction"]["negative_pct"],
                    "positive_pct": s["direction"]["positive_pct"],
                    "direction_changes": s["direction"]["changes"],
                    "residual_drift_counts_per_min": s["detector_input"]["residual_drift_counts_per_min"],
                    "detector_input_std_counts": s["detector_input"]["std_counts"],
                    "noise_sigma_counts": s["detector_input"]["noise_sigma_counts"],
                    "peak_to_noise_db": s["detector_input"]["peak_to_noise_db"],
                    "legacy_baseline_excursion_counts_per_min": s["legacy"]["baseline_excursion_counts_per_min"],
                    "legacy_removed_component_ratio_db": s["legacy"]["removed_component_ratio_db"],
                    "saccade_sample_fraction": s["counters"].get("saccade_sample_fraction"),
                    "blink_count": s["counters"].get("blink_count"),
                })
    return rows


def variant_label(spec: MethodSpec) -> str:
    p = spec.params
    if getattr(p, "corrected_smoothing", True) is False:
        return "original_smoothing"
    if getattr(p, "response", "rc") == "butterworth2":
        return "butterworth2"
    return "default"


def aggregate(rows: Sequence[dict], keys: Sequence[str], fields: Sequence[str]) -> List[dict]:
    """Mean and sample standard deviation of ``fields`` grouped by ``keys``."""
    groups: Dict[tuple, List[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
    out = []
    for key, members in groups.items():
        agg = dict(zip(keys, key))
        agg["n"] = len(members)
        for f in fields:
            values = [m[f] for m in members if m.get(f) is not None]
            agg[f + "_mean"] = M.mean(values) if values else None
            agg[f + "_sd"] = M.stdev(values) if values else None
        out.append(agg)
    return out


# ---------------------------------------------------------------------------
# Verification against the recorded outputs
# ---------------------------------------------------------------------------

def _floats(column: Sequence[str]) -> List[float]:
    return [float(v) for v in column]


def _fit_initial_hp_states(spec: MethodSpec, raw: List[float], t: List[float],
                           recorded: List[float]) -> List[float]:
    """Least-squares estimate of the two high-pass section outputs at row 0.

    The Test 03 file only stores the final output, not the internal section
    states.  The filter is linear, so its output is (response with zero
    initial states) + s1 * (response to state 1) + s2 * (response to state 2);
    s1 and s2 follow from a 2x2 least-squares fit to the recorded output.
    """
    def run(inputs, first_input, states, lpf0):
        f = spec.build_filter(50.0)
        f._started = True
        f.hpf_last_input[0] = first_input
        f.hpf_last_output[0] = states[0]
        f.hpf_last_input[1] = states[0]
        f.hpf_last_output[1] = states[1]
        f.lpf_last_output = lpf0
        return [f.process(x, ti) for x, ti in zip(inputs[1:], t[1:])]

    zeros = [0.0] * len(raw)
    base = run(raw, raw[0], (0.0, 0.0), recorded[0])
    g1 = run(zeros, 0.0, (1.0, 0.0), 0.0)
    g2 = run(zeros, 0.0, (0.0, 1.0), 0.0)
    target = [r - b for r, b in zip(recorded[1:], base)]
    a11 = sum(x * x for x in g1)
    a12 = sum(x * y for x, y in zip(g1, g2))
    a22 = sum(y * y for y in g2)
    b1 = sum(x * y for x, y in zip(g1, target))
    b2 = sum(x * y for x, y in zip(g2, target))
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-12:
        return [0.0, 0.0]
    return [(b1 * a22 - b2 * a12) / det, (a11 * b2 - a12 * b1) / det]


def _seed_filter(spec: MethodSpec, filt, cols: Dict[str, List[str]], raw: List[float],
                 t: List[float], recorded_filtered: List[float]) -> str:
    """Put ``filt`` into the state it had after processing row 0 of the
    recording.  Returns a note describing what could not be recovered."""
    key = spec.key
    if key == "unfiltered":
        return ""
    if key == "rc_bandpass":
        filt._started = True
        filt.hpf_last_input[0] = raw[0]
        filt.hpf_last_output[0] = float(cols["HPF_out"][0])
        filt.lpf_last_output = float(cols["LPF_out"][0])
        return "state from HPF_out/LPF_out (2 d.p.)"
    if key == "soltani":
        s1, s2 = _fit_initial_hp_states(spec, raw, t, recorded_filtered)
        filt._started = True
        filt.hpf_last_input[0] = raw[0]
        filt.hpf_last_output[0] = s1
        filt.hpf_last_input[1] = s1
        filt.hpf_last_output[1] = s2
        filt.lpf_last_output = recorded_filtered[0]
        return "section states not recorded; fitted by least squares"
    if key == "adaptive":
        filt.baseline = float(cols["Adaptive_Baseline"][0])
        filt.lpf_output = recorded_filtered[0]
        filt.adaptation_events = int(cols["Adapt_Events"][0])
        filt._velocity.update(raw[0], t[0])
        return "velocity window warms up over the first 4 rows"
    if key == "blink_reset":
        filt.baseline = float(cols["Baseline"][0])
        filt.lpf_output = recorded_filtered[0]
        filt.blinks.count = int(cols["Blink_Count"][0])
        filt.baseline_resets = int(cols["Resets"][0])
        return "blink-detector state at row 0 not recorded (assumed idle)"
    if key == "velocity":
        filt.baseline = float(cols["Baseline"][0])
        filt.filtered_position = recorded_filtered[0]
        filt._velocity.update(raw[0], t[0])
        return "velocity window warms up over the first 4 rows"
    if key in ("combined", "full_system_april"):
        filt.baseline = float(cols["Baseline"][0])
        filt.lpf_output = recorded_filtered[0]
        filt.filtered_position = recorded_filtered[0]
        filt._velocity.update(raw[0], t[0])
        if filt.blinks is not None and "Blinks" in cols:
            filt.blinks.count = int(cols["Blinks"][0])
            filt.baseline_resets = int(cols["Resets"][0])
        return "intermediate smoother and blink-detector state at row 0 not recorded"
    raise KeyError(key)


def verify_recording(rec: Recording) -> dict:
    """Re-run the producing method over a recorded file and compare outputs."""
    info = rec.info or {}
    # The recordings were made with the original dual-mode coefficient
    # assignment, so verification always uses it, whatever the current default.
    spec = method_for_recorded_file(info["test_num"], info["tag"], as_recorded=True)
    channel = sorted(rec.channels)[0]
    cols = rec.columns
    raw = rec.channels[channel]
    t = rec.t_s
    filt_col = f"Filtered_{channel}"
    recorded_filtered = _floats(cols[filt_col]) if filt_col in cols else list(raw)
    recorded_dirs = cols["Direction"]
    base_col = "Adaptive_Baseline" if "Adaptive_Baseline" in cols else "Baseline"
    recorded_baseline = _floats(cols[base_col])

    filt = spec.build_filter(50.0)
    note = _seed_filter(spec, filt, cols, raw, t, recorded_filtered)
    detector = DirectionDetector(spec.dead_zone, spec.hold_frames, True, AXIS_OF_CHANNEL[channel])
    calibrated = spec.baseline_mode == "calibrated"

    def detect(value: float, i: int) -> str:
        return detector.update(value, recorded_baseline[i] if calibrated else 0.0)

    # Row 0: the original detector saw its first sample here.
    detect(recorded_filtered[0], 0)

    agree = 0
    sq_err = 0.0
    max_err = 0.0
    sq_err_late = 0.0
    n_late = 0
    base_max_err = 0.0
    n = len(raw)
    t_late = t[0] + 5.0
    for i in range(1, n):
        y = filt.process(raw[i], t[i])
        d = detect(y, i)
        agree += d == recorded_dirs[i]
        err = y - recorded_filtered[i]
        sq_err += err * err
        max_err = max(max_err, abs(err))
        if t[i] >= t_late:
            sq_err_late += err * err
            n_late += 1
        if not calibrated and filt.baseline is not None:
            base_max_err = max(base_max_err, abs(filt.baseline - recorded_baseline[i]))

    signal_sd = M.stdev(recorded_filtered)
    rmse = math.sqrt(sq_err / max(1, n - 1))
    result = {
        "session": session_name(rec.path),
        "file": os.path.basename(rec.path),
        "test_num": spec.test_num,
        "method": spec.key,
        "channel": channel,
        "rows": n,
        "direction_agreement_pct": 100.0 * agree / max(1, n - 1),
        "filtered_rmse_counts": rmse,
        "filtered_rmse_pct_of_sd": 100.0 * rmse / signal_sd if signal_sd else 0.0,
        "filtered_rmse_after_5s_counts": math.sqrt(sq_err_late / n_late) if n_late else 0.0,
        "filtered_max_abs_err_counts": max_err,
        "baseline_max_abs_err_counts": base_max_err,
        "seed_note": note,
    }
    counters = filt.counters()
    if "Blink_Count" in cols or "Blinks" in cols:
        result["blinks_recorded"] = int(cols["Blink_Count" if "Blink_Count" in cols else "Blinks"][-1])
        result["blinks_replayed"] = counters.get("blink_count")
    if "Adapt_Events" in cols:
        result["adapt_recorded"] = int(cols["Adapt_Events"][-1])
        result["adapt_replayed"] = counters.get("adaptation_samples")
    return result


def verify_summary_json(json_path: str, rec: Recording) -> List[dict]:
    """Recompute the fields of a recorded JSON summary from its CSV using
    ``eog.metrics`` and report (field, recorded, recomputed)."""
    with open(json_path, "r", encoding="utf-8") as f:
        js = json.load(f)
    channel = sorted(rec.channels)[0]
    raw = rec.channels[channel]
    dirs = rec.columns["Direction"]
    checks: List[dict] = []

    def add(name, recorded, recomputed):
        checks.append({"json": os.path.basename(json_path), "field": name,
                       "recorded": recorded, "recomputed": recomputed,
                       "abs_diff": abs(recorded - recomputed)})

    if "channels" in js:                                    # Test 01 summary
        r = js["channels"][channel]
        add("total_samples", r["total_samples"], len(raw))
        add("raw_mean", r["raw_mean"], M.mean(raw))
        add("raw_std", r["raw_std"], M.stdev(raw))
        add("raw_range", r["raw_range"], max(raw) - min(raw))
        add("direction_changes", r["direction_changes"], M.direction_changes(dirs))
        add("direction_stability_pct", r["direction_stability_pct"],
            M.non_transition_pct_last_n(dirs, 100))
    else:                                                   # Test 07 summary
        filtered = _floats(rec.columns[f"Filtered_{channel}"])
        baseline = _floats(rec.columns["Baseline"])
        add("total_samples", js["total_samples"], len(raw))
        add("signal.raw_std", js["signal"]["raw_std"], M.stdev(raw))
        add("signal.filt_std", js["signal"]["filt_std"], M.stdev(filtered))
        add("signal.bl_range", js["signal"]["bl_range"], M.baseline_excursion(baseline))
        add("signal.drift_uv_min", js["signal"]["drift_uv_min"],
            M.baseline_excursion_per_min(baseline, js["duration_sec"]))
        add("signal.snr_db", js["signal"]["snr_db"], M.removed_component_ratio_db(raw, filtered))
        add("performance.stab_pct", js["performance"]["stab_pct"],
            M.direction_fractions(dirs)["centre_pct"])
        add("performance.changes", js["performance"]["changes"], M.direction_changes(dirs))
    return checks


def matching_summary_json(csv_path: str) -> Optional[str]:
    """The recorded JSON summary that belongs to a Test 01 / Test 07 CSV."""
    folder = os.path.dirname(csv_path)
    info = read_info(csv_path)
    if info is None:
        return None
    if info["test_num"] == 7:
        cand = os.path.join(folder, f"test_07_summary_{info['channel']}_{info['timestamp']}.json")
        return cand if os.path.exists(cand) else None
    if info["test_num"] == 1:
        cands = sorted(glob.glob(os.path.join(folder, "test_01_summary_*.json")))
        return cands[0] if len(cands) == 1 else None
    return None


def read_info(path: str) -> Optional[dict]:
    from .io import identify_run_file
    return identify_run_file(path)


def default_specs(include_april: bool = False) -> List[MethodSpec]:
    keys = list(COMPARISON_ORDER) + (["full_system_april"] if include_april else [])
    return [METHODS[k] for k in keys]
