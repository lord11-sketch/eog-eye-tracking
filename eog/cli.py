"""Command-line interface.

    python -m eog methods                      list the methods
    python -m eog live    --method combined --channel H --port COM5
    python -m eog replay  --input <run.csv> --method combined
    python -m eog compare --input data/recorded
    python -m eog verify  --data data/recorded

``live`` falls back to interactive prompts when ``--method`` / ``--channel``
are omitted and the terminal is interactive.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from . import io as eio
from . import replay as rp
from .filters import REFERENCE_FS
from .methods import COMPARISON_ORDER, METHODS, MethodSpec, get_method, with_options
from .pipeline import ChannelPipeline
from .units import AdcConfig

CHANNEL_CHOICES = ("H", "V", "BOTH")


# ---------------------------------------------------------------------------
# Shared option groups
# ---------------------------------------------------------------------------

def _add_method_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("method variants")
    g.add_argument("--original-smoothing", action="store_true",
                   help="Tests 06/07: use the as-recorded dual-mode coefficient assignment "
                        "(saccades smoothed heavily, fixation hardly). The default is the corrected "
                        "assignment: heavy smoothing in fixation, light during saccades")
    g.add_argument("--butterworth", action="store_true",
                   help="Tests 02/03: use true 2nd-order Butterworth sections instead of the "
                        "original first-order RC sections")
    g.add_argument("--no-rate-compensation", action="store_true",
                   help="use the per-sample constants literally instead of converting them from "
                        f"the {REFERENCE_FS:g} Hz reference rate to the actual sample rate")


def _add_unit_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("unit conversion (metrics are computed in ADC counts)")
    g.add_argument("--adc-bits", type=int, default=14)
    g.add_argument("--adc-vref", type=float, default=5.0, help="ADC reference in volts (nominal 5.0)")
    g.add_argument("--gain", type=float, default=None,
                   help="total analogue front-end gain (V/V); enables electrode-referred microvolts")


def _adc(args) -> AdcConfig:
    return AdcConfig(bits=args.adc_bits, vref=args.adc_vref, gain=args.gain)


def _spec(args, name: str) -> MethodSpec:
    return with_options(get_method(name), original_smoothing=args.original_smoothing,
                        butterworth=args.butterworth)


def _print_summary(summary: dict) -> None:
    d, x, leg = summary["direction"], summary["detector_input"], summary["legacy"]
    print(f"  [{summary['channel']}] {summary['title']}")
    print(f"      samples {summary['samples']}  duration {summary['duration_s']:.1f} s  "
          f"rate {summary['sample_rate_hz']:.2f} Hz")
    print(f"      centre {d['centre_pct']:.1f} %  negative {d['negative_pct']:.1f} %  "
          f"positive {d['positive_pct']:.1f} %  direction changes {d['changes']}")
    print(f"      residual drift {x['residual_drift_counts_per_min']:.0f} counts/min "
          f"({x['residual_drift_mv_at_adc_per_min']:.1f} mV/min at ADC)  "
          f"noise sigma {x['noise_sigma_counts']:.0f} counts  "
          f"peak-to-noise {x['peak_to_noise_db']:.1f} dB")
    print(f"      legacy: baseline excursion {leg['baseline_excursion_counts_per_min']:.0f} counts/min, "
          f"removed-component ratio {leg['removed_component_ratio_db']:.2f} dB")
    if summary["counters"]:
        print("      counters: " + ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                                              for k, v in summary["counters"].items()))


# ---------------------------------------------------------------------------
# methods
# ---------------------------------------------------------------------------

def cmd_methods(args) -> int:
    print(f"{'key':20s} {'test':4s} {'origin':10s} title")
    for spec in METHODS.values():
        print(f"{spec.key:20s} {spec.test_num:02d}   {spec.origin:10s} {spec.title}  {spec.citation}")
    return 0


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------

def prompt_method() -> str:
    print("\nSelect method:")
    keys = list(COMPARISON_ORDER)
    for i, k in enumerate(keys, 1):
        print(f"  [{i}] Test {METHODS[k].test_num:02d}  {METHODS[k].title}")
    while True:
        choice = input(f"\nChoice (1-{len(keys)}): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            return keys[int(choice) - 1]
        print("Invalid choice")


def prompt_channel() -> str:
    print("\nSelect channel to test:")
    print("  [1] Horizontal input only (A0, LEFT/RIGHT)   <- use this for a single EXG Pill")
    print("  [2] Vertical input only   (A1, UP/DOWN)")
    print("  [3] Both inputs, processed simultaneously")
    print("\n  NOTE: channel = which Arduino input the pill is on,")
    print("        not the direction of eye movement being tested.")
    while True:
        choice = input("\nChoice (1/2/3): ").strip()
        if choice in ("1", "2", "3"):
            return CHANNEL_CHOICES[int(choice) - 1]
        print("Invalid - enter 1, 2 or 3")


def run_live(port, spec: MethodSpec, channels: List[str], *, out_dir: str, duration_s: float,
             fs: Optional[float], clock: str, rate_compensate: bool, calibration_s: float,
             adc: AdcConfig, stamp: Optional[str] = None, quiet: bool = False,
             host_clock=time.monotonic) -> Dict[str, dict]:
    """Acquire from ``port`` (anything with ``readline()``), process every
    requested channel on each sample, write CSV + JSON per channel.

    Always returns the summaries, including after Ctrl+C, a port error or a
    data timeout; output files are closed in every case.
    """
    stream = eio.LineStream(port, adc_max=adc.full_scale, host_clock=host_clock)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    pipes: Dict[str, ChannelPipeline] = {}
    writers: Dict[str, eio.RunWriter] = {}
    micros = eio.MicrosClock()
    counter: Optional[eio.SampleCountClock] = None
    host_t0: Optional[float] = None
    used_clock = clock
    record_start: Optional[float] = None
    missing_since: Dict[str, Optional[float]] = {ch: None for ch in channels}
    warned: Dict[str, bool] = {ch: False for ch in channels}
    last_status = 0.0
    last_t = 0.0

    def say(msg: str, **kw) -> None:
        if not quiet:
            print(msg, **kw)

    try:
        for sample in stream:
            if not pipes:
                # First valid sample: the wire format tells us which firmware is talking.
                if fs is None:
                    fs = eio.NOMINAL_FS.get(sample.fmt, REFERENCE_FS)
                if used_clock == "auto":
                    used_clock = "firmware" if sample.t_us is not None else "samples"
                if used_clock == "firmware" and sample.t_us is None:
                    raise eio.SerialError("--clock firmware needs the v3 firmware "
                                          "('H,V,micros' lines); this stream has no timestamps")
                counter = eio.SampleCountClock(fs)
                for ch in channels:
                    pipes[ch] = ChannelPipeline(spec, ch, fs, rate_compensate=rate_compensate,
                                                calibration_s=calibration_s)
                    writers[ch] = eio.RunWriter(out_dir, spec.test_num, spec.file_tag, ch, stamp)
                say(f"Stream format '{sample.fmt}', nominal {fs:g} Hz, sample clock '{used_clock}'")
                say(f"Calibrating for {calibration_s:g} s - look straight ahead and keep still")

            if used_clock == "firmware":
                t = micros.update(sample.t_us)
            elif used_clock == "samples":
                t = counter.update()
            else:                                   # host arrival time (original behaviour)
                if host_t0 is None:
                    host_t0 = stream.last_host_time
                t = stream.last_host_time - host_t0
            last_t = t

            for ch in channels:
                raw = sample.h if ch == "H" else sample.v
                if raw is None:
                    if missing_since[ch] is None:
                        missing_since[ch] = t
                    elif not warned[ch] and t - missing_since[ch] > 3.0:
                        warned[ch] = True
                        say(f"\nWARNING: no data on channel {ch} (stream carries H={sample.h} "
                            f"V={sample.v}). Check the channel selection and wiring.")
                    continue
                pipe = pipes[ch]
                was_calibrated = pipe.calibrated
                row = pipe.step(raw, t)
                if not was_calibrated and pipe.calibrated:
                    say(f"\n[{ch}] calibration done ({len(pipe.calibrator.values)} samples)")
                    if record_start is None:
                        record_start = t
                if row is not None:
                    writers[ch].write(row)

            now = host_clock()
            if not quiet and now - last_status >= 0.5:
                last_status = now
                parts = []
                for ch, pipe in pipes.items():
                    if pipe.calibrated and pipe.directions:
                        parts.append(f"[{ch}] {pipe.directions[-1]:6s} in={pipe.detector_input[-1]:8.1f}")
                    else:
                        parts.append(f"[{ch}] calibrating {pipe.calibrator.elapsed(t):.1f}s")
                print("\r" + " | ".join(parts) + f"  t={t:6.1f}s", end="", flush=True)

            if duration_s > 0 and record_start is not None and t - record_start >= duration_s:
                say(f"\n{duration_s:g}-second recording complete.")
                break
    except KeyboardInterrupt:
        say("\nStopped by user.")
    finally:
        summaries: Dict[str, dict] = {}
        for ch, pipe in pipes.items():
            s = pipe.summary(adc)
            s["clock"] = used_clock
            s["stream"] = dict(vars(stream.stats), format=stream.format)
            s["recorded_at"] = stamp
            writers[ch].close(s)
            s["csv_file"] = os.path.basename(writers[ch].csv_path)
            summaries[ch] = s

    if stream.stats.stop_reason:
        say(f"\nStream ended: {stream.stats.stop_reason}")
    st = stream.stats
    say(f"Lines read {st.lines}, samples {st.samples}, rejected: {st.parse_failures} malformed, "
        f"{st.range_failures} out of range, {st.format_changes} wrong format")
    for ch, s in summaries.items():
        expected = fs or 0.0
        if s["samples"] > 10 and expected and abs(s["sample_rate_hz"] - expected) / expected > 0.05:
            say(f"WARNING [{ch}]: measured rate {s['sample_rate_hz']:.1f} Hz differs from the "
                f"nominal {expected:g} Hz used to build the filters; rerun with --fs.")
    return summaries


def cmd_live(args) -> int:
    interactive = sys.stdin.isatty()
    method = args.method
    channel = args.channel
    if method is None:
        if not interactive:
            print("error: --method is required when not running interactively", file=sys.stderr)
            return 2
        method = prompt_method()
    if channel is None:
        if not interactive:
            print("error: --channel is required when not running interactively", file=sys.stderr)
            return 2
        channel = prompt_channel()
    spec = _spec(args, method)
    channels = ["H", "V"] if channel == "BOTH" else [channel]

    print("=" * 70)
    print(f"TEST {spec.test_num:02d}: {spec.title}")
    print("=" * 70)
    try:
        port = eio.open_serial(args.port, args.baud)
    except eio.SerialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Connected to {port.port} @ {args.baud}")
    try:
        summaries = run_live(port, spec, channels, out_dir=args.out_dir, duration_s=args.duration,
                             fs=args.fs, clock=args.clock,
                             rate_compensate=not args.no_rate_compensation,
                             calibration_s=args.calibration_s, adc=_adc(args))
    except eio.SerialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        port.close()

    print("\n" + "=" * 70)
    for s in summaries.values():
        _print_summary(s)
        print(f"      CSV: {os.path.join(args.out_dir, s['csv_file'])}")
    if not summaries:
        print("No samples were received.")
        return 1
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def cmd_replay(args) -> int:
    spec = _spec(args, args.method)
    rec = eio.read_recording(args.input)
    channels = [args.channel] if args.channel else sorted(rec.channels)
    fs = args.fs or eio.estimate_fs(rec.t_s)
    print(f"Replaying {os.path.basename(args.input)}: {len(rec)} samples, "
          f"measured rate {eio.estimate_fs(rec.t_s):.2f} Hz, clock '{args.clock}'")
    if rec.skipped_rows:
        print(f"  ({rec.skipped_rows} unreadable rows skipped)")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for ch in channels:
        writer = eio.RunWriter(args.out_dir, spec.test_num, spec.file_tag, ch, stamp) if args.out_dir else None
        pipe = rp.replay(rec, spec, ch, fs=fs, clock=args.clock,
                         rate_compensate=not args.no_rate_compensation,
                         calibration_s=args.calibration_s,
                         on_row=writer.write if writer else None)
        summary = pipe.summary(_adc(args))
        summary["replayed_from"] = os.path.basename(args.input)
        summary["clock"] = args.clock
        summary["variant"] = rp.variant_label(spec)
        _print_summary(summary)
        if writer:
            writer.close(summary)
            print(f"      CSV:  {writer.csv_path}\n      JSON: {writer.json_path}")
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

COMPARE_FIELDS = ["centre_pct", "direction_changes", "residual_drift_counts_per_min",
                  "noise_sigma_counts", "peak_to_noise_db",
                  "legacy_baseline_excursion_counts_per_min", "legacy_removed_component_ratio_db",
                  "saccade_sample_fraction", "blink_count"]


def _fmt(mean, sd, digits=1) -> str:
    if mean is None:
        return "-"
    return f"{mean:.{digits}f} +/- {sd:.{digits}f}"


def cmd_compare(args) -> int:
    files = rp.find_run_csvs(args.input)
    if args.only_test is not None:
        files = [f for f in files if (eio.identify_run_file(f) or {}).get("test_num") == args.only_test]
    if not files:
        print("error: no run CSV files found", file=sys.stderr)
        return 1
    recordings = [eio.read_recording(f) for f in files]
    specs = [_spec(args, k) for k in (args.methods or COMPARISON_ORDER)]
    if args.include_variants:
        extra = []
        for s in specs:
            for opts in ({"original_smoothing": True}, {"butterworth": True}):
                v = with_options(METHODS[s.key], **opts)
                if v is not METHODS[s.key] and v not in extra and v not in specs:
                    extra.append(v)
        specs += extra

    rows = rp.compare(recordings, specs, clock=args.clock, fs=args.fs,
                      rate_compensate=not args.no_rate_compensation, adc=_adc(args))
    agg = rp.aggregate(rows, ["channel", "test_num", "method", "variant"], COMPARE_FIELDS)

    print(f"Replayed {len(specs)} method configurations over {len(recordings)} recordings "
          f"(identical input per method). Values are mean +/- SD across recordings.")
    print("All quantities are RECOMPUTED by offline replay, in ADC counts.\n")
    head = (f"{'ch':2s} {'method':18s} {'variant':20s} {'n':>3s} {'centre %':>16s} "
            f"{'dir changes':>14s} {'resid drift c/min':>20s} {'noise sigma':>14s} {'peak/noise dB':>14s}")
    print(head)
    print("-" * len(head))
    for a in agg:
        print(f"{a['channel']:2s} {a['method']:18s} {a['variant']:20s} {a['n']:3d} "
              f"{_fmt(a['centre_pct_mean'], a['centre_pct_sd']):>16s} "
              f"{_fmt(a['direction_changes_mean'], a['direction_changes_sd']):>14s} "
              f"{_fmt(a['residual_drift_counts_per_min_mean'], a['residual_drift_counts_per_min_sd'], 0):>20s} "
              f"{_fmt(a['noise_sigma_counts_mean'], a['noise_sigma_counts_sd']):>14s} "
              f"{_fmt(a['peak_to_noise_db_mean'], a['peak_to_noise_db_sd']):>14s}")

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        _write_csv(os.path.join(args.out_dir, "replay_comparison_per_recording.csv"), rows)
        _write_csv(os.path.join(args.out_dir, "replay_comparison_summary.csv"), agg)
        print(f"\nWrote {args.out_dir}/replay_comparison_per_recording.csv and "
              f"replay_comparison_summary.csv")
    return 0


def _write_csv(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in r.items()})


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args) -> int:
    files = rp.find_run_csvs([args.data])
    if not files:
        print("error: no run CSV files found", file=sys.stderr)
        return 1
    results, json_checks = [], []
    for f in files:
        rec = eio.read_recording(f)
        if rec.info is None:
            continue
        results.append(rp.verify_recording(rec))
        js = rp.matching_summary_json(f)
        if js:
            json_checks.extend(rp.verify_summary_json(js, rec))

    print("Replay verification: recorded file re-processed by the method that produced it.")
    print("(filter state seeded from row 0; clock = recorded Time_ms)\n")
    head = (f"{'session':22s} {'test':4s} {'method':18s} {'dir agree %':>11s} {'RMSE':>8s} "
            f"{'RMSE %sd':>8s} {'RMSE>5s':>8s} {'max err':>8s}")
    print(head)
    print("-" * len(head))
    for r in results:
        print(f"{r['session']:22s} {r['test_num']:02d}   {r['method']:18s} "
              f"{r['direction_agreement_pct']:11.2f} {r['filtered_rmse_counts']:8.2f} "
              f"{r['filtered_rmse_pct_of_sd']:8.2f} {r['filtered_rmse_after_5s_counts']:8.2f} "
              f"{r['filtered_max_abs_err_counts']:8.1f}")

    by_test = rp.aggregate(results, ["test_num", "method"],
                           ["direction_agreement_pct", "filtered_rmse_counts",
                            "filtered_rmse_pct_of_sd", "filtered_rmse_after_5s_counts"])
    print("\nPer method (mean over files):")
    for a in by_test:
        print(f"  Test {a['test_num']:02d} {a['method']:18s} n={a['n']}  "
              f"direction agreement {a['direction_agreement_pct_mean']:.2f} %  "
              f"RMSE {a['filtered_rmse_counts_mean']:.2f} counts "
              f"({a['filtered_rmse_pct_of_sd_mean']:.2f} % of signal SD)")

    worst = max((c["abs_diff"] for c in json_checks), default=0.0)
    exact = sum(1 for c in json_checks if c["abs_diff"] < 1e-6)
    print(f"\nRecorded JSON summaries recomputed from their CSVs: {len(json_checks)} fields, "
          f"{exact} identical to 1e-6, worst absolute difference {worst:.4g}")
    for c in sorted(json_checks, key=lambda c: -c["abs_diff"])[:5]:
        if c["abs_diff"] >= 1e-6:
            print(f"  {c['json']} {c['field']}: recorded {c['recorded']:.4f} recomputed {c['recomputed']:.4f}")

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        _write_csv(os.path.join(args.out_dir, "replay_verification.csv"), results)
        _write_csv(os.path.join(args.out_dir, "json_summary_recomputation.csv"), json_checks)
        print(f"\nWrote {args.out_dir}/replay_verification.csv and json_summary_recomputation.csv")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eog", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("methods", help="list available methods")
    p.set_defaults(func=cmd_methods)

    p = sub.add_parser("live", help="acquire from the serial port and run one method")
    p.add_argument("--method", help="method key or test number (see 'methods'); prompts if omitted")
    p.add_argument("--channel", choices=CHANNEL_CHOICES, help="H, V or BOTH; prompts if omitted")
    p.add_argument("--port", default="AUTO", help="serial port, e.g. COM5 or /dev/ttyACM0 (default: auto-detect)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--duration", type=float, default=30.0,
                   help="seconds to record after calibration; 0 = until Ctrl+C (default 30)")
    p.add_argument("--fs", type=float, default=None,
                   help="sample rate in Hz (default: 50 for 'H:..,V:..' streams, 250 for CSV streams)")
    p.add_argument("--clock", choices=("auto", "firmware", "samples", "host"), default="auto",
                   help="sample clock: firmware micros() timestamps, sample count / fs, or host "
                        "arrival time (the original behaviour). auto = firmware if available, else samples")
    p.add_argument("--calibration-s", type=float, default=3.0)
    p.add_argument("--out-dir", default="results", help="output directory (default: results/)")
    _add_method_options(p)
    _add_unit_options(p)
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("replay", help="run one method over a recorded CSV (no hardware)")
    p.add_argument("--input", required=True, help="recorded run CSV with Time_ms and Raw_H/Raw_V")
    p.add_argument("--method", required=True)
    p.add_argument("--channel", choices=("H", "V"), help="default: every raw channel in the file")
    p.add_argument("--fs", type=float, default=None, help="sample rate (default: measured from the file)")
    p.add_argument("--clock", choices=("recorded", "samples"), default="recorded")
    p.add_argument("--calibration-s", type=float, default=3.0)
    p.add_argument("--out-dir", default="analysis_output/replay",
                   help="where to write the regenerated CSV + JSON ('' to skip)")
    _add_method_options(p)
    _add_unit_options(p)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("compare", help="run all methods over the same recordings")
    p.add_argument("--input", nargs="+", required=True, help="recorded CSV files and/or directories")
    p.add_argument("--methods", nargs="+", help="subset of methods (default: the seven tests)")
    p.add_argument("--only-test", type=int, default=None,
                   help="use only recordings of this test number as input (e.g. 1)")
    p.add_argument("--include-variants", action="store_true",
                   help="also run the original-smoothing and Butterworth variants")
    p.add_argument("--fs", type=float, default=None)
    p.add_argument("--clock", choices=("recorded", "samples"), default="recorded")
    p.add_argument("--out-dir", default="analysis_output", help="where to write the tables ('' to skip)")
    _add_method_options(p)
    _add_unit_options(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("verify", help="check that replay reproduces the recorded outputs")
    p.add_argument("--data", default="data/recorded")
    p.add_argument("--out-dir", default="analysis_output", help="where to write the tables ('' to skip)")
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
