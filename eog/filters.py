"""Drift-removal / smoothing methods compared in the project.

Every class here is a pure, per-sample signal processor:

* no serial I/O, no printing, no wall-clock access;
* ``process(raw, t)`` takes one raw ADC sample and its timestamp in seconds
  (from whichever sample clock the caller chooses) and returns the value the
  direction detector should look at;
* the tunable constants live in frozen ``*Params`` dataclasses whose defaults
  are the values used in the original test scripts.

Sample-rate handling
--------------------
The original scripts hard-coded per-sample constants (EMA alphas, a 5-sample
velocity window) that were tuned with the 50 Hz firmware.  Those constants are
kept as the *reference-rate* definition (``REFERENCE_FS``).  When a filter is
built for a different sample rate and ``rate_compensate`` is true (default),
each constant is converted so that its *time* behaviour is preserved:

* EMA alpha  ->  ``1 - (1 - alpha) ** (REFERENCE_FS / fs)``  (same time constant)
* N-sample window  ->  same span in seconds

At ``fs == REFERENCE_FS`` the conversion is the identity, so the recorded
results are reproduced exactly.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

REFERENCE_FS = 50.0
"""Sample rate (Hz) at which the original per-sample constants were tuned."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def scale_alpha(alpha: float, fs: float, reference_fs: float = REFERENCE_FS) -> float:
    """Convert an EMA coefficient tuned at ``reference_fs`` to sample rate ``fs``.

    Preserves the EMA time constant.  Identity when ``fs == reference_fs``.
    """
    if fs == reference_fs:
        return alpha
    if not 0.0 < alpha < 1.0:
        return alpha
    return 1.0 - (1.0 - alpha) ** (reference_fs / fs)


def scale_window(samples: int, fs: float, reference_fs: float = REFERENCE_FS) -> int:
    """Convert an N-sample window (spanning N-1 intervals) to sample rate ``fs``."""
    if fs == reference_fs:
        return samples
    intervals = max(1, round((samples - 1) * fs / reference_fs))
    return intervals + 1


def ema_cutoff_hz(alpha: float, fs: float) -> float:
    """Exact -3 dB frequency of ``y[n] = alpha*x[n] + (1-alpha)*y[n-1]``.

    Returns ``fs/2`` when the response never drops 3 dB below Nyquist.
    """
    if alpha <= 0.0:
        return 0.0
    if alpha >= 1.0:
        return fs / 2.0
    cos_w = 1.0 - (alpha * alpha) / (2.0 * (1.0 - alpha))
    if cos_w <= -1.0:
        return fs / 2.0
    return math.acos(cos_w) * fs / (2.0 * math.pi)


def ema_time_constant_s(alpha: float, fs: float) -> float:
    """Time constant (s) of an EMA with coefficient ``alpha`` at rate ``fs``."""
    if alpha <= 0.0:
        return math.inf
    if alpha >= 1.0:
        return 0.0
    return -1.0 / (fs * math.log(1.0 - alpha))


class WindowVelocity:
    """|x[n] - x[n-(N-1)]| / (t[n] - t[n-(N-1)]) over a sliding N-sample window.

    This is the velocity estimate used by Tests 04, 06 and 07.  It works on the
    *raw* signal, so its noise floor is set by the raw sample noise (see
    docs/methods-review.md).
    """

    def __init__(self, window: int = 5):
        if window < 2:
            raise ValueError("velocity window must be at least 2 samples")
        self._x: Deque[float] = deque(maxlen=window)
        self._t: Deque[float] = deque(maxlen=window)

    def update(self, raw: float, t: float) -> float:
        self._x.append(raw)
        self._t.append(t)
        if len(self._x) < 2:
            return 0.0
        dt = self._t[-1] - self._t[0]
        if dt > 0:
            return abs((self._x[-1] - self._x[0]) / dt)
        return 0.0


# ---------------------------------------------------------------------------
# Test 01 - unfiltered
# ---------------------------------------------------------------------------

class Unfiltered:
    """Test 01: pass-through.  The detector compares raw against a fixed
    calibration baseline."""

    baseline: Optional[float] = None
    velocity = 0.0
    mode = "-"
    blink_status = "-"

    def __init__(self, fs: float = REFERENCE_FS):
        self.fs = fs

    def process(self, raw: float, t: float) -> float:
        return float(raw)

    def counters(self) -> Dict[str, int]:
        return {}


