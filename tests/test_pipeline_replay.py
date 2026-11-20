import csv
import glob
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from eog import io as eio
from eog import metrics as M
from eog import replay as rp
from eog.cli import main as cli_main
from eog.cli import run_live
from eog.methods import COMPARISON_ORDER, METHODS, get_method, with_options
from eog.pipeline import ChannelPipeline
from eog.units import AdcConfig
from tests._synth import cued_gaze
from tests.test_io import FakeClock, FakePort

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDED = os.path.join(ROOT, "data", "recorded")
HAVE_DATA = os.path.isdir(RECORDED)


def run_pipeline(key, xs, ts, **kw):
    pipe = ChannelPipeline(METHODS[key], "H", 50.0, **kw)
    truth_index = []
    for i, (x, t) in enumerate(zip(xs, ts)):
        if pipe.step(x, t) is not None:
            truth_index.append(i)
    return pipe, truth_index


class MethodRegistry(unittest.TestCase):
    def test_lookup_by_key_number_and_alias(self):
        self.assertEqual(get_method("combined").test_num, 7)
        self.assertEqual(get_method("03").key, "soltani")
        self.assertEqual(get_method("Butterworth").key, "rc_bandpass")
        with self.assertRaises(KeyError):
            get_method("nope")

    def test_original_detector_settings_are_preserved(self):
        expected = {"unfiltered": (300, 5), "rc_bandpass": (300, 8), "soltani": (300, 8),
                    "adaptive": (300, 8), "blink_reset": (300, 8), "velocity": (300, 8),
                    "combined": (250, 6)}
        for key, (dead_zone, hold) in expected.items():
            self.assertEqual((METHODS[key].dead_zone, METHODS[key].hold_frames), (dead_zone, hold), key)

    def test_options_only_touch_applicable_methods(self):
        self.assertIs(with_options(METHODS["adaptive"], original_smoothing=True, butterworth=True),
                      METHODS["adaptive"])
        self.assertTrue(METHODS["combined"].params.corrected_smoothing)
        self.assertTrue(METHODS["velocity"].params.corrected_smoothing)
        self.assertFalse(with_options(METHODS["combined"], original_smoothing=True).params.corrected_smoothing)
        self.assertFalse(METHODS["full_system_april"].params.corrected_smoothing)   # historical, as recorded
        self.assertEqual(with_options(METHODS["soltani"], butterworth=True).params.response, "butterworth2")


class PipelineOnSyntheticCues(unittest.TestCase):
    """Sanity checks on a labelled synthetic signal (not a performance claim)."""

    def test_unfiltered_follows_cues_without_drift(self):
        xs, ts, truth = cued_gaze(noise_sd=30.0)
        pipe, idx = run_pipeline("unfiltered", xs, ts)
        acc = M.direction_accuracy(pipe.directions, [truth[i] for i in idx])
        self.assertGreater(acc["accuracy"], 0.9)

    def test_unfiltered_fails_under_drift(self):
        xs, ts, truth = cued_gaze(noise_sd=30.0, drift_per_s=60.0)
        pipe, idx = run_pipeline("unfiltered", xs, ts)
        acc = M.direction_accuracy(pipe.directions, [truth[i] for i in idx])
        self.assertLess(acc["accuracy"], 0.6)
        self.assertGreater(pipe.summary()["detector_input"]["residual_drift_counts_per_min"], 3000.0)

    def test_calibration_samples_are_not_recorded(self):
        xs, ts, _ = cued_gaze(seconds=10.0)
        pipe, idx = run_pipeline("combined", xs, ts)
        self.assertEqual(idx[0], 151)                       # 3 s at 50 Hz, inclusive
        self.assertEqual(pipe.summary()["samples"], len(xs) - 151)

    def test_calibrated_and_internal_baseline_modes(self):
        xs, ts, _ = cued_gaze(seconds=10.0)
        fixed, _ = run_pipeline("rc_bandpass", xs, ts)
        self.assertEqual(len(set(fixed.baseline)), 1)       # constant calibration baseline
        adaptive, _ = run_pipeline("adaptive", xs, ts)
        self.assertGreater(len(set(adaptive.baseline)), 1)  # tracked baseline, in raw units
        self.assertGreater(min(adaptive.baseline), 5000.0)

    def test_rate_compensation_scales_detector_hold(self):
        pipe = ChannelPipeline(METHODS["combined"], "H", 250.0)
        self.assertEqual(pipe.detector.hold_frames, 30)
        literal = ChannelPipeline(METHODS["combined"], "H", 250.0, rate_compensate=False)
        self.assertEqual(literal.detector.hold_frames, 6)

    def test_summary_units(self):
        xs, ts, _ = cued_gaze(seconds=10.0, drift_per_s=10.0)
        pipe, _ = run_pipeline("unfiltered", xs, ts)
        s = pipe.summary(AdcConfig(gain=500.0))
        d = s["detector_input"]
        self.assertAlmostEqual(d["residual_drift_mv_at_adc_per_min"],
                               d["residual_drift_counts_per_min"] * 5000.0 / 16384.0)
        self.assertIsNotNone(d["residual_drift_uv_at_electrode_per_min"])
        self.assertIsNone(pipe.summary()["detector_input"]["residual_drift_uv_at_electrode_per_min"])


