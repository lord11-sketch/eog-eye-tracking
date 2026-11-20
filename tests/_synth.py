"""Synthetic signals for unit tests (not physiological models)."""

import math
import random


def sine(freq_hz, fs, seconds, amplitude=1.0, offset=0.0):
    n = int(seconds * fs)
    return [offset + amplitude * math.sin(2 * math.pi * freq_hz * i / fs) for i in range(n)]


def amplitude_after(filter_obj, signal, fs, settle_fraction=0.5):
    """Peak |output| over the last part of ``signal`` (steady-state gain for a
    unit-amplitude sine)."""
    out = [filter_obj.process(x, i / fs) for i, x in enumerate(signal)]
    tail = out[int(len(out) * settle_fraction):]
    return max(abs(v) for v in tail)


def cued_gaze(fs=50.0, seconds=40.0, hold_s=2.0, amplitude=1500.0, offset=8200.0,
              drift_per_s=0.0, noise_sd=0.0, seed=1):
    """DC-coupled gaze signal following a known cue sequence.

    Returns (samples, times, truth_labels).  The gaze level cycles
    CENTER -> +1 -> CENTER -> -1.  With the detector default ``invert=True``
    a positive deflection is labelled LEFT and a negative one RIGHT.
    """
    rng = random.Random(seed)
    cycle = [(0, "CENTER"), (1, "LEFT"), (0, "CENTER"), (-1, "RIGHT")]
    xs, ts, truth = [], [], []
    for i in range(int(seconds * fs)):
        t = i / fs
        level, label = cycle[int(t // hold_s) % len(cycle)]
        xs.append(offset + level * amplitude + drift_per_s * t + rng.gauss(0.0, noise_sd))
        ts.append(t)
        truth.append(label)
    return xs, ts, truth
