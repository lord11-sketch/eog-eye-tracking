import math
import random
import statistics
import unittest

from eog import filters as F
from tests._synth import amplitude_after, sine


class RateScaling(unittest.TestCase):
    def test_identity_at_reference_rate(self):
        for alpha in (0.001, 0.01, 0.3, 0.9):
            self.assertEqual(F.scale_alpha(alpha, 50.0), alpha)
        self.assertEqual(F.scale_window(5, 50.0), 5)

    def test_time_constant_preserved(self):
        for alpha in (0.001, 0.01, 0.3, 0.9):
            scaled = F.scale_alpha(alpha, 250.0)
            self.assertAlmostEqual(F.ema_time_constant_s(scaled, 250.0),
                                   F.ema_time_constant_s(alpha, 50.0), places=9)

    def test_window_keeps_time_span(self):
        # 5 samples at 50 Hz span 80 ms -> 21 samples at 250 Hz
        self.assertEqual(F.scale_window(5, 250.0), 21)

    def test_ema_cutoff_of_lpf_alpha(self):
        # The original comment said alpha = 0.3 is "~15 Hz at 50 Hz".
        self.assertAlmostEqual(F.ema_cutoff_hz(0.3, 50.0), 2.87, places=1)

    def test_rate_compensated_step_response_matches_across_rates(self):
        def baseline_after(fs, compensate, seconds=1.0):
            f = F.VelocityDualMode(fs=fs, rate_compensate=compensate)
            f.process(0.0, 0.0)
            for i in range(1, int(seconds * fs) + 1):
                f.process(1000.0, i / fs)
            return f.baseline

        ref = baseline_after(50.0, True)
        self.assertAlmostEqual(baseline_after(250.0, True), ref, delta=0.01 * ref)
        self.assertGreater(baseline_after(250.0, False), 2.0 * ref)   # uncompensated: 5x too fast


class WindowVelocityTest(unittest.TestCase):
    def test_ramp(self):
        v = F.WindowVelocity(5)
        out = [v.update(1000.0 * i / 50.0, i / 50.0) for i in range(20)]
        self.assertEqual(out[0], 0.0)
        self.assertAlmostEqual(out[-1], 1000.0, places=6)

    def test_zero_dt_is_safe(self):
        v = F.WindowVelocity(5)
        v.update(1.0, 1.0)
        self.assertEqual(v.update(5.0, 1.0), 0.0)


class RCBandpassTest(unittest.TestCase):
    def test_first_output_is_zero_and_dc_is_removed(self):
        f = F.RCBandpass(F.BandpassParams(0.1, 15.0, 1), fs=50.0)
        self.assertEqual(f.process(8200.0, 0.0), 0.0)
        for i in range(1, 3000):
            y = f.process(8200.0, i / 50.0)
        self.assertAlmostEqual(y, 0.0, places=6)

    def test_step_decays_towards_zero(self):
        f = F.RCBandpass(F.BandpassParams(0.1, 15.0, 1), fs=50.0)
        f.process(0.0, 0.0)
        ys = [f.process(1000.0, i / 50.0) for i in range(1, 50 * 20)]
        self.assertGreater(max(ys), 900.0)
        self.assertLess(abs(ys[-1]), 5.0)       # 20 s >> 1.6 s time constant

    def _gain_at_cutoff(self, params, fs=1000.0, fc=5.0):
        f = F.RCBandpass(params, fs=fs)
        return 20 * math.log10(amplitude_after(f, sine(fc, fs, 4.0), fs))

    def test_one_section_is_3db_down_at_cutoff(self):
        g = self._gain_at_cutoff(F.BandpassParams(5.0, 400.0, 1))
        self.assertAlmostEqual(g, -3.0, delta=0.3)

    def test_two_cascaded_sections_are_6db_down_not_butterworth(self):
        g = self._gain_at_cutoff(F.BandpassParams(5.0, 400.0, 2))
        self.assertAlmostEqual(g, -6.0, delta=0.4)

    def test_butterworth_option_is_3db_down_at_cutoff(self):
        g = self._gain_at_cutoff(F.BandpassParams(5.0, 400.0, 2, response="butterworth2"))
        self.assertAlmostEqual(g, -3.0, delta=0.3)

    def test_butterworth_coefficients_match_scipy(self):
        try:
            from scipy import signal
        except ImportError:
            self.skipTest("scipy not installed")
        for kind, fc in (("highpass", 0.05), ("lowpass", 15.0)):
            b, a = signal.butter(2, fc, kind, fs=50.0)
            bq = F._Biquad.butterworth(kind, fc, 50.0)
            for got, want in zip((bq.b0, bq.b1, bq.b2, bq.a1, bq.a2), (b[0], b[1], b[2], a[1], a[2])):
                self.assertAlmostEqual(got, want, places=9)

    def test_invalid_response_rejected(self):
        with self.assertRaises(ValueError):
            F.RCBandpass(F.BandpassParams(response="chebyshev"))


