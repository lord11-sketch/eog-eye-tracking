"""Input/output: serial line parsing, sample clocks, recorded-CSV reading and
run-file writing.

``pyserial`` is imported lazily so that offline replay, analysis and the unit
tests work on a machine without it.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional

from .pipeline import ChannelPipeline, csv_header

# ---------------------------------------------------------------------------
# Serial line parsing
# ---------------------------------------------------------------------------

_LABELLED = re.compile(r"^[Hh]:(-?\d+)(?:,[Vv]:(-?\d+))?$")
_INT = re.compile(r"^-?\d+$")

FORMAT_LABELLED = "labelled"      # "H:<h>,V:<v>"          (v1 firmware, 50 Hz)
FORMAT_CSV2 = "csv2"              # "<h>,<v>"              (v2 firmware, 250 Hz)
FORMAT_CSV3 = "csv3"              # "<h>,<v>,<micros>"     (v3 firmware, 250 Hz)
FORMAT_SINGLE = "single"          # "<h>"

NOMINAL_FS = {FORMAT_LABELLED: 50.0, FORMAT_CSV2: 250.0, FORMAT_CSV3: 250.0}
"""Nominal sample rate of the firmware version that emits each wire format."""


@dataclass(frozen=True)
class Sample:
    h: Optional[int]
    v: Optional[int]
    t_us: Optional[int] = None
    fmt: str = FORMAT_SINGLE


def parse_line(line: str) -> Optional[Sample]:
    """Parse one serial line.  Returns ``None`` for anything malformed.

    Accepted formats::

        H:1234,V:5678        labelled (also lower-case, V optional)
        1234,5678,123456     H, V, firmware micros() timestamp
        1234,5678            H, V
        1234                 H only

    Parsing is strict: every field must be a whole integer and the field count
    must match one of the formats, so a line truncated or merged by a serial
    glitch is rejected rather than silently mis-read.
    """
    line = line.strip()
    if not line:
        return None
    m = _LABELLED.match(line)
    if m:
        v = int(m.group(2)) if m.group(2) is not None else None
        return Sample(int(m.group(1)), v, None, FORMAT_LABELLED)
    parts = [p.strip() for p in line.split(",")]
    if not all(_INT.match(p) for p in parts):
        return None
    if len(parts) == 1:
        return Sample(int(parts[0]), None, None, FORMAT_SINGLE)
    if len(parts) == 2:
        return Sample(int(parts[0]), int(parts[1]), None, FORMAT_CSV2)
    if len(parts) == 3:
        t_us = int(parts[2])
        if t_us < 0:
            return None
        return Sample(int(parts[0]), int(parts[1]), t_us, FORMAT_CSV3)
    return None


class MicrosClock:
    """Turns the firmware's 32-bit ``micros()`` counter into seconds since the
    first sample, handling the ~71.6-minute rollover."""

    WRAP = 1 << 32

    def __init__(self):
        self._last: Optional[int] = None
        self._elapsed_us = 0

    def update(self, t_us: int) -> float:
        if self._last is not None:
            self._elapsed_us += (t_us - self._last) % self.WRAP
        self._last = t_us
        return self._elapsed_us / 1e6


class SampleCountClock:
    """t = n / fs.  Jitter-free; assumes the firmware paces samples at ``fs``
    and that no lines are lost (lost lines compress time slightly)."""

    def __init__(self, fs: float):
        self.fs = fs
        self._n = 0

    def update(self) -> float:
        t = self._n / self.fs
        self._n += 1
        return t


# ---------------------------------------------------------------------------
# Serial port access
# ---------------------------------------------------------------------------

class SerialError(RuntimeError):
    """Raised for port-discovery / connection problems (never ``sys.exit``)."""


ARDUINO_VIDS = (0x2341, 0x2A03)
_KEYWORDS = ("arduino", "ch340", "wch", "cp210", "acm", "usb serial", "usb-serial")


def find_port() -> str:
    """Pick the most likely Arduino port.

    Order: Arduino USB vendor ID, then a description keyword.  Bluetooth
    virtual ports are skipped (the original keyword list matched any port whose
    description contained "serial", which on Windows includes "Standard Serial
    over Bluetooth link").
    """
    try:
        from serial.tools import list_ports
    except ImportError as exc:                       # pragma: no cover
        raise SerialError("pyserial is not installed (pip install pyserial)") from exc

    ports = list(list_ports.comports())
    candidates = [p for p in ports if "bluetooth" not in str(p).lower()]
    for p in candidates:
        if getattr(p, "vid", None) in ARDUINO_VIDS:
            return p.device
    for p in candidates:
        if any(k in str(p).lower() for k in _KEYWORDS):
            return p.device
    listing = "\n".join(f"  - {p}" for p in ports) or "  (none)"
    raise SerialError(f"no Arduino-like serial port found. Available ports:\n{listing}\n"
                      "Pass one explicitly with --port.")


def open_serial(port: str = "AUTO", baud: int = 115200, timeout: float = 1.0, settle_s: float = 2.0):
    """Open the port (auto-detecting if ``port == 'AUTO'``) and flush stale input."""
    try:
        import serial
    except ImportError as exc:                       # pragma: no cover
        raise SerialError("pyserial is not installed (pip install pyserial)") from exc
    if port.upper() == "AUTO":
        port = find_port()
    try:
        ser = serial.Serial(port, baud, timeout=timeout)
    except serial.SerialException as exc:
        raise SerialError(f"could not open {port}: {exc}") from exc
    time.sleep(settle_s)                # board resets when the port opens
    ser.reset_input_buffer()
    return ser


@dataclass
class StreamStats:
    lines: int = 0
    samples: int = 0
    parse_failures: int = 0
    range_failures: int = 0
    format_changes: int = 0
    empty_reads: int = 0
    stop_reason: str = ""


class LineStream:
    """Iterates validated ``Sample`` objects from anything with ``readline()``
    (a ``serial.Serial`` or a test double).

    Robustness measures over the original read loop:

    * the first line after connecting is discarded (it is usually partial, and
      a truncated numeric line still parses as a valid, wrong number);
    * values outside ``0..adc_max`` are rejected;
    * once a wire format has been seen, lines in another format are rejected;
    * malformed lines are counted in ``stats`` instead of vanishing silently;
    * a port error or ``no_data_timeout_s`` without a valid sample ends the
      iteration with ``stats.stop_reason`` set, so the caller can still close
      its files and write a summary.
    """

    def __init__(self, port, adc_max: int = 16383, no_data_timeout_s: float = 5.0,
                 host_clock: Callable[[], float] = time.monotonic, discard_first: bool = True):
        self.port = port
        self.adc_max = adc_max
        self.no_data_timeout_s = no_data_timeout_s
        self.host_clock = host_clock
        self.discard_first = discard_first
        self.stats = StreamStats()
        self.format: Optional[str] = None
        self.last_host_time = 0.0

    def _in_range(self, value: Optional[int]) -> bool:
        return value is None or 0 <= value <= self.adc_max

    def __iter__(self) -> Iterator[Sample]:
        last_good = self.host_clock()
        first = self.discard_first
        while True:
            try:
                raw = self.port.readline()
            except Exception as exc:        # serial.SerialException, OSError on unplug
                self.stats.stop_reason = f"serial read failed: {exc}"
                return
            now = self.host_clock()
            if not raw:
                self.stats.empty_reads += 1
                if now - last_good > self.no_data_timeout_s:
                    self.stats.stop_reason = f"no valid data for {self.no_data_timeout_s:.0f} s"
                    return
                continue
            self.stats.lines += 1
            if first:
                first = False
                continue
            sample = parse_line(raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw)
            if sample is None:
                self.stats.parse_failures += 1
            elif not (self._in_range(sample.h) and self._in_range(sample.v)):
                self.stats.range_failures += 1
            elif self.format is not None and sample.fmt != self.format:
                self.stats.format_changes += 1
            else:
                self.format = sample.fmt
                self.stats.samples += 1
                last_good = now
                self.last_host_time = now
                yield sample
                continue
            if now - last_good > self.no_data_timeout_s:
                self.stats.stop_reason = f"no valid data for {self.no_data_timeout_s:.0f} s"
                return


# ---------------------------------------------------------------------------
# Recorded CSV files
# ---------------------------------------------------------------------------

_NAME = re.compile(r"test_(\d{2})_(\w+?)_(H|V|BOTH)_(\d{8}_\d{6})\.csv$")
_NAME_OLD = re.compile(r"test_(\d{2})_(\w+?)_(\d{8}_\d{6})\.csv$")


def identify_run_file(path: str) -> Optional[dict]:
    """Parse ``test_<NN>_<tag>_<H|V|BOTH>_<YYYYMMDD_HHMMSS>.csv``."""
    base = os.path.basename(path)
    m = _NAME.match(base)
    if m:
        return {"test_num": int(m.group(1)), "tag": m.group(2), "channel": m.group(3),
                "timestamp": m.group(4), "filename": base}
    m = _NAME_OLD.match(base)
    if m:
        return {"test_num": int(m.group(1)), "tag": m.group(2), "channel": "H",
                "timestamp": m.group(3), "filename": base}
    return None


@dataclass
class Recording:
    """A recorded run: timestamps, raw channel data and all original columns."""
    path: str
    t_s: List[float]
    channels: Dict[str, List[float]]
    columns: Dict[str, List[str]]
    skipped_rows: int = 0
    info: Optional[dict] = None

    def __len__(self) -> int:
        return len(self.t_s)


def read_recording(path: str) -> Recording:
    """Read a run CSV (original or current format).

    Requires a ``Time_ms`` column and at least one ``Raw_H`` / ``Raw_V``
    column.  Rows whose time or raw value is not numeric (for example a last
    line truncated by an interrupted run) are skipped and counted.
    """
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        raw_cols = {c[4:]: c for c in fields if c in ("Raw_H", "Raw_V")}
        if "Time_ms" not in fields or not raw_cols:
            raise ValueError(f"{path}: need 'Time_ms' and a 'Raw_H'/'Raw_V' column, found {fields}")
        t_s: List[float] = []
        channels: Dict[str, List[float]] = {ch: [] for ch in raw_cols}
        columns: Dict[str, List[str]] = {c: [] for c in fields}
        skipped = 0
        for row in reader:
            try:
                t = float(row["Time_ms"]) / 1000.0
                values = {ch: float(row[col]) for ch, col in raw_cols.items()}
            except (TypeError, ValueError):
                skipped += 1
                continue
            t_s.append(t)
            for ch, value in values.items():
                channels[ch].append(value)
            for c in fields:
                columns[c].append(row[c])
    return Recording(path=path, t_s=t_s, channels=channels, columns=columns,
                     skipped_rows=skipped, info=identify_run_file(path))


def estimate_fs(t_s: List[float]) -> float:
    """Effective sample rate of a recording, snapped to the nearest integer Hz
    when within 1 % of it (recorded host timestamps are quantised to 1 ms)."""
    if len(t_s) < 2 or t_s[-1] <= t_s[0]:
        return 0.0
    fs = (len(t_s) - 1) / (t_s[-1] - t_s[0])
    nearest = round(fs)
    if nearest > 0 and abs(fs - nearest) / nearest < 0.01:
        return float(nearest)
    return fs


# ---------------------------------------------------------------------------
# Run output
# ---------------------------------------------------------------------------

@dataclass
class RunWriter:
    """Writes one channel's CSV (flushed every ``flush_every`` rows) and its
    JSON summary.  File names keep the original convention so the analysis
    script can group them: ``test_<NN>_<tag>_<CH>_<stamp>.csv``."""

    out_dir: str
    test_num: int
    tag: str
    channel: str
    stamp: str
    flush_every: int = 10
    csv_path: str = field(init=False)
    json_path: str = field(init=False)

    def __post_init__(self):
        os.makedirs(self.out_dir, exist_ok=True)
        stem = f"test_{self.test_num:02d}_{self.tag}_{self.channel}_{self.stamp}"
        self.csv_path = os.path.join(self.out_dir, stem + ".csv")
        self.json_path = os.path.join(self.out_dir, stem + ".summary.json")
        self._file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(csv_header(self.channel))
        self._rows = 0

    def write(self, row: dict) -> None:
        self._writer.writerow(ChannelPipeline.format_row(row))
        self._rows += 1
        if self._rows % self.flush_every == 0:
            self._file.flush()

    def close(self, summary: Optional[dict] = None) -> None:
        if not self._file.closed:
            self._file.flush()
            self._file.close()
        if summary is not None:
            with open(self.json_path, "w", encoding="utf-8") as jf:
                json.dump(summary, jf, indent=2)
