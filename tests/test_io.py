import csv
import json
import os
import tempfile
import unittest

from eog import io as eio
from eog.methods import METHODS
from eog.pipeline import ChannelPipeline, csv_header

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDED = os.path.join(ROOT, "data", "recorded")


class FakePort:
    """Stand-in for serial.Serial: returns scripted lines, then b'' (timeout)."""

    def __init__(self, lines, then=None):
        self.lines = list(lines)
        self.then = then            # exception instance to raise when lines run out
        self.closed = False

    def readline(self):
        if self.lines:
            item = self.lines.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item if isinstance(item, bytes) else item.encode()
        if self.then is not None:
            raise self.then
        return b""

    def close(self):
        self.closed = True


class FakeClock:
    """Advances by ``step`` seconds on every call."""

    def __init__(self, step=0.02):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class ParseLine(unittest.TestCase):
    def test_labelled(self):
        s = eio.parse_line("H:1234,V:5678\r\n")
        self.assertEqual((s.h, s.v, s.t_us, s.fmt), (1234, 5678, None, eio.FORMAT_LABELLED))
        self.assertEqual(eio.parse_line("h:12,v:34").v, 34)
        self.assertEqual(eio.parse_line("H:99").v, None)

    def test_csv_with_and_without_timestamp(self):
        s = eio.parse_line("8454,8449,123456")
        self.assertEqual((s.h, s.v, s.t_us, s.fmt), (8454, 8449, 123456, eio.FORMAT_CSV3))
        s = eio.parse_line("8454,8449")
        self.assertEqual((s.h, s.v, s.t_us, s.fmt), (8454, 8449, None, eio.FORMAT_CSV2))
        self.assertEqual(eio.parse_line("77").h, 77)

    def test_malformed_lines_are_rejected(self):
        for bad in ("", "   ", "H:", "H:12,V:", "H:1x", "12,ab", "1,2,3,4", "12,,34", "34,V:5678",
                    "H:12,V:34,56", "1.5,2", "garbage", "1,2,-5"):
            self.assertIsNone(eio.parse_line(bad), bad)


class Clocks(unittest.TestCase):
    def test_micros_clock_handles_rollover(self):
        c = eio.MicrosClock()
        self.assertEqual(c.update(2**32 - 6000), 0.0)
        self.assertAlmostEqual(c.update(2**32 - 2000), 0.004)
        self.assertAlmostEqual(c.update(2000), 0.008)             # wrapped
        self.assertAlmostEqual(c.update(6000), 0.012)

    def test_sample_count_clock(self):
        c = eio.SampleCountClock(250.0)
        self.assertEqual([c.update() for _ in range(3)], [0.0, 0.004, 0.008])


class LineStreamTest(unittest.TestCase):
    def test_discards_first_line_and_counts_rejects(self):
        port = FakePort(["34,5678", "H:100,V:200", "nonsense", "H:99999,V:1", "100,200", "H:101,V:201"])
        stream = eio.LineStream(port, adc_max=16383, no_data_timeout_s=0.5, host_clock=FakeClock())
        samples = list(stream)
        self.assertEqual([(s.h, s.v) for s in samples], [(100, 200), (101, 201)])
        self.assertEqual(stream.stats.parse_failures, 1)
        self.assertEqual(stream.stats.range_failures, 1)
        self.assertEqual(stream.stats.format_changes, 1)      # "100,200" after a labelled stream
        self.assertIn("no valid data", stream.stats.stop_reason)

    def test_port_error_ends_iteration_cleanly(self):
        port = FakePort(["x", "H:1,V:2"], then=OSError("device unplugged"))
        stream = eio.LineStream(port, host_clock=FakeClock())
        self.assertEqual(len(list(stream)), 1)
        self.assertIn("device unplugged", stream.stats.stop_reason)


class RecordingFiles(unittest.TestCase):
    def test_identify_run_file(self):
        info = eio.identify_run_file("x/test_05_blink_reset_H_20260410_222000.csv")
        self.assertEqual((info["test_num"], info["tag"], info["channel"]), (5, "blink_reset", "H"))
        self.assertEqual(eio.identify_run_file("test_07_combined_BOTH_20260422_164431.csv")["channel"], "BOTH")
        self.assertIsNone(eio.identify_run_file("results_master_table.csv"))

    @unittest.skipUnless(os.path.isdir(RECORDED), "recorded data not present")
    def test_read_recorded_file(self):
        path = os.path.join(RECORDED, "Results_2_Horizontal", "test_01_unfiltered_H_20260410_224000.csv")
        rec = eio.read_recording(path)
        self.assertEqual(len(rec), 1499)
        self.assertEqual(sorted(rec.channels), ["H"])
        self.assertEqual(rec.channels["H"][0], float(rec.columns["Raw_H"][0]))
        self.assertEqual(eio.estimate_fs(rec.t_s), 50.0)
        self.assertEqual(rec.skipped_rows, 0)

    def test_truncated_last_row_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test_01_unfiltered_H_20260101_000000.csv")
            with open(path, "w", newline="") as f:
                f.write("Time_ms,Raw_H,Baseline,Direction\n0,8000,8000,CENTER\n20,8001,8000,CENTER\n40,80")
            rec = eio.read_recording(path)
            self.assertEqual(len(rec), 3)                     # "80" is still a number
            with open(path, "a", newline="") as f:
                f.write("\n60,")
            rec = eio.read_recording(path)
            self.assertEqual((len(rec), rec.skipped_rows), (3, 1))

    def test_missing_columns_raise(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.csv")
            with open(path, "w") as f:
                f.write("a,b\n1,2\n")
            with self.assertRaises(ValueError):
                eio.read_recording(path)

    def test_run_writer_round_trip(self):
        spec = METHODS["combined"]
        pipe = ChannelPipeline(spec, "H", 50.0, calibration_s=0.1, min_calibration_samples=2)
        with tempfile.TemporaryDirectory() as d:
            w = eio.RunWriter(d, spec.test_num, spec.file_tag, "H", "20260101_000000")
            for i in range(100):
                row = pipe.step(8000 + (i % 7), i / 50.0)
                if row:
                    w.write(row)
            w.close(pipe.summary())
            self.assertTrue(os.path.basename(w.csv_path).startswith("test_07_combined_H_"))
            with open(w.csv_path, newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], csv_header("H"))
            self.assertEqual(len(rows) - 1, pipe.summary()["samples"])
            with open(w.json_path) as f:
                self.assertEqual(json.load(f)["method"], "combined")
            # A file written by the package can be replayed by the package.
            rec = eio.read_recording(w.csv_path)
            self.assertEqual(rec.info["test_num"], 7)
            self.assertEqual(len(rec), len(rows) - 1)


if __name__ == "__main__":
    unittest.main()