@unittest.skipUnless(HAVE_DATA, "recorded data not present")
class ReplayOnRecordedData(unittest.TestCase):
    def _one(self, pattern):
        return eio.read_recording(sorted(glob.glob(os.path.join(RECORDED, "Results_2_Horizontal", pattern)))[0])

    def test_linear_methods_reproduce_recorded_output_exactly(self):
        for pattern in ("test_01_*.csv", "test_02_*.csv", "test_03_*.csv"):
            r = rp.verify_recording(self._one(pattern))
            self.assertEqual(r["direction_agreement_pct"], 100.0, pattern)
            self.assertLess(r["filtered_rmse_counts"], 0.01, pattern)     # CSV has 2 decimals

    def test_adaptive_and_velocity_reproduce_recorded_output_closely(self):
        for pattern in ("test_04_*.csv", "test_06_*.csv"):
            r = rp.verify_recording(self._one(pattern))
            self.assertGreater(r["direction_agreement_pct"], 98.0, pattern)
            self.assertLess(r["filtered_rmse_pct_of_sd"], 3.0, pattern)

    def test_verification_uses_original_smoothing_automatically(self):
        for tag, num in (("velocity", 6), ("full", 7), ("combined", 7)):
            spec = rp.method_for_recorded_file(num, tag, as_recorded=True)
            self.assertFalse(spec.params.corrected_smoothing, tag)
        self.assertTrue(rp.method_for_recorded_file(6, "velocity").params.corrected_smoothing)

    def test_recorded_test07_is_the_april_blink_variant(self):
        rec = self._one("test_07_*.csv")
        self.assertIn("Blinks", rec.columns)
        self.assertEqual(rp.method_for_recorded_file(7, rec.info["tag"]).key, "full_system_april")

    def test_recorded_json_metrics_recompute_from_csv(self):
        rec = self._one("test_07_*.csv")
        checks = rp.verify_summary_json(rp.matching_summary_json(rec.path), rec)
        self.assertGreaterEqual(len(checks), 8)
        for c in checks:
            self.assertLess(c["abs_diff"], 0.02, c["field"])

    def test_compare_runs_every_method_on_identical_input(self):
        rec = self._one("test_01_*.csv")
        rows = rp.compare([rec], [METHODS[k] for k in COMPARISON_ORDER])
        self.assertEqual([r["method"] for r in rows], COMPARISON_ORDER)
        self.assertEqual(len({r["samples"] for r in rows}), 1)
        self.assertEqual(len({r["source_file"] for r in rows}), 1)

    def test_sample_clock_and_recorded_clock_agree_for_time_free_methods(self):
        rec = self._one("test_01_*.csv")
        a = rp.replay(rec, METHODS["rc_bandpass"], clock="recorded")
        b = rp.replay(rec, METHODS["rc_bandpass"], clock="samples")
        self.assertEqual(a.filtered[:500], b.filtered[:500])

    def test_cli_replay_writes_csv_and_json(self):
        rec_path = self._one("test_01_*.csv").path
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cli_main(["replay", "--input", rec_path, "--method", "combined", "--out-dir", d]), 0)
            csvs = glob.glob(os.path.join(d, "test_07_combined_H_*.csv"))
            self.assertEqual(len(csvs), 1)
            with open(glob.glob(os.path.join(d, "*.summary.json"))[0]) as f:
                summary = json.load(f)
            self.assertEqual(summary["replayed_from"], os.path.basename(rec_path))
            self.assertAlmostEqual(summary["sample_rate_hz"], 50.0, delta=0.1)