class AdaptiveBaselineTest(unittest.TestCase):
    def test_slow_drift_triggers_fast_adaptation(self):
        f = F.AdaptiveBaseline()
        for i in range(50 * 60):                       # 60 s ramp at 20 counts/s (slow)
            f.process(8000.0 + 20.0 * i / 50.0, i / 50.0)
        self.assertGreater(f.adaptation_events, 0)
        self.assertLess(abs((8000.0 + 20.0 * 60) - f.baseline), 250.0)

    def test_velocity_gate_blocks_fast_adaptation_during_a_step(self):
        f = F.AdaptiveBaseline()
        for i in range(100):
            f.process(8000.0, i / 50.0)
        f.process(10000.0, 100 / 50.0)                 # 2000-count step: velocity far above the gate
        self.assertGreater(f.velocity, f.params.velocity_gate)
        self.assertEqual(f.adaptation_events, 0)

    def test_held_gaze_is_eventually_absorbed(self):
        # Limitation documented in docs/methods-review.md: once the step is over
        # velocity is low, so a held deflection is treated as drift.
        f = F.AdaptiveBaseline()
        for i in range(100):
            f.process(8000.0, i / 50.0)
        for i in range(100, 100 + 50 * 10):
            y = f.process(10000.0, i / 50.0)
        self.assertGreater(f.adaptation_events, 0)
        self.assertLess(abs(y), 300.0)                 # back inside the dead zone after 10 s


class BlinkDetectorTest(unittest.TestCase):
    def _pulse(self, width_samples, amplitude=2000.0):
        det = F.BlinkDetector()
        statuses = []
        for i in range(100):
            dev = amplitude if 10 <= i < 10 + width_samples else 0.0
            statuses.append(det.update(dev, i * 0.02))
        return det, [s for s in statuses if s]

    def test_valid_duration(self):
        det, statuses = self._pulse(10)                # 200 ms
        self.assertEqual(statuses, ["START", "VALID"])
        self.assertEqual(det.count, 1)

    def test_too_short_and_too_long_are_invalid(self):
        self.assertEqual(self._pulse(2)[1], ["START", "INVALID"])     # 40 ms
        self.assertEqual(self._pulse(30)[1], ["START", "INVALID"])    # 600 ms

    def test_boundary_duration_is_deterministic_with_sample_clock(self):
        # 5 samples x 20 ms = exactly the 100 ms minimum.
        self.assertEqual(self._pulse(5)[1], ["START", "VALID"])

    def test_no_spurious_settle_window_at_time_zero(self):
        f = F.BlinkReset()
        for i in range(20):
            f.process(8000.0, i * 0.02)                # clock starts at t = 0
        self.assertEqual(f.baseline_resets, 0)
        self.assertEqual(f.blinks.ms_since_blink(0.1), math.inf)

    def test_saccade_transient_is_counted_as_a_blink(self):
        # The negative result: nothing distinguishes a 200 ms saccade transient
        # from a blink, so it is "detected" and triggers a baseline reset.
        f = F.BlinkReset()
        for i in range(400):
            raw = 8000.0 + (3000.0 if 100 <= i < 110 else 0.0)
            f.process(raw, i * 0.02)
        self.assertEqual(f.blinks.count, 1)
        self.assertGreater(f.baseline_resets, 0)


class DualModeTest(unittest.TestCase):
    def test_mode_classification(self):
        f = F.VelocityDualMode()
        for i in range(50):
            f.process(8000.0, i / 50.0)
        self.assertEqual(f.mode, "FIXATION")
        f.process(9000.0, 50 / 50.0)
        self.assertEqual(f.mode, "SACCADE")

    def test_default_smooths_fixation_more_than_saccades(self):
        # Regression: in y = a*x + (1-a)*y a smaller ``a`` is heavier smoothing.
        for f in (F.VelocityDualMode(), F.Combined()):
            self.assertLess(f.fixation_alpha, f.saccade_alpha, type(f).__name__)
            self.assertGreater(F.ema_time_constant_s(f.fixation_alpha, 50.0),
                               F.ema_time_constant_s(f.saccade_alpha, 50.0))
        v = F.VelocityDualMode()
        self.assertEqual((v.fixation_alpha, v.saccade_alpha), (0.1, 0.9))
        c = F.Combined()
        self.assertEqual((c.fixation_alpha, c.saccade_alpha), (0.15, 0.85))

    def test_original_assignment_is_still_available(self):
        original = F.VelocityDualMode(F.DualModeParams(corrected_smoothing=False))
        self.assertEqual((original.fixation_alpha, original.saccade_alpha), (0.9, 0.1))
        combined = F.Combined(F.CombinedParams(corrected_smoothing=False))
        self.assertEqual((combined.fixation_alpha, combined.saccade_alpha), (0.85, 0.15))

    def test_default_reduces_fixation_noise_versus_original(self):
        rng = random.Random(3)
        xs = [8000.0 + rng.gauss(0.0, 20.0) for _ in range(3000)]   # fixation: velocity < threshold

        def output_sd(params):
            f = F.VelocityDualMode(params)
            ys = [f.process(x, i / 50.0) for i, x in enumerate(xs)]
            self.assertEqual(f.saccade_count, 0)
            return statistics.stdev(ys[500:])

        self.assertLess(output_sd(F.DualModeParams()),
                        0.5 * output_sd(F.DualModeParams(corrected_smoothing=False)))


class CombinedTest(unittest.TestCase):
    def test_default_has_no_blink_stage(self):
        f = F.Combined()
        self.assertIsNone(f.blinks)
        self.assertNotIn("blink_count", f.counters())

    def test_april_variant_has_blink_stage(self):
        f = F.Combined(F.CombinedParams(blink=F.BlinkParams()))
        for i in range(400):
            f.process(8000.0 + (1000.0 if 100 <= i < 110 else 0.0), i * 0.02)
        self.assertIsNotNone(f.blinks)
        self.assertEqual(f.counters()["blink_count"], 1)

    def test_first_sample_returns_zero_and_sets_baseline(self):
        f = F.Combined()
        self.assertEqual(f.process(8123.0, 0.0), 0.0)
        self.assertEqual(f.baseline, 8123.0)


if __name__ == "__main__":
    unittest.main()
