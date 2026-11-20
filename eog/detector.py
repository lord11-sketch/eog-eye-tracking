"""Direction detection and start-of-run calibration (pure, no I/O)."""

from __future__ import annotations

from typing import List, Optional

from .filters import REFERENCE_FS

CENTER = "CENTER"
AXIS_LABELS = {
    "horizontal": ("LEFT", "RIGHT"),
    "vertical": ("UP", "DOWN"),
}
NEGATIVE_LABELS = ("LEFT", "UP")
POSITIVE_LABELS = ("RIGHT", "DOWN")


def scale_hold_frames(frames: int, fs: float, reference_fs: float = REFERENCE_FS) -> int:
    """Convert a hold count tuned at ``reference_fs`` to the same hold *time*."""
    if fs == reference_fs:
        return frames
    return max(1, round(frames * fs / reference_fs))


class DirectionDetector:
    """Three-state threshold detector with hold-frame hysteresis.

    ``diff = value - baseline`` (negated when ``invert``) is compared against a
    symmetric dead zone.  A new state must be observed on ``hold_frames``
    consecutive samples before the output switches.  Identical to the detector
    used by every original test script.

    Labels: horizontal -> LEFT / CENTER / RIGHT, vertical -> UP / CENTER / DOWN.
    Which physical direction maps to which label depends on electrode polarity;
    ``invert`` flips it.
    """

    def __init__(self, dead_zone: float = 300.0, hold_frames: int = 8,
                 invert: bool = True, axis: str = "horizontal"):
        if axis not in AXIS_LABELS:
            raise ValueError(f"axis must be one of {sorted(AXIS_LABELS)}")
        self.dead_zone = dead_zone
        self.hold_frames = hold_frames
        self.invert = invert
        self.axis = axis
        self.neg_label, self.pos_label = AXIS_LABELS[axis]
        self.current_direction = CENTER
        self.candidate_direction = CENTER
        self.hold_count = 0

    def classify(self, value: float, baseline: float = 0.0) -> str:
        """Instantaneous (un-debounced) classification of one sample."""
        diff = value - baseline
        if self.invert:
            diff = -diff
        if diff < -self.dead_zone:
            return self.neg_label
        if diff > self.dead_zone:
            return self.pos_label
        return CENTER

    def update(self, value: float, baseline: float = 0.0) -> str:
        instant = self.classify(value, baseline)
        if instant == self.current_direction:
            self.hold_count = 0
            self.candidate_direction = instant
        elif instant == self.candidate_direction:
            self.hold_count += 1
            if self.hold_count >= self.hold_frames:
                self.current_direction = instant
                self.hold_count = 0
        else:
            self.candidate_direction = instant
            self.hold_count = 1
        return self.current_direction


class Calibrator:
    """Collects samples for ``duration_s`` (and at least ``min_samples``) and
    returns their median.

    Time comes from the caller's sample clock, not ``time.time()``, so the same
    code runs live and in offline replay.  The median is ``sorted[n // 2]``
    (the upper-middle element for even ``n``), as in the original.
    """

    def __init__(self, duration_s: float = 3.0, min_samples: int = 30):
        self.duration_s = duration_s
        self.min_samples = min_samples
        self.start_time: Optional[float] = None
        self.values: List[float] = []
        self.done = False
        self.baseline: Optional[float] = None

    def elapsed(self, t: float) -> float:
        return 0.0 if self.start_time is None else t - self.start_time

    def feed(self, value: float, t: float) -> bool:
        """Feed one sample; returns True once calibration is complete."""
        if self.done:
            return True
        if self.start_time is None:
            self.start_time = t
        self.values.append(value)
        if self.elapsed(t) >= self.duration_s and len(self.values) >= self.min_samples:
            ordered = sorted(self.values)
            self.baseline = ordered[len(ordered) // 2]
            self.done = True
        return self.done