# ---------------------------------------------------------------------------
# Tests 02 / 03 - fixed band-pass filters from the literature
# ---------------------------------------------------------------------------

class _Biquad:
    """Direct-form-I biquad (used only for the optional true-Butterworth mode)."""

    def __init__(self, b0, b1, b2, a1, a2):
        self.b0, self.b1, self.b2, self.a1, self.a2 = b0, b1, b2, a1, a2
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    @classmethod
    def butterworth(cls, kind: str, cutoff_hz: float, fs: float) -> "_Biquad":
        """2nd-order Butterworth (Q = 1/sqrt(2)), bilinear transform with
        pre-warping.  Matches ``scipy.signal.butter(2, cutoff_hz, kind, fs=fs)``."""
        w0 = 2.0 * math.pi * cutoff_hz / fs
        cos_w, sin_w = math.cos(w0), math.sin(w0)
        alpha = sin_w / math.sqrt(2.0)          # sin(w0) / (2Q), Q = 1/sqrt(2)
        a0 = 1.0 + alpha
        if kind == "highpass":
            b0, b1, b2 = (1.0 + cos_w) / 2.0, -(1.0 + cos_w), (1.0 + cos_w) / 2.0
        elif kind == "lowpass":
            b0, b1, b2 = (1.0 - cos_w) / 2.0, (1.0 - cos_w), (1.0 - cos_w) / 2.0
        else:
            raise ValueError(f"unknown biquad kind {kind!r}")
        return cls(b0 / a0, b1 / a0, b2 / a0, (-2.0 * cos_w) / a0, (1.0 - alpha) / a0)

    def settle(self, x: float, y: float) -> None:
        """Set the state as if the input had been ``x`` (output ``y``) forever."""
        self.x1 = self.x2 = x
        self.y1 = self.y2 = y

    def step(self, x: float) -> float:
        y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        self.x2, self.x1 = self.x1, x
        self.y2, self.y1 = self.y1, y
        return y


@dataclass(frozen=True)
class BandpassParams:
    hpf_cutoff_hz: float = 0.1
    lpf_cutoff_hz: float = 15.0
    hpf_sections: int = 1
    """Number of cascaded first-order high-pass sections (``response='rc'``)."""
    response: str = "rc"
    """``'rc'``: the original first-order sections (default, as recorded).
    ``'butterworth2'``: true 2nd-order Butterworth HP + LP (optional)."""


