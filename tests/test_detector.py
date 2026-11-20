import unittest

from eog.detector import Calibrator, DirectionDetector, scale_hold_frames


class DirectionDetectorTest(unittest.TestCase):
    def test_needs_hold_frames_consecutive_samples(self):
        d = DirectionDetector(dead_zone=300, hold_frames=8, invert=False)
        out = [d.update(1000.0) for _ in range(8)]
        self.assertEqual(out[:7], ["CENTER"] * 7)
        self.assertEqual(out[7], "RIGHT")

    def test_short_glitch_is_ignored(self):
        d = DirectionDetector(dead_zone=300, hold_frames=8, invert=False)
        seq = [0.0] * 5 + [1000.0] * 7 + [0.0] * 5
        self.assertEqual({d.update(v) for v in seq}, {"CENTER"})

    def test_interrupted_candidate_restarts_the_count(self):
        d = DirectionDetector(dead_zone=300, hold_frames=4, invert=False)
        seq = [1000.0] * 3 + [-1000.0] + [1000.0] * 3
        self.assertEqual([d.update(v) for v in seq][-1], "CENTER")
        self.assertEqual(d.update(1000.0), "RIGHT")

    def test_invert_flips_labels(self):
        d = DirectionDetector(dead_zone=300, hold_frames=1, invert=True)
        self.assertEqual(d.classify(1000.0), "LEFT")
        self.assertEqual(d.classify(-1000.0), "RIGHT")

    def test_dead_zone_is_exclusive_and_relative_to_baseline(self):
        d = DirectionDetector(dead_zone=300, hold_frames=1, invert=False)
        self.assertEqual(d.classify(8500.0, 8200.0), "CENTER")    # exactly +300
        self.assertEqual(d.classify(8501.0, 8200.0), "RIGHT")

    def test_vertical_labels(self):
        d = DirectionDetector(dead_zone=300, hold_frames=1, invert=False, axis="vertical")
        self.assertEqual(d.classify(-1000.0), "UP")
        self.assertEqual(d.classify(1000.0), "DOWN")

    def test_bad_axis(self):
        with self.assertRaises(ValueError):
            DirectionDetector(axis="diagonal")

    def test_hold_time_scaling(self):
        self.assertEqual(scale_hold_frames(8, 50.0), 8)
        self.assertEqual(scale_hold_frames(8, 250.0), 40)       # 160 ms either way


class CalibratorTest(unittest.TestCase):
    def test_completes_on_sample_clock_not_wall_clock(self):
        cal = Calibrator(duration_s=3.0, min_samples=30)
        done_at = None
        for i in range(400):
            if cal.feed(float(i), i / 50.0):
                done_at = i
                break
        self.assertEqual(done_at, 150)                           # t = 3.00 s
        self.assertEqual(len(cal.values), 151)

    def test_min_samples_enforced(self):
        cal = Calibrator(duration_s=3.0, min_samples=30)
        for i in range(29):
            self.assertFalse(cal.feed(1.0, i * 1.0))             # 1 Hz: time elapsed, too few samples
        self.assertTrue(cal.feed(1.0, 29.0))

    def test_median_is_upper_middle_for_even_counts(self):
        cal = Calibrator(duration_s=0.0, min_samples=4)
        for i, v in enumerate([10.0, 40.0, 20.0, 30.0]):
            cal.feed(v, float(i))
        self.assertEqual(cal.baseline, 30.0)

    def test_median_ignores_outliers(self):
        cal = Calibrator(duration_s=0.0, min_samples=5)
        for i, v in enumerate([100.0, 101.0, 99.0, 100.0, 9999.0]):
            cal.feed(v, float(i))
        self.assertEqual(cal.baseline, 100.0)


if __name__ == "__main__":
    unittest.main()
