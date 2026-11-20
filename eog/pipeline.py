"""Per-channel processing pipeline shared by live acquisition and offline replay.

    raw sample + timestamp -> method filter -> calibration -> direction detector
                           -> one output row -> summary metrics

The pipeline never touches a serial port, a file or the wall clock; the caller
supplies samples and timestamps.  That is what makes a live run and a replay of
its CSV go through exactly the same code.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import metrics as M
from .detector import Calibrator, DirectionDetector, scale_hold_frames
from .filters import REFERENCE_FS
from .methods import MethodSpec
from .units import AdcConfig

AXIS_OF_CHANNEL = {"H": "horizontal", "V": "vertical"}

ROW_FIELDS = ["Time_ms", "Raw", "Filtered", "Baseline", "Detector_Input", "Direction",
              "Velocity", "Mode", "Blink_Status"]


def csv_header(channel: str) -> List[str]:
    """Column names of a run CSV for ``channel`` ('H' or 'V')."""
    return ["Time_ms", f"Raw_{channel}", f"Filtered_{channel}", "Baseline", "Detector_Input",
            "Direction", "Velocity", "Mode", "Blink_Status"]


class ChannelPipeline:
    """Runs one method on one channel."""

    def __init__(self, spec: MethodSpec, channel: str = "H", fs: float = REFERENCE_FS, *,
                 rate_compensate: bool = True, invert: bool = True,
                 calibration_s: float = 3.0, min_calibration_samples: int = 30,
                 axis: Optional[str] = None):
        if channel not in AXIS_OF_CHANNEL:
            raise ValueError("channel must be 'H' or 'V'")
        self.spec = spec
        self.channel = channel
        self.fs = fs
        self.rate_compensate = rate_compensate
        self.axis = axis or AXIS_OF_CHANNEL[channel]
        self.filter = spec.build_filter(fs, rate_compensate)
        hold = scale_hold_frames(spec.hold_frames, fs) if rate_compensate else spec.hold_frames
        self.detector = DirectionDetector(dead_zone=spec.dead_zone, hold_frames=hold,
                                          invert=invert, axis=self.axis)
        self.calibrator = Calibrator(calibration_s, min_calibration_samples)
        self.calibration_baseline: Optional[float] = None
        self.samples_seen = 0

        self.t: List[float] = []
        self.raw: List[float] = []
        self.filtered: List[float] = []
        self.baseline: List[float] = []
        self.detector_input: List[float] = []
        self.directions: List[str] = []

    @property
    def calibrated(self) -> bool:
        return self.calibrator.done

    def step(self, raw: float, t: float) -> Optional[dict]:
        """Process one sample.  Returns an output row, or ``None`` while the
        calibration window is still open (those samples are not recorded,
        matching the original scripts)."""
        self.samples_seen += 1
        filtered = self.filter.process(raw, t)

        if not self.calibrator.done:
            if self.calibrator.feed(filtered, t):
                if self.spec.baseline_mode == "calibrated":
                    self.calibration_baseline = self.calibrator.baseline
            return None

        if self.spec.baseline_mode == "calibrated":
            baseline = self.calibration_baseline
            detector_input = filtered - baseline
            direction = self.detector.update(filtered, baseline)
        else:
            baseline = self.filter.baseline
            detector_input = filtered
            direction = self.detector.update(filtered, 0.0)

        self.t.append(t)
        self.raw.append(raw)
        self.filtered.append(filtered)
        self.baseline.append(baseline)
        self.detector_input.append(detector_input)
        self.directions.append(direction)

        return {
            "Time_ms": int(round(t * 1000.0)),
            "Raw": raw,
            "Filtered": filtered,
            "Baseline": baseline,
            "Detector_Input": detector_input,
            "Direction": direction,
            "Velocity": self.filter.velocity,
            "Mode": self.filter.mode,
            "Blink_Status": self.filter.blink_status,
        }

    @staticmethod
    def format_row(row: dict) -> list:
        """Row -> list of CSV cells (order of ``csv_header``)."""
        raw = row["Raw"]
        return [
            row["Time_ms"],
            int(raw) if float(raw).is_integer() else raw,
            f"{row['Filtered']:.2f}",
            f"{row['Baseline']:.2f}",
            f"{row['Detector_Input']:.2f}",
            row["Direction"],
            f"{row['Velocity']:.0f}",
            row["Mode"],
            row["Blink_Status"],
        ]

    def summary(self, adc: AdcConfig = AdcConfig()) -> dict:
        return summarise(
            t=self.t, raw=self.raw, filtered=self.filtered, baseline=self.baseline,
            detector_input=self.detector_input, directions=self.directions,
            counters=self.filter.counters(), adc=adc,
            header={
                "method": self.spec.key,
                "test_num": self.spec.test_num,
                "title": self.spec.title,
                "channel": self.channel,
                "axis": self.axis,
                "fs_nominal_hz": self.fs,
                "rate_compensated": self.rate_compensate,
                "dead_zone_counts": self.detector.dead_zone,
                "hold_frames": self.detector.hold_frames,
                "calibration_samples": len(self.calibrator.values),
            },
        )


def summarise(*, t: List[float], raw: List[float], filtered: List[float], baseline: List[float],
              detector_input: List[float], directions: List[str], counters: Dict[str, int],
              adc: AdcConfig, header: Optional[dict] = None) -> dict:
    """Summary metrics for one channel of one run.  See ``eog.metrics`` for the
    definition (and original name) of every field."""
    duration = (t[-1] - t[0]) if len(t) > 1 else 0.0
    fractions = M.direction_fractions(directions)
    excursion_rate = M.baseline_excursion_per_min(baseline, duration)
    residual = M.residual_drift_per_min(t, detector_input)

    out = dict(header or {})
    out.update({
        "samples": len(raw),
        "duration_s": duration,
        "sample_rate_hz": M.effective_sample_rate_hz(t),
        "raw": {
            "mean_counts": M.mean(raw),
            "std_counts": M.stdev(raw),
            "range_counts": (max(raw) - min(raw)) if raw else 0.0,
        },
        "detector_input": {
            "std_counts": M.stdev(detector_input),
            "noise_sigma_counts": M.robust_noise_sigma(detector_input),
            "peak_to_noise_db": M.peak_to_noise_db(detector_input),
            "residual_drift_counts_per_min": residual,
            "residual_drift_mv_at_adc_per_min": adc.counts_to_mv_at_adc(residual),
            "residual_drift_uv_at_electrode_per_min": adc.counts_to_uv_at_electrode(residual),
        },
        "direction": {
            "centre_pct": fractions["centre_pct"],
            "negative_pct": fractions["negative_pct"],
            "positive_pct": fractions["positive_pct"],
            "changes": M.direction_changes(directions),
            **{f"dwell_{k}": v for k, v in M.dwell_times_ms(directions, t).items()},
        },
        "legacy": {
            # Same arithmetic as the original scripts, renamed.  See docs/methods-review.md.
            "baseline_excursion_counts": M.baseline_excursion(baseline),
            "baseline_excursion_counts_per_min": excursion_rate,          # was "drift uV/min"
            "removed_component_ratio_db": M.removed_component_ratio_db(raw, filtered),  # was "SNR dB"
            "non_transition_pct_last_100": M.non_transition_pct_last_n(directions, 100),  # Test 01 "stability"
        },
        "counters": dict(counters),
        "units": adc.describe(),
    })
    sacc = counters.get("saccade_samples")
    fix = counters.get("fixation_samples")
    if sacc is not None and fix is not None and (sacc + fix) > 0:
        out["counters"]["saccade_sample_fraction"] = sacc / (sacc + fix)
    return out