class RCBandpass:
    """Tests 02 and 03: cascaded first-order high-pass section(s) + first-order
    low-pass, each discretised as in the original scripts::

        HP:  y[n] = a * (y[n-1] + x[n] - x[n-1]),   a = RC / (RC + dt)
        LP:  y[n] = b * x[n] + (1 - b) * y[n-1],     b = dt / (RC + dt)
        RC = 1 / (2*pi*fc),  dt = 1 / fs

    Naming note: the original scripts call these "Butterworth".  One section is
    a first-order response (6 dB/octave); two identical cascaded sections (Test
    03) give a critically-damped second-order response (Q = 0.5), which is 6 dB
    down at ``fc`` - not a Butterworth (Q = 0.707, 3 dB down at ``fc``).  Set
    ``response='butterworth2'`` for a true 2nd-order Butterworth.

    Because alphas are derived from ``fs``, this filter is correct at any
    sample rate *provided the real rate is passed in*; the original scripts
    always passed 50.
    """

    velocity = 0.0
    mode = "-"
    blink_status = "-"
    baseline: Optional[float] = None

    def __init__(self, params: BandpassParams = BandpassParams(), fs: float = REFERENCE_FS,
                 rate_compensate: bool = True):
        # rate_compensate is accepted for a uniform constructor signature; the
        # coefficients are always derived from ``fs``.
        self.params = params
        self.fs = fs
        dt = 1.0 / fs
        rc_hpf = 1.0 / (2.0 * math.pi * params.hpf_cutoff_hz)
        self.hpf_alpha = rc_hpf / (rc_hpf + dt)
        rc_lpf = 1.0 / (2.0 * math.pi * params.lpf_cutoff_hz)
        self.lpf_alpha = dt / (rc_lpf + dt)

        if params.response not in ("rc", "butterworth2"):
            raise ValueError(f"unknown response {params.response!r}")
        if params.hpf_sections < 1:
            raise ValueError("hpf_sections must be >= 1")

        self._started = False
        self.hpf_last_input = [0.0] * params.hpf_sections
        self.hpf_last_output = [0.0] * params.hpf_sections
        self.lpf_last_output = 0.0
        self._bq_hp = self._bq_lp = None
        if params.response == "butterworth2":
            self._bq_hp = _Biquad.butterworth("highpass", params.hpf_cutoff_hz, fs)
            self._bq_lp = _Biquad.butterworth("lowpass", params.lpf_cutoff_hz, fs)

    @property
    def hpf_output(self) -> float:
        """Output of the last high-pass section (the ``HPF_out`` CSV column)."""
        return self.hpf_last_output[-1]

    def process(self, raw: float, t: float) -> float:
        if not self._started:
            self._started = True
            self.hpf_last_input[0] = raw
            if self._bq_hp is not None:
                self._bq_hp.settle(raw, 0.0)
            return 0.0

        if self._bq_hp is not None:
            hp = self._bq_hp.step(raw)
            self.hpf_last_output[-1] = hp
            self.lpf_last_output = self._bq_lp.step(hp)
            return self.lpf_last_output

        x = raw
        for i in range(len(self.hpf_last_output)):
            y = self.hpf_alpha * (self.hpf_last_output[i] + x - self.hpf_last_input[i])
            self.hpf_last_input[i] = x
            self.hpf_last_output[i] = y
            x = y

        self.lpf_last_output = self.lpf_alpha * x + (1.0 - self.lpf_alpha) * self.lpf_last_output
        return self.lpf_last_output

    def counters(self) -> Dict[str, int]:
        return {}


# ---------------------------------------------------------------------------
# Test 04 - adaptive EMA baseline with velocity gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdaptiveParams:
    alpha_slow: float = 0.001
    alpha_fast: float = 0.01
    drift_threshold: float = 100.0     # ADC counts
    velocity_gate: float = 800.0       # ADC counts / s
    velocity_window: int = 5           # samples at REFERENCE_FS
    lpf_alpha: float = 0.3


class AdaptiveBaseline:
    """Test 04 (novel): EMA baseline whose adaptation rate switches to *fast*
    when the deviation from baseline is large **and** the signal velocity is
    low (interpreted as electrode drift rather than a saccade).

    Note: ``lpf_alpha = 0.3`` was commented "~15 Hz at 50 Hz" in the original;
    its actual -3 dB point is ``ema_cutoff_hz(0.3, 50)`` = 2.9 Hz.
    """

    mode = "-"
    blink_status = "-"

    def __init__(self, params: AdaptiveParams = AdaptiveParams(), fs: float = REFERENCE_FS,
                 rate_compensate: bool = True):
        self.params = params
        self.fs = fs
        conv = fs if rate_compensate else REFERENCE_FS
        self.alpha_slow = scale_alpha(params.alpha_slow, conv)
        self.alpha_fast = scale_alpha(params.alpha_fast, conv)
        self.lpf_alpha = scale_alpha(params.lpf_alpha, conv)
        self._velocity = WindowVelocity(scale_window(params.velocity_window, conv))
        self.baseline: Optional[float] = None
        self.lpf_output = 0.0
        self.velocity = 0.0
        self.adaptation_events = 0

    def process(self, raw: float, t: float) -> float:
        if self.baseline is None:
            self.baseline = float(raw)
            self.lpf_output = 0.0
            self._velocity.update(raw, t)
            return 0.0

        self.velocity = self._velocity.update(raw, t)
        drift = abs(raw - self.baseline)
        if drift > self.params.drift_threshold and self.velocity < self.params.velocity_gate:
            alpha = self.alpha_fast
            self.adaptation_events += 1
        else:
            alpha = self.alpha_slow
        self.baseline = alpha * raw + (1.0 - alpha) * self.baseline

        ac_signal = raw - self.baseline
        self.lpf_output = self.lpf_alpha * ac_signal + (1.0 - self.lpf_alpha) * self.lpf_output
        return self.lpf_output

    def counters(self) -> Dict[str, int]:
        return {"adaptation_samples": self.adaptation_events}


