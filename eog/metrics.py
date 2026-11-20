"""Performance metrics (pure functions over lists of samples).

Two groups:

1. **Legacy metrics** - the quantities the original scripts computed, with
   exactly the same arithmetic but honest names.  Each docstring gives the
   original name so recorded JSON/CSV values can be traced.
2. **Added metrics** - better-defined replacements that are comparable across
   methods.  Values computed with these are always labelled "recomputed".

Everything is in raw ADC counts unless a function name says otherwise; see
``eog.units`` for conversion.  ``docs/methods-review.md`` explains why each
legacy metric was renamed.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence

from .detector import CENTER, NEGATIVE_LABELS, POSITIVE_LABELS


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def stdev(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in [0, 100]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def effective_sample_rate_hz(t_s: Sequence[float]) -> float:
    """(n - 1) / (t_last - t_first).

    The original summaries divided the number of *recorded* samples by a
    duration that also included the 3 s calibration period, which is why the
    recorded JSON files report ~45.4 Hz for a 50 Hz stream.
    """
    if len(t_s) < 2 or t_s[-1] <= t_s[0]:
        return 0.0
    return (len(t_s) - 1) / (t_s[-1] - t_s[0])


def interval_jitter_ms(t_s: Sequence[float]) -> Dict[str, float]:
    """Spread of the inter-sample interval in milliseconds."""
    if len(t_s) < 3:
        return {"median_ms": 0.0, "std_ms": 0.0, "max_ms": 0.0}
    dt = [(b - a) * 1000.0 for a, b in zip(t_s, t_s[1:])]
    return {"median_ms": statistics.median(dt), "std_ms": stdev(dt), "max_ms": max(dt)}


# ---------------------------------------------------------------------------
# Direction-label statistics
# ---------------------------------------------------------------------------

def direction_fractions(directions: Sequence[str]) -> Dict[str, float]:
    """Percentage of samples in each detector state.

    ``centre_pct`` is what the original Test 07 summary and ``eog_analysis.py``
    called "stability %".  It measures time spent at centre, not stability: a
    detector that never leaves CENTER scores 100.
    """
    n = len(directions)
    if n == 0:
        return {"centre_pct": 0.0, "negative_pct": 0.0, "positive_pct": 0.0}
    centre = sum(1 for d in directions if d == CENTER)
    neg = sum(1 for d in directions if d in NEGATIVE_LABELS)
    pos = sum(1 for d in directions if d in POSITIVE_LABELS)
    return {
        "centre_pct": 100.0 * centre / n,
        "negative_pct": 100.0 * neg / n,
        "positive_pct": 100.0 * pos / n,
    }


def direction_changes(directions: Sequence[str]) -> int:
    """Number of transitions *into* a non-centre state (original definition:
    starts from CENTER, returns to centre are not counted)."""
    changes = 0
    last = CENTER
    for d in directions:
        if d != last and d != CENTER:
            changes += 1
        last = d
    return changes


def non_transition_pct_last_n(directions: Sequence[str], n: int = 100) -> float:
    """Original Test 01 "direction_stability_pct": over the last ``n`` samples,
    the percentage on which the label was CENTER or unchanged from the previous
    sample.  (A different quantity from ``centre_pct`` despite sharing the
    name "stability" in the original outputs.)"""
    flags: List[int] = []
    last = CENTER
    for d in directions:
        flags.append(1 if (d == last or d == CENTER) else 0)
        last = d
    tail = flags[-n:]
    return 100.0 * sum(tail) / len(tail) if tail else 0.0


def dwell_times_ms(directions: Sequence[str], t_s: Sequence[float]) -> Dict[str, float]:
    """Mean duration (ms) of completed negative / positive detections."""
    durations = {"negative": [], "positive": []}
    last = CENTER
    start: Optional[float] = None
    for d, t in zip(directions, t_s):
        if d != last:
            if last != CENTER and start is not None:
                key = "negative" if last in NEGATIVE_LABELS else "positive"
                durations[key].append((t - start) * 1000.0)
            start = t if d != CENTER else None
            last = d
    return {
        "negative_mean_ms": mean(durations["negative"]),
        "positive_mean_ms": mean(durations["positive"]),
        "negative_events": len(durations["negative"]),
        "positive_events": len(durations["positive"]),
    }


def direction_accuracy(predicted: Sequence[str], truth: Sequence[str]) -> Dict[str, object]:
    """Sample-wise accuracy and confusion matrix against ground-truth labels.

    Needs a labelled (cued) recording.  None of the recordings in
    ``data/recorded`` carry cue labels, so no accuracy figure is reported for
    them; this function exists for future cued recordings and is exercised on
    synthetic signals in the unit tests.
    """
    if len(predicted) != len(truth):
        raise ValueError("predicted and truth must have the same length")
    labels = sorted(set(predicted) | set(truth))
    confusion = {t: {p: 0 for p in labels} for t in labels}
    correct = 0
    for p, t in zip(predicted, truth):
        confusion[t][p] += 1
        correct += p == t
    n = len(truth)
    recall = {}
    for t in labels:
        total = sum(confusion[t].values())
        if total:
            recall[t] = confusion[t][t] / total
    return {
        "n": n,
        "accuracy": correct / n if n else 0.0,
        "balanced_accuracy": mean(list(recall.values())),
        "recall": recall,
        "confusion": confusion,
    }


# ---------------------------------------------------------------------------
# Baseline / drift
# ---------------------------------------------------------------------------

def baseline_excursion(baseline: Sequence[float]) -> float:
    """max - min of the baseline estimate (original ``bl_range`` /
    ``baseline_drift_total``)."""
    return (max(baseline) - min(baseline)) if len(baseline) > 1 else 0.0


def baseline_excursion_per_min(baseline: Sequence[float], duration_s: float) -> float:
    """Baseline excursion divided by duration, per minute, in ADC counts/min.

    This is the arithmetic behind the original "drift uV/min" figures
    (``drift_uv_min``, ``drift_rate_uv_per_min``).  Two caveats:

    * the unit was ADC counts, never microvolts;
    * it measures how far the *baseline estimate* travelled.  For methods with
      a fixed calibration baseline (Tests 01-03) it is identically zero; for
      adaptive methods it grows the more the baseline tracks the signal.  It is
      therefore not a measure of residual drift and is not comparable across
      methods.  Use ``residual_drift_per_min`` for that.
    """
    if duration_s <= 0:
        return 0.0
    return baseline_excursion(baseline) / duration_s * 60.0


def linear_slope(t_s: Sequence[float], values: Sequence[float]) -> float:
    """Least-squares slope of ``values`` against ``t_s`` (units per second)."""
    n = len(values)
    if n < 2 or len(t_s) != n:
        return 0.0
    mt, mv = mean(t_s), mean(values)
    sxx = sum((t - mt) ** 2 for t in t_s)
    if sxx == 0:
        return 0.0
    sxy = sum((t - mt) * (v - mv) for t, v in zip(t_s, values))
    return sxy / sxx


def bin_medians(t_s: Sequence[float], values: Sequence[float], bin_s: float = 1.0):
    """Median time and median value of consecutive ``bin_s``-second bins."""
    if not values:
        return [], []
    t0 = t_s[0]
    bins: Dict[int, List[int]] = {}
    for i, t in enumerate(t_s):
        bins.setdefault(int((t - t0) // bin_s), []).append(i)
    tm, vm = [], []
    for k in sorted(bins):
        idx = bins[k]
        tm.append(statistics.median(t_s[i] for i in idx))
        vm.append(statistics.median(values[i] for i in idx))
    return tm, vm


def residual_drift_per_min(t_s: Sequence[float], detector_input: Sequence[float],
                           bin_s: float = 1.0) -> float:
    """Trend of the resting level of the signal the detector thresholds, in
    ADC counts per minute (unsigned).

    ``detector_input`` is (filtered - baseline): what is left for the dead-zone
    comparison after the method under test has done its drift removal.  The
    signal is reduced to the median of each ``bin_s``-second bin (so that brief
    eye-movement excursions do not dominate) and a least-squares line is fitted
    through those medians.  The same definition applies to every method, so
    values are comparable when the methods are run on the same recording.
    """
    tm, vm = bin_medians(t_s, detector_input, bin_s)
    return abs(linear_slope(tm, vm)) * 60.0


# ---------------------------------------------------------------------------
# Signal / noise
# ---------------------------------------------------------------------------

def removed_component_ratio_db(raw: Sequence[float], filtered: Sequence[float]) -> float:
    """20*log10( std(filtered) / std(raw - filtered) ).

    This is the original "SNR (dB)".  ``raw - filtered`` is whatever the filter
    removed - DC offset, baseline wander and noise together - so the ratio
    describes how much of the raw signal's variation the filter kept versus
    removed.  It is not a signal-to-noise ratio: it does not separate eye
    movement from noise, and a filter that changes nothing scores +infinity.
    """
    if len(raw) != len(filtered) or len(raw) < 2:
        return 0.0
    s = stdev(filtered)
    r = stdev([a - b for a, b in zip(raw, filtered)])
    if s <= 0 or r <= 0:
        return 0.0
    return 20.0 * math.log10(s / r)


def robust_noise_sigma(values: Sequence[float]) -> float:
    """Sample-noise estimate from the median absolute deviation of the first
    difference: ``1.4826 * MAD(diff) / sqrt(2)``.

    Saccades occupy a small fraction of samples, so they are outliers of the
    first difference and barely move the MAD; what remains is the sample-to-
    sample noise of the signal.  Exact for white Gaussian noise.
    """
    if len(values) < 3:
        return 0.0
    diff = [b - a for a, b in zip(values, values[1:])]
    med = statistics.median(diff)
    mad = statistics.median([abs(d - med) for d in diff])
    return 1.4826 * mad / math.sqrt(2.0)


def peak_to_noise_db(values: Sequence[float]) -> float:
    """20*log10( P95(|x - median(x)|) / robust_noise_sigma(x) ).

    A label-free signal-to-noise proxy: the size of the large excursions (eye
    movements) relative to the sample noise *of the same signal*.  Unlike the
    legacy ratio it rewards a filter for lowering noise while keeping movement
    amplitude.  It is still a proxy - a true SNR needs cued recordings.
    """
    if len(values) < 3:
        return 0.0
    med = statistics.median(values)
    peak = percentile([abs(v - med) for v in values], 95.0)
    sigma = robust_noise_sigma(values)
    if peak <= 0 or sigma <= 0:
        return 0.0
    return 20.0 * math.log10(peak / sigma)
