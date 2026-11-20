"""Registry of the methods under comparison.

Each ``MethodSpec`` bundles the filter, its parameters and the detector
settings exactly as configured in the corresponding original test script.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional

from . import filters as F


@dataclass(frozen=True)
class MethodSpec:
    key: str
    test_num: int
    file_tag: str
    """Tag used in output file names: ``test_<NN>_<file_tag>_<CH>_<stamp>.csv``."""
    title: str
    citation: str
    origin: str
    """'literature', 'novel' or 'baseline'."""
    filter_cls: Callable
    params: Optional[object]
    dead_zone: float
    hold_frames: int
    baseline_mode: str
    """'calibrated': the detector compares against the median of the filter
    output during the calibration window (Tests 01-03).
    'internal': the filter removes its own baseline; the detector compares
    against 0 and calibration only acts as a settling delay (Tests 04-07)."""
    note: str = ""

    def build_filter(self, fs: float, rate_compensate: bool = True):
        if self.params is None:
            return self.filter_cls(fs=fs)
        return self.filter_cls(self.params, fs=fs, rate_compensate=rate_compensate)


_SPECS: List[MethodSpec] = [
    MethodSpec(
        key="unfiltered", test_num=1, file_tag="unfiltered",
        title="Unfiltered baseline", citation="-", origin="baseline",
        filter_cls=F.Unfiltered, params=None,
        dead_zone=300, hold_frames=5, baseline_mode="calibrated",
    ),
    MethodSpec(
        key="rc_bandpass", test_num=2, file_tag="butterworth",
        title="Fixed band-pass 0.1-15 Hz (first-order sections)", citation="[3]", origin="literature",
        filter_cls=F.RCBandpass,
        params=F.BandpassParams(hpf_cutoff_hz=0.1, lpf_cutoff_hz=15.0, hpf_sections=1),
        dead_zone=300, hold_frames=8, baseline_mode="calibrated",
        note="Called 'Butterworth' in the original script; implemented as first-order RC sections.",
    ),
    MethodSpec(
        key="soltani", test_num=3, file_tag="low_cutoff",
        title="Low-cutoff high-pass 0.05 Hz (two cascaded first-order sections) + 20 Hz low-pass",
        citation="[15], [9]", origin="literature",
        filter_cls=F.RCBandpass,
        params=F.BandpassParams(hpf_cutoff_hz=0.05, lpf_cutoff_hz=20.0, hpf_sections=2),
        dead_zone=300, hold_frames=8, baseline_mode="calibrated",
        note="Described as a 2nd-order Butterworth; the cascade is critically damped (Q = 0.5).",
    ),
    MethodSpec(
        key="adaptive", test_num=4, file_tag="adaptive",
        title="Adaptive EMA baseline with velocity gate", citation="novel", origin="novel",
        filter_cls=F.AdaptiveBaseline, params=F.AdaptiveParams(),
        dead_zone=300, hold_frames=8, baseline_mode="internal",
    ),
    MethodSpec(
        key="blink_reset", test_num=5, file_tag="blink_reset",
        title="Blink-locked baseline reset", citation="novel (negative result)", origin="novel",
        filter_cls=F.BlinkReset, params=F.BlinkResetParams(),
        dead_zone=300, hold_frames=8, baseline_mode="internal",
        note="Documented failure: the blink detector cannot tell blinks from saccade transients.",
    ),
    MethodSpec(
        key="velocity", test_num=6, file_tag="velocity",
        title="Velocity-domain dual-mode smoothing", citation="novel", origin="novel",
        filter_cls=F.VelocityDualMode, params=F.DualModeParams(),
        dead_zone=300, hold_frames=8, baseline_mode="internal",
    ),
    MethodSpec(
        key="combined", test_num=7, file_tag="combined",
        title="Combined system (Test 04 + Test 06)", citation="novel", origin="novel",
        filter_cls=F.Combined, params=F.CombinedParams(),
        dead_zone=250, hold_frames=6, baseline_mode="internal",
    ),
    MethodSpec(
        key="full_system_april", test_num=7, file_tag="full",
        title="April 'full system' (Test 04 + 05 + 06) - historical", citation="novel (superseded)",
        origin="novel",
        filter_cls=F.Combined,
        params=F.CombinedParams(blink=F.BlinkParams(), corrected_smoothing=False),
        dead_zone=250, hold_frames=6, baseline_mode="internal",
        note="Variant that produced the recorded test_07_full_* files (original smoothing); "
             "superseded by 'combined'.",
    ),
]

METHODS: Dict[str, MethodSpec] = {s.key: s for s in _SPECS}

ALIASES = {
    "01": "unfiltered", "1": "unfiltered", "raw": "unfiltered",
    "02": "rc_bandpass", "2": "rc_bandpass", "butterworth": "rc_bandpass", "hossain": "rc_bandpass",
    "03": "soltani", "3": "soltani", "low_cutoff": "soltani",
    "04": "adaptive", "4": "adaptive", "adaptive_baseline": "adaptive",
    "05": "blink_reset", "5": "blink_reset", "blink": "blink_reset",
    "06": "velocity", "6": "velocity", "velocity_mode": "velocity",
    "07": "combined", "7": "combined", "full_system": "combined",
    "full": "full_system_april",
}

COMPARISON_ORDER = ["unfiltered", "rc_bandpass", "soltani", "adaptive", "blink_reset",
                    "velocity", "combined"]
"""The seven methods of the study (the historical April variant is excluded)."""


def get_method(name: str) -> MethodSpec:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in METHODS:
        raise KeyError(f"unknown method {name!r}; choose from {', '.join(METHODS)}")
    return METHODS[key]


def method_for_recorded_file(test_num: int, file_tag: str, *, as_recorded: bool = False) -> MethodSpec:
    """Spec for a ``test_<NN>_<tag>_...csv`` file.

    With ``as_recorded=True`` the spec is configured the way the original
    scripts ran it (original dual-mode smoothing), which is what replay
    verification of the April recordings needs.
    """
    found = None
    for spec in _SPECS:
        if spec.test_num == test_num and spec.file_tag == file_tag:
            found = spec
            break
    if found is None:
        for spec in _SPECS:
            if spec.test_num == test_num:
                found = spec
                break
    if found is None:
        raise KeyError(f"no method for test {test_num} ({file_tag})")
    return with_options(found, original_smoothing=True) if as_recorded else found


def with_options(spec: MethodSpec, *, original_smoothing: bool = False,
                 butterworth: bool = False) -> MethodSpec:
    """Return ``spec`` with optional (non-default) method variants applied.

    * ``original_smoothing`` - Tests 06/07: use the as-recorded assignment of
      the dual-mode coefficients (saccades smoothed heavily, fixation hardly)
      instead of the corrected default.  Required to reproduce the recordings.
    * ``butterworth`` - Tests 02/03: true 2nd-order Butterworth sections.

    Options that do not apply to a method are ignored.
    """
    params = spec.params
    if (original_smoothing and isinstance(params, (F.DualModeParams, F.CombinedParams))
            and params.corrected_smoothing):
        params = replace(params, corrected_smoothing=False)
    if butterworth and isinstance(params, F.BandpassParams):
        params = replace(params, response="butterworth2")
    if params is spec.params:
        return spec
    return replace(spec, params=params)
