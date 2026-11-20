"""Port-fidelity check against the original student scripts.

Feeds identical samples and timestamps to the original filter classes and to
the ``eog`` package and requires bit-identical outputs.

Tests 06/07 are compared with ``corrected_smoothing=False``: the package
default now swaps the two dual-mode coefficients (see docs/methods-review.md),
and the original assignment is kept as an option so that this check and replay
verification still hold.

The original scripts live in ``_sources/`` (a private snapshot that is not part
of the published repository), so these tests are skipped when it is absent.
"""

import importlib.util
import os
import random
import sys
import unittest

from eog import filters as F
from eog.detector import Calibrator, DirectionDetector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATEST = os.path.join(ROOT, "_sources", "test_suite_latest")
APRIL = os.path.join(ROOT, "_sources", "test_suite_april")
HAVE_SOURCES = os.path.isdir(LATEST) and os.path.isdir(APRIL)

try:
    import serial  # noqa: F401  (the original common.py imports it at module level)
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False


def load_original(folder, filename):
    """Import an original script by path without writing bytecode into _sources."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, folder)
    try:
        sys.modules.pop("common", None)
        name = "orig_" + os.path.basename(folder) + "_" + filename[:-3]
        spec = importlib.util.spec_from_file_location(name, os.path.join(folder, filename))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(folder)
        sys.modules.pop("common", None)


def synthetic_stream(n=4000, seed=7, t0=1_000_000.0):
    """Saccade-like pulses + slow drift + noise, with jittered host timestamps.

    ``t0`` is large because the original blink code relies on ``time.time()``
    being far from zero.
    """
    rng = random.Random(seed)
    xs, ts = [], []
    t = t0
    level = 8200.0
    pulse = 0.0
    for i in range(n):
        t += 0.02 + rng.uniform(-0.0008, 0.0008)
        if rng.random() < 0.01:
            pulse = rng.choice([-1, 1]) * rng.uniform(800, 5000)
        pulse *= 0.9
        level += 0.3
        xs.append(int(level + pulse + rng.gauss(0, 60)))
        ts.append(t)
    return xs, ts


@unittest.skipUnless(HAVE_SOURCES and HAVE_SERIAL, "original scripts (_sources/) or pyserial not available")
class OriginalEquivalence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xs, cls.ts = synthetic_stream()

    def _compare(self, original_step, ported_step):
        for i, (x, t) in enumerate(zip(self.xs, self.ts)):
            self.assertEqual(original_step(x, t), ported_step(x, t), f"sample {i}")

    def test_02_rc_bandpass(self):
        o = load_original(LATEST, "test_02_fixed_butterworth.py").ButterworthFilter()
        p = F.RCBandpass(F.BandpassParams(0.1, 15.0, 1))
        self._compare(lambda x, t: o.process(x), p.process)

    def test_03_soltani(self):
        o = load_original(LATEST, "test_03_low_cutoff.py").SoltaniFilter()
        p = F.RCBandpass(F.BandpassParams(0.05, 20.0, 2))
        self._compare(lambda x, t: o.process(x), p.process)

    def test_04_adaptive(self):
        o = load_original(LATEST, "test_04_adaptive_baseline.py").AdaptiveBaselineFilter()
        p = F.AdaptiveBaseline()
        self._compare(lambda x, t: (o.process(x, t), o.adaptive_baseline, o.velocity, o.adaptation_events),
                      lambda x, t: (p.process(x, t), p.baseline, p.velocity, p.adaptation_events))

    def test_05_blink_reset(self):
        o = load_original(LATEST, "test_05_blink_reset.py").BlinkResetFilter()
        p = F.BlinkReset()

        def orig(x, t):
            y, status = o.process(x, t)
            return y, o.baseline, o.blink_count, o.baseline_resets, status or "-"

        self._compare(orig, lambda x, t: (p.process(x, t), p.baseline, p.blinks.count,
                                          p.baseline_resets, p.blink_status))
        self.assertGreater(p.blinks.count, 0, "stream should contain detected 'blinks'")

    def test_06_velocity(self):
        o = load_original(LATEST, "test_06_velocity_mode.py").VelocityDomainFilter()
        p = F.VelocityDualMode(F.DualModeParams(corrected_smoothing=False))

        def orig(x, t):
            y, v, mode = o.process(x, t)
            return y, v, mode, o.baseline

        self._compare(orig, lambda x, t: (p.process(x, t), p.velocity, p.mode, p.baseline))

    def test_07_combined(self):
        o = load_original(LATEST, "test_07_full_system.py").CombinedFilter()
        p = F.Combined(F.CombinedParams(corrected_smoothing=False))

        def orig(x, t):
            y, v, mode = o.process(x, t)
            return y, v, mode, o.adaptive_baseline, o.adaptation_events

        self._compare(orig, lambda x, t: (p.process(x, t), p.velocity, p.mode, p.baseline,
                                          p.adaptation_events))

    def test_07_april_full_system(self):
        o = load_original(APRIL, "test_07_full_system.py").FullSystemFilter()
        p = F.Combined(F.CombinedParams(blink=F.BlinkParams(), corrected_smoothing=False))

        def orig(x, t):
            y, v, mode = o.process(x, t)
            return y, v, mode, o.adaptive_baseline, o.blink_count, o.baseline_resets

        self._compare(orig, lambda x, t: (p.process(x, t), p.velocity, p.mode, p.baseline,
                                          p.blinks.count, p.baseline_resets))
        self.assertGreater(p.blinks.count, 0)

    def test_direction_detector(self):
        common = load_original(LATEST, "test_02_fixed_butterworth.py")
        o = common.DirectionDetector(dead_zone=300, hold_frames=8, invert=True, axis="vertical")
        p = DirectionDetector(dead_zone=300, hold_frames=8, invert=True, axis="vertical")
        for x in self.xs:
            self.assertEqual(o.update(x, 8200, 0.0), p.update(x, 8200))

    def test_calibrator_median(self):
        values = self.xs[:151]
        ordered = sorted(values)
        cal = Calibrator(duration_s=3.0, min_samples=30)
        for i, v in enumerate(values):
            cal.feed(v, i * 0.02)
        self.assertTrue(cal.done)
        self.assertEqual(cal.baseline, ordered[len(ordered) // 2])


if __name__ == "__main__":
    unittest.main()