# ---------------------------------------------------------------------------
# Test 05 - blink-locked baseline reset (documented negative result)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlinkParams:
    threshold: float = 800.0           # ADC counts from baseline
    min_duration_ms: float = 100.0
    max_duration_ms: float = 400.0
    post_blink_settle_ms: float = 200.0


class BlinkDetector:
    """Amplitude/duration blink detector shared by Test 05 and the April
    "full system".

    A "blink" is any excursion beyond ``threshold`` that falls back below half
    the threshold within ``min_duration_ms..max_duration_ms``.  Nothing in this
    rule is specific to blinks: a saccade transient on an AC-coupled front end
    has the same shape, which is why this method produced the false-positive
    rate documented as a negative result.
    """

    def __init__(self, params: BlinkParams = BlinkParams()):
        self.params = params
        self.in_blink = False
        self.start_time: Optional[float] = None
        self.peak = 0.0
        self.last_blink_time: Optional[float] = None
        self.count = 0
        self.history: Deque[dict] = deque(maxlen=10)

    def update(self, deviation: float, t: float) -> Optional[str]:
        """``deviation`` = |raw - baseline|.  Returns START / VALID / INVALID / None."""
        p = self.params
        if not self.in_blink:
            if deviation > p.threshold:
                self.in_blink = True
                self.start_time = t
                self.peak = deviation
                return "START"
            return None
        if deviation > self.peak:
            self.peak = deviation
        if deviation < p.threshold * 0.5:
            duration_ms = round((t - self.start_time) * 1000.0, 6)
            self.in_blink = False
            if p.min_duration_ms <= duration_ms <= p.max_duration_ms:
                self.count += 1
                self.last_blink_time = t
                self.history.append({"time": t, "duration_ms": duration_ms, "peak": self.peak})
                return "VALID"
            return "INVALID"
        return None

    def ms_since_blink(self, t: float) -> float:
        """Milliseconds since the last VALID blink (``inf`` if none yet).

        The original initialised ``last_blink_time = 0`` and relied on
        ``time.time()`` being huge; with a sample clock starting at 0 that
        would open a spurious settle window, hence the explicit ``None``.

        Times are rounded to 1e-6 ms so that, with a sample-count clock, a
        value such as 5 samples x 20 ms compares as exactly 100 ms instead of
        99.99999999 ms.
        """
        if self.last_blink_time is None:
            return math.inf
        return round((t - self.last_blink_time) * 1000.0, 6)


@dataclass(frozen=True)
class BlinkResetParams:
    blink: BlinkParams = BlinkParams()
    baseline_alpha: float = 0.01
    blink_reset_alpha: float = 0.3
    lpf_alpha: float = 0.3


class BlinkReset:
    """Test 05 (novel, documented failure): after a detected blink, pull the
    baseline quickly towards the current signal on the assumption that the eye
    has returned to centre."""

    velocity = 0.0
    mode = "-"

    def __init__(self, params: BlinkResetParams = BlinkResetParams(), fs: float = REFERENCE_FS,
                 rate_compensate: bool = True):
        self.params = params
        self.fs = fs
        conv = fs if rate_compensate else REFERENCE_FS
        self.baseline_alpha = scale_alpha(params.baseline_alpha, conv)
        self.blink_reset_alpha = scale_alpha(params.blink_reset_alpha, conv)
        self.blend_alpha = scale_alpha(0.3, conv)
        self.lpf_alpha = scale_alpha(params.lpf_alpha, conv)
        self.blinks = BlinkDetector(params.blink)
        self.baseline: Optional[float] = None
        self.lpf_output = 0.0
        self.baseline_resets = 0
        self.blink_status = "INIT"

    def process(self, raw: float, t: float) -> float:
        if self.baseline is None:
            self.baseline = float(raw)
            self.lpf_output = 0.0
            self.blink_status = "INIT"
            return 0.0

        status = self.blinks.update(abs(raw - self.baseline), t)
        self.blink_status = status or "-"
        since = self.blinks.ms_since_blink(t)
        settle = self.params.blink.post_blink_settle_ms

        if 0 < since < settle:
            alpha = self.blink_reset_alpha
            if settle * 0.5 < since < settle * 0.8:
                self.baseline = (1.0 - self.blend_alpha) * self.baseline + self.blend_alpha * raw
                self.baseline_resets += 1
        else:
            alpha = self.baseline_alpha

        self.baseline = alpha * raw + (1.0 - alpha) * self.baseline
        ac_signal = raw - self.baseline
        self.lpf_output = self.lpf_alpha * ac_signal + (1.0 - self.lpf_alpha) * self.lpf_output
        return self.lpf_output

    def counters(self) -> Dict[str, int]:
        return {"blink_count": self.blinks.count, "blink_reset_samples": self.baseline_resets}


