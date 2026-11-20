import math
import random
import unittest

from eog import metrics as M
from eog.units import AdcConfig


class DirectionMetrics(unittest.TestCase):
    DIRS = ["CENTER"] * 4 + ["LEFT"] * 3 + ["CENTER"] * 2 + ["RIGHT"] * 1

    def test_fractions(self):
        f = M.direction_fractions(self.DIRS)
        self.assertEqual((f["centre_pct"], f["negative_pct"], f["positive_pct"]), (60.0, 30.0, 10.0))

    def test_detector_stuck_at_centre_scores_100_percent(self):
        # Why "% CENTER" cannot be called stability.
        self.assertEqual(M.direction_fractions(["CENTER"] * 50)["centre_pct"], 100.0)

    def test_changes_count_entries_into_non_centre_states(self):
        self.assertEqual(M.direction_changes(self.DIRS), 2)
        self.assertEqual(M.direction_changes(["LEFT", "RIGHT", "LEFT"]), 3)
        self.assertEqual(M.direction_changes([]), 0)

    def test_vertical_labels_are_counted(self):
        f = M.direction_fractions(["UP", "DOWN", "CENTER", "CENTER"])
        self.assertEqual((f["negative_pct"], f["positive_pct"]), (25.0, 25.0))

    def test_non_transition_pct(self):
        dirs = ["CENTER"] * 98 + ["LEFT", "LEFT"]
        self.assertEqual(M.non_transition_pct_last_n(dirs, 100), 99.0)

    def test_dwell_times(self):
        dirs = ["CENTER", "LEFT", "LEFT", "CENTER", "RIGHT", "RIGHT", "RIGHT", "CENTER"]
        t = [i * 0.1 for i in range(len(dirs))]
        d = M.dwell_times_ms(dirs, t)
        self.assertAlmostEqual(d["negative_mean_ms"], 200.0)
        self.assertAlmostEqual(d["positive_mean_ms"], 300.0)

    def test_accuracy_and_confusion(self):
        truth = ["LEFT", "LEFT", "CENTER", "CENTER", "RIGHT", "RIGHT"]
        pred = ["LEFT", "CENTER", "CENTER", "CENTER", "RIGHT", "LEFT"]
        a = M.direction_accuracy(pred, truth)
        self.assertAlmostEqual(a["accuracy"], 4 / 6)
        self.assertEqual(a["confusion"]["RIGHT"]["LEFT"], 1)
        self.assertAlmostEqual(a["recall"]["CENTER"], 1.0)
        self.assertAlmostEqual(a["balanced_accuracy"], (0.5 + 1.0 + 0.5) / 3)
        with self.assertRaises(ValueError):
            M.direction_accuracy(pred, truth[:-1])


class DriftMetrics(unittest.TestCase):
    def test_residual_drift_recovers_slope(self):
        t = [i / 50.0 for i in range(1500)]
        x = [5.0 * ti + 100.0 for ti in t]                       # 5 counts/s
        self.assertAlmostEqual(M.residual_drift_per_min(t, x), 300.0, places=6)

    def test_residual_drift_is_unsigned_and_zero_for_flat(self):
        t = [i / 10.0 for i in range(100)]
        self.assertAlmostEqual(M.residual_drift_per_min(t, [-2.0 * ti for ti in t]), 120.0)
        self.assertEqual(M.residual_drift_per_min(t, [3.0] * 100), 0.0)

    def test_residual_drift_is_robust_to_brief_excursions(self):
        t = [i / 50.0 for i in range(1500)]
        x = [2.0 * ti + (4000.0 if (i % 150) < 15 else 0.0) for i, ti in enumerate(t)]
        self.assertAlmostEqual(M.residual_drift_per_min(t, x), 120.0, delta=6.0)

    def test_legacy_excursion_is_zero_for_fixed_baseline(self):
        # Tests 01-03 record a constant calibration baseline, so the original
        # "drift" figure is identically zero for them regardless of real drift.
        self.assertEqual(M.baseline_excursion_per_min([8390.0] * 1500, 30.0), 0.0)

    def test_legacy_excursion_grows_when_baseline_tracks(self):
        baseline = [8000.0 + i for i in range(1500)]
        self.assertAlmostEqual(M.baseline_excursion_per_min(baseline, 30.0), 1499.0 * 2)


class NoiseMetrics(unittest.TestCase):
    def test_removed_component_ratio(self):
        raw = [8000.0 + (100.0 if i % 2 else -100.0) + i for i in range(1000)]
        filtered = [(100.0 if i % 2 else -100.0) for i in range(1000)]
        expected = 20 * math.log10(M.stdev(filtered) / M.stdev([r - f for r, f in zip(raw, filtered)]))
        self.assertAlmostEqual(M.removed_component_ratio_db(raw, filtered), expected)

    def test_removed_component_ratio_handles_degenerate_input(self):
        self.assertEqual(M.removed_component_ratio_db([1.0, 2.0], [1.0, 2.0]), 0.0)
        self.assertEqual(M.removed_component_ratio_db([1.0], [1.0]), 0.0)

    def test_robust_sigma_matches_gaussian_noise_and_ignores_steps(self):
        rng = random.Random(11)
        noise = [rng.gauss(0.0, 50.0) for _ in range(20000)]
        self.assertAlmostEqual(M.robust_noise_sigma(noise), 50.0, delta=2.5)
        stepped = [n + (3000.0 if (i // 500) % 2 else 0.0) for i, n in enumerate(noise)]
        self.assertAlmostEqual(M.robust_noise_sigma(stepped), 50.0, delta=2.5)

    def test_peak_to_noise_improves_when_noise_is_removed(self):
        rng = random.Random(5)
        clean = [(2000.0 if (i // 250) % 2 else 0.0) for i in range(5000)]
        noisy = [c + rng.gauss(0.0, 100.0) for c in clean]
        less_noisy = [c + rng.gauss(0.0, 25.0) for c in clean]
        self.assertGreater(M.peak_to_noise_db(less_noisy), M.peak_to_noise_db(noisy) + 6.0)


class TimingAndUnits(unittest.TestCase):
    def test_effective_sample_rate(self):
        t = [3.0 + i * 0.02 for i in range(1499)]
        self.assertAlmostEqual(M.effective_sample_rate_hz(t), 50.0, places=6)
        self.assertEqual(M.effective_sample_rate_hz([1.0]), 0.0)

    def test_jitter(self):
        j = M.interval_jitter_ms([0.0, 0.02, 0.04, 0.061])
        self.assertAlmostEqual(j["median_ms"], 20.0)
        self.assertAlmostEqual(j["max_ms"], 21.0)

    def test_percentile(self):
        self.assertEqual(M.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(M.percentile([0, 10], 95), 9.5)

    def test_counts_to_volts(self):
        adc = AdcConfig(bits=14, vref=5.0)
        self.assertAlmostEqual(adc.counts_to_mv_at_adc(16384), 5000.0)
        self.assertAlmostEqual(adc.counts_to_mv_at_adc(300), 91.552734375)
        self.assertEqual(adc.full_scale, 16383)

    def test_electrode_microvolts_need_a_gain(self):
        self.assertIsNone(AdcConfig().counts_to_uv_at_electrode(100))
        self.assertAlmostEqual(AdcConfig(gain=1000.0).counts_to_uv_at_electrode(16384), 5000.0)


if __name__ == "__main__":
    unittest.main()