def labelled_lines(n, h0=8000, v0=8100):
    return ["H:partial"] + [f"H:{h0 + (i % 5)},V:{v0 + (i % 3)}" for i in range(n)]


class LiveLoopWithFakePort(unittest.TestCase):
    """Exercises the acquisition loop without opening a serial port."""

    def _run(self, port, channels, **kw):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        opts = dict(out_dir=d, duration_s=2.0, fs=None, clock="auto", rate_compensate=True,
                    calibration_s=1.0, adc=AdcConfig(), stamp="20260101_000000", quiet=True,
                    host_clock=FakeClock(0.02))
        opts.update(kw)
        return d, run_live(port, METHODS["combined"], channels, **opts)

    def test_both_channels_are_processed_simultaneously(self):
        d, summaries = self._run(FakePort(labelled_lines(400)), ["H", "V"])
        self.assertEqual(sorted(summaries), ["H", "V"])
        self.assertEqual(summaries["H"]["samples"], summaries["V"]["samples"])
        self.assertEqual(summaries["H"]["fs_nominal_hz"], 50.0)        # labelled stream -> 50 Hz
        self.assertEqual(summaries["H"]["clock"], "samples")
        self.assertAlmostEqual(summaries["H"]["duration_s"], 2.0, delta=0.05)   # --duration honoured
        for ch in ("H", "V"):
            path = os.path.join(d, summaries[ch]["csv_file"])
            with open(path, newline="") as f:
                self.assertEqual(len(list(csv.reader(f))) - 1, summaries[ch]["samples"])
            self.assertTrue(os.path.exists(path.replace(".csv", ".summary.json")))

    def test_firmware_timestamps_drive_the_clock_and_rate(self):
        lines = ["junk"] + [f"{8000 + i % 4},{8100},{4_294_960_000 + i * 4000 & 0xFFFFFFFF}" for i in range(1500)]
        d, summaries = self._run(FakePort(lines), ["H"])
        s = summaries["H"]
        self.assertEqual((s["clock"], s["fs_nominal_hz"]), ("firmware", 250.0))
        self.assertAlmostEqual(s["sample_rate_hz"], 250.0, delta=0.5)   # across a micros() rollover
        self.assertEqual(s["hold_frames"], 30)

    def test_keyboard_interrupt_still_writes_outputs(self):
        port = FakePort(labelled_lines(200), then=KeyboardInterrupt())
        d, summaries = self._run(port, ["H"], duration_s=0)
        self.assertGreater(summaries["H"]["samples"], 0)
        self.assertTrue(os.path.exists(os.path.join(d, summaries["H"]["csv_file"])))
        self.assertTrue(glob.glob(os.path.join(d, "*.summary.json")))

    def test_port_failure_still_writes_outputs_and_reports_reason(self):
        port = FakePort(labelled_lines(200), then=OSError("unplugged"))
        d, summaries = self._run(port, ["H"], duration_s=0)
        self.assertIn("unplugged", summaries["H"]["stream"]["stop_reason"])
        self.assertGreater(summaries["H"]["samples"], 0)

    def test_missing_channel_produces_no_rows_but_does_not_crash(self):
        lines = ["x"] + [f"H:{8000 + i % 3}" for i in range(300)]
        d, summaries = self._run(FakePort(lines), ["H", "V"])
        self.assertGreater(summaries["H"]["samples"], 0)
        self.assertEqual(summaries["V"]["samples"], 0)


class CliArguments(unittest.TestCase):
    def test_live_requires_method_when_not_interactive(self):
        # Returns before any serial port is opened.
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertEqual(cli_main(["live", "--channel", "H"]), 2)
            self.assertEqual(cli_main(["live", "--method", "combined"]), 2)

    def test_unknown_method_is_an_error(self):
        self.assertEqual(cli_main(["replay", "--input", "missing.csv", "--method", "nope"]), 2)

    def test_methods_listing(self):
        self.assertEqual(cli_main(["methods"]), 0)


if __name__ == "__main__":
    unittest.main()