# ---------------------------------------------------------------------------
# Test 06 - velocity-domain dual-mode smoothing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DualModeParams:
    saccade_threshold: float = 2000.0   # ADC counts / s
    fixation_alpha: float = 0.9
    saccade_alpha: float = 0.1
    velocity_window: int = 5
    baseline_alpha: float = 0.01
    corrected_smoothing: bool = True
    """The EMA used is ``y = a*x + (1-a)*y``, in which a *larger* ``a`` means
    *less* smoothing.  The original scripts applied ``fixation_alpha`` (0.9)
    to fixation samples and ``saccade_alpha`` (0.1) to saccade samples, which
    smooths saccades heavily and fixation hardly at all - the opposite of the
    stated intent.  ``True`` (default) swaps the assignment so fixation is
    smoothed heavily and saccades lightly, as the method describes.  ``False``
    reproduces the as-recorded behaviour (needed to verify the recordings)."""

    def mode_alphas(self):
        """(alpha used in FIXATION, alpha used in SACCADE)."""
        if self.corrected_smoothing:
            return self.saccade_alpha, self.fixation_alpha
        return self.fixation_alpha, self.saccade_alpha


class VelocityDualMode:
    """Test 06 (novel): classify each sample as SACCADE or FIXATION from the
    windowed velocity and smooth the baseline-removed signal with a different
    EMA coefficient in each mode."""

    blink_status = "-"

    def __init__(self, params: DualModeParams = DualModeParams(), fs: float = REFERENCE_FS,
                 rate_compensate: bool = True):
        self.params = params
        self.fs = fs
        conv = fs if rate_compensate else REFERENCE_FS
        fix, sac = params.mode_alphas()
        self.fixation_alpha = scale_alpha(fix, conv)
        self.saccade_alpha = scale_alpha(sac, conv)
        self.baseline_alpha = scale_alpha(params.baseline_alpha, conv)
        self._velocity = WindowVelocity(scale_window(params.velocity_window, conv))
        self.baseline: Optional[float] = None
        self.filtered_position = 0.0
        self.velocity = 0.0
        self.mode = "INIT"
        self.saccade_count = 0
        self.fixation_count = 0

    def process(self, raw: float, t: float) -> float:
        if self.baseline is None:
            self.baseline = float(raw)
            self.filtered_position = 0.0
            self._velocity.update(raw, t)
            self.mode = "INIT"
            return 0.0

        self.baseline = self.baseline_alpha * raw + (1.0 - self.baseline_alpha) * self.baseline
        self.velocity = self._velocity.update(raw, t)

        if self.velocity > self.params.saccade_threshold:
            self.saccade_count += 1
            self.mode = "SACCADE"
            alpha = self.saccade_alpha
        else:
            self.fixation_count += 1
            self.mode = "FIXATION"
            alpha = self.fixation_alpha

        ac_signal = raw - self.baseline
        self.filtered_position = alpha * ac_signal + (1.0 - alpha) * self.filtered_position
        return self.filtered_position

    def counters(self) -> Dict[str, int]:
        return {"saccade_samples": self.saccade_count, "fixation_samples": self.fixation_count}


# ---------------------------------------------------------------------------
# Test 07 - combined system
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CombinedParams:
    alpha_slow: float = 0.001
    alpha_fast: float = 0.05
    drift_threshold: float = 80.0
    velocity_gate: float = 800.0
    saccade_threshold: float = 1500.0
    fixation_alpha: float = 0.85
    saccade_alpha: float = 0.15
    lpf_alpha: float = 0.2
    velocity_window: int = 5
    corrected_smoothing: bool = True    # see DualModeParams.corrected_smoothing
    blink: Optional[BlinkParams] = None
    """``None`` = current combined system (Test 04 + Test 06).
    ``BlinkParams()`` = the April "full system", which also applied the Test 05
    blink reset.  The recorded Test 07 data in ``data/recorded`` was produced
    by the April variant."""

    def mode_alphas(self):
        if self.corrected_smoothing:
            return self.saccade_alpha, self.fixation_alpha
        return self.fixation_alpha, self.saccade_alpha


class Combined:
    """Test 07: adaptive velocity-gated baseline (Test 04) feeding dual-mode
    smoothing (Test 06) and a final EMA low-pass.

    With ``params.blink`` set this reproduces the earlier "full system" that
    additionally ran the blink-locked reset.
    """

    def __init__(self, params: CombinedParams = CombinedParams(), fs: float = REFERENCE_FS,
                 rate_compensate: bool = True):
        self.params = params
        self.fs = fs
        conv = fs if rate_compensate else REFERENCE_FS
        self.alpha_slow = scale_alpha(params.alpha_slow, conv)
        self.alpha_fast = scale_alpha(params.alpha_fast, conv)
        fix, sac = params.mode_alphas()
        self.fixation_alpha = scale_alpha(fix, conv)
        self.saccade_alpha = scale_alpha(sac, conv)
        self.lpf_alpha = scale_alpha(params.lpf_alpha, conv)
        self.blend_alpha = scale_alpha(0.3, conv)
        self._velocity = WindowVelocity(scale_window(params.velocity_window, conv))
        self.blinks = BlinkDetector(params.blink) if params.blink is not None else None

        self.baseline: Optional[float] = None
        self.filtered_position = 0.0
        self.lpf_output = 0.0
        self.velocity = 0.0
        self.mode = "INIT"
        self.blink_status = "-"
        self.saccade_count = 0
        self.fixation_count = 0
        self.adaptation_events = 0
        self.baseline_resets = 0

    def process(self, raw: float, t: float) -> float:
        if self.baseline is None:
            self.baseline = float(raw)
            self.filtered_position = 0.0
            self.lpf_output = 0.0
            self._velocity.update(raw, t)
            self.mode = "INIT"
            return 0.0

        in_settle = False
        if self.blinks is not None:
            status = self.blinks.update(abs(raw - self.baseline), t)
            self.blink_status = status or "-"
            since = self.blinks.ms_since_blink(t)
            settle = self.params.blink.post_blink_settle_ms
            in_settle = 0 < since < settle

        self.velocity = self._velocity.update(raw, t)
        drift = abs(raw - self.baseline)

        if in_settle:
            alpha = self.alpha_fast
            if settle * 0.5 < since < settle * 0.8:
                self.baseline = (1.0 - self.blend_alpha) * self.baseline + self.blend_alpha * raw
                self.baseline_resets += 1
        elif drift > self.params.drift_threshold and self.velocity < self.params.velocity_gate:
            alpha = self.alpha_fast
            self.adaptation_events += 1
        else:
            alpha = self.alpha_slow
        self.baseline = alpha * raw + (1.0 - alpha) * self.baseline

        if self.velocity > self.params.saccade_threshold:
            self.saccade_count += 1
            self.mode = "SACCADE"
            smooth = self.saccade_alpha
        else:
            self.fixation_count += 1
            self.mode = "FIXATION"
            smooth = self.fixation_alpha

        ac_signal = raw - self.baseline
        self.filtered_position = smooth * ac_signal + (1.0 - smooth) * self.filtered_position
        self.lpf_output = self.lpf_alpha * self.filtered_position + (1.0 - self.lpf_alpha) * self.lpf_output
        return self.lpf_output

    def counters(self) -> Dict[str, int]:
        out = {
            "saccade_samples": self.saccade_count,
            "fixation_samples": self.fixation_count,
            "adaptation_samples": self.adaptation_events,
        }
        if self.blinks is not None:
            out["blink_count"] = self.blinks.count
            out["blink_reset_samples"] = self.baseline_resets
        return out
