#!/usr/bin/env python3
"""Aggregate analysis of recorded runs.

Reads every ``test_*.csv`` under a data directory (recursively) and writes:

* ``results_master_table.csv`` - one row per test / channel, mean and SD
  across runs, computed from the columns each run recorded;
* ``results_summary.txt``      - the same in readable form;
* ``figures/``                 - optional (``--figures``, needs matplotlib).

Usage (from the repository root)::

    python analysis/eog_analysis.py --data data/recorded
    python analysis/eog_analysis.py --data data/recorded --figures

Relationship to the original script
-----------------------------------
The arithmetic of the three headline quantities is unchanged, so the numbers
match what the original ``eog_analysis.py`` produced from the same files; only
the names changed, because the original names claimed more than was measured:

    original name        name here                         why
    "Stability %"        centre %                          fraction of samples labelled CENTER
    "Drift uV/min"       baseline excursion, counts/min    ADC counts, and it tracks the baseline
                                                           estimate, not residual drift
    "SNR dB"             removed-component ratio, dB       std(filtered) / std(raw - filtered)

See docs/methods-review.md.  Tests 06 and 07 were recorded
with the original smoothing assignment, and the recorded ``test_07_full_*`` runs
are the April variant (04 + 05 + 06), not the project's Test 07.  These per-run
numbers come from *different* recordings per method; for a like-for-like comparison use
``python -m eog compare`` (same input for every method).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eog import io as eio                     # noqa: E402
from eog import metrics as M                  # noqa: E402
from eog import replay as rp                  # noqa: E402
from eog.methods import COMPARISON_ORDER, METHODS, method_for_recorded_file  # noqa: E402

TEST_NAMES = {
    1: "Unfiltered (raw baseline)",
    2: "Fixed band-pass 0.1-15 Hz, first-order sections [3]",
    3: "Low-cutoff HPF 0.05 Hz, cascaded first-order [15]",
    4: "Adaptive baseline with velocity gate (novel)",
    5: "Blink-locked reset (novel - documented negative result)",
    6: "Velocity-domain dual-mode smoothing (novel)",
    7: "Combined system, 04 + 06 (novel)",
}

APRIL_FULL = "April 'full system' (04 + 05 + 06) - historical variant, not the project's Test 07"


def test_name(test_num, tag):
    """Display name of a recorded run.  Recorded ``test_07_full_*`` files are the
    April variant with the blink reset; the project's Test 07 (04 + 06) exists
    only as offline replay (``python -m eog compare``)."""
    if test_num == 7 and tag == "full":
        return APRIL_FULL
    return TEST_NAMES.get(test_num, tag)


# (key, column title, decimals)
TABLE_FIELDS = [
    ("centre_pct", "Centre % [was 'Stability %']", 1),
    ("baseline_excursion_counts_per_min", "Baseline excursion counts/min [was 'Drift uV/min']", 1),
    ("removed_component_ratio_db", "Removed-component ratio dB [was 'SNR dB']", 2),
    ("raw_std", "Raw std (counts)", 1),
    ("filt_std", "Filtered std (counts)", 1),
    ("direction_changes", "Direction changes", 1),
    ("removed_component_std", "Removed-component std (counts) [was 'Noise floor']", 1),
    ("sample_rate_hz", "Sample rate (Hz)", 2),
]


def _floats(values):
    out = []
    for v in values:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            pass
    return out


def channel_columns(rec, info):
    """[(channel, raw, filtered, direction, baseline)] column names for a file,
    covering the original single-channel, the original BOTH and the current layout."""
    cols = rec.columns

    def first(*names):
        return next((n for n in names if n in cols), None)

    out = []
    dual = "Raw_H" in cols and "Raw_V" in cols
    for ch in sorted(rec.channels):
        if dual:
            out.append((ch, f"Raw_{ch}", first(f"Filt_{ch}", f"Filtered_{ch}"),
                        first(f"Direction_{ch}", f"Dir_{ch}"), first(f"Baseline_{ch}", f"Base_{ch}")))
        else:
            out.append((ch, f"Raw_{ch}", first(f"Filtered_{ch}"), first("Direction"),
                        first("Baseline", "Adaptive_Baseline")))
    return out


def extract_metrics(rec):
    """Per-channel metrics of one recorded file, from its own columns."""
    info = rec.info
    t = rec.t_s
    # The original divided by max(Time_ms), which includes the ~3 s calibration
    # offset.  Kept for the legacy excursion figure so recorded numbers are
    # reproduced; the sample rate uses the correct first-to-last span.
    legacy_duration = t[-1] if t else 0.0
    results = []
    for ch, raw_col, filt_col, dir_col, base_col in channel_columns(rec, info):
        raw = rec.channels[ch]
        m = {
            "test_num": info["test_num"], "tag": info["tag"], "channel": ch,
            "filename": info["filename"], "samples": len(raw),
            "duration_s": (t[-1] - t[0]) if len(t) > 1 else 0.0,
            "sample_rate_hz": M.effective_sample_rate_hz(t),
            "raw_mean": M.mean(raw), "raw_std": M.stdev(raw),
            "raw_range": (max(raw) - min(raw)) if raw else 0.0,
        }
        if filt_col:
            filt = _floats(rec.columns[filt_col])
            if len(filt) == len(raw) and len(raw) > 1:
                m["filt_std"] = M.stdev(filt)
                m["removed_component_ratio_db"] = M.removed_component_ratio_db(raw, filt)
                m["removed_component_std"] = M.stdev([r - f for r, f in zip(raw, filt)])
        if base_col:
            base = _floats(rec.columns[base_col])
            if len(base) > 10:
                m["baseline_excursion_counts"] = M.baseline_excursion(base)
                m["baseline_excursion_counts_per_min"] = M.baseline_excursion_per_min(base, legacy_duration)
        if dir_col:
            dirs = rec.columns[dir_col]
            if dirs:
                f = M.direction_fractions(dirs)
                m.update(centre_pct=f["centre_pct"], negative_pct=f["negative_pct"],
                         positive_pct=f["positive_pct"], direction_changes=M.direction_changes(dirs))
        results.append(m)
    return results


def aggregate(all_metrics):
    groups = defaultdict(list)
    for m in all_metrics:
        groups[(m["test_num"], m["tag"], m["channel"])].append(m)
    out = {}
    for key, runs in groups.items():
        agg = {"n_runs": len(runs)}
        fields = {k for r in runs for k, v in r.items() if isinstance(v, (int, float))}
        for f in fields - {"test_num"}:
            values = [r[f] for r in runs if f in r]
            agg[f + "_mean"] = M.mean(values)
            agg[f + "_std"] = M.stdev(values)
        out[key] = agg
    return out


def _cell(agg, key, suffix, digits):
    value = agg.get(f"{key}_{suffix}")
    return "" if value is None else f"{value:.{digits}f}"


def write_master_table(aggregated, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        header = ["Test", "Method", "File tag", "Channel", "Runs"]
        for _, title, _ in TABLE_FIELDS:
            header += [f"{title} (mean)", f"{title} (sd)"]
        w.writerow(header)
        for (test_num, tag, ch) in sorted(aggregated):
            a = aggregated[(test_num, tag, ch)]
            row = [f"Test {test_num:02d}", test_name(test_num, tag), tag, ch, a["n_runs"]]
            for key, _, digits in TABLE_FIELDS:
                row += [_cell(a, key, "mean", digits), _cell(a, key, "std", digits)]
            w.writerow(row)


def write_summary_txt(aggregated, path, n_files):
    with open(path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("EOG TEST SUITE - AGGREGATE ANALYSIS OF RECORDED RUNS\n")
        f.write(f"{n_files} CSV files. All amplitudes in raw ADC counts.\n")
        f.write("Each test was a separate recording, so differences between tests include\n")
        f.write("differences between recordings. For a like-for-like comparison run\n")
        f.write("'python -m eog compare --input <data dir>'.\n")
        f.write("=" * 80 + "\n")
        for (test_num, tag, ch) in sorted(aggregated):
            a = aggregated[(test_num, tag, ch)]
            f.write(f"\nTEST {test_num:02d}: {test_name(test_num, tag)} - channel {ch} "
                    f"({a['n_runs']} run{'s' if a['n_runs'] != 1 else ''})\n")
            for key, title, digits in TABLE_FIELDS:
                mean, sd = _cell(a, key, "mean", digits), _cell(a, key, "std", digits)
                if mean == "":
                    f.write(f"    {title:58s} n/a\n")
                else:
                    f.write(f"    {title:58s} {mean} +/- {sd}\n")
        f.write("\n" + "=" * 80 + "\n")
        f.write("No 'vs literature' comparison is printed: the original compared these\n")
        f.write("ADC-count figures against microvolt targets (see docs/methods-review.md).\n")


# ---------------------------------------------------------------------------
# Figures (optional)
# ---------------------------------------------------------------------------

INK = "#0b0b0b"
INK_2 = "#52514e"
SURFACE = "#fcfcfb"
GRID = "#e4e3df"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
SHORT = {"unfiltered": "01 Unfiltered", "rc_bandpass": "02 Band-pass 0.1-15 Hz",
         "soltani": "03 HPF 0.05 Hz", "adaptive": "04 Adaptive baseline",
         "blink_reset": "05 Blink reset", "velocity": "06 Velocity dual-mode",
         "combined": "07 Combined"}


def _style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def figure_replay_traces(rec, path, plt):
    """Small multiples: every method on one recording (identical input)."""
    from matplotlib.patches import Patch
    ch = sorted(rec.channels)[0]
    specs = [METHODS[k] for k in COMPARISON_ORDER]
    fig, axes = plt.subplots(len(specs) + 1, 1, figsize=(7.16, 9.6), sharex=True, facecolor=SURFACE)
    ax = axes[0]
    _style(ax)
    ax.plot(rec.t_s, rec.channels[ch], color=INK_2, linewidth=0.8)
    ax.set_title(f"Raw input (ADC counts) - {os.path.basename(rec.path)}", loc="left",
                 fontsize=9, color=INK)
    for ax, spec in zip(axes[1:], specs):
        _style(ax)
        pipe = rp.replay(rec, spec, ch)
        dz = pipe.detector.dead_zone
        ax.axhspan(-dz, dz, color=GRID, alpha=0.7, linewidth=0)
        ax.plot(pipe.t, pipe.detector_input, color=INK, linewidth=0.8)
        lo, hi = ax.get_ylim()
        strip = lo - 0.12 * (hi - lo)
        for labels, colour in ((("LEFT", "UP"), BLUE), (("RIGHT", "DOWN"), ORANGE)):
            mask = [d in labels for d in pipe.directions]
            ax.fill_between(pipe.t, strip, lo, where=mask, color=colour, linewidth=0, step="mid")
        ax.set_ylim(strip, hi)
        ax.set_title(f"{SHORT[spec.key]} - detector input", loc="left", fontsize=9, color=INK)
    axes[-1].set_xlabel("Time (s)", fontsize=9, color=INK_2)
    neg, pos = pipe.detector.neg_label, pipe.detector.pos_label
    fig.legend(handles=[Patch(color=BLUE, label=f"detected {neg}"),
                        Patch(color=ORANGE, label=f"detected {pos}"),
                        Patch(color=GRID, label="dead zone")],
               loc="lower right", ncol=3, frameon=False, fontsize=8, labelcolor=INK_2)
    fig.suptitle("Offline replay: every method on the same recording (recomputed)",
                 x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def figure_replay_summary(rows, path, plt):
    """Dot + SD range per method for three replay metrics, one panel per metric and channel."""
    panels = [("peak_to_noise_db", "Peak-to-noise (dB)"),
              ("direction_changes", "Direction changes"),
              ("centre_pct", "CENTER samples (%)")]
    channels = sorted({r["channel"] for r in rows})
    fig, axes = plt.subplots(len(channels), len(panels), figsize=(7.16, 2.3 * len(channels) + 0.8),
                             sharey=True, facecolor=SURFACE, squeeze=False)
    order = list(reversed(COMPARISON_ORDER))
    for i, ch in enumerate(channels):
        for j, (field, title) in enumerate(panels):
            ax = axes[i][j]
            _style(ax)
            ax.grid(axis="y", visible=False)
            ax.grid(axis="x", color=GRID, linewidth=0.6)
            for y, key in enumerate(order):
                values = [r[field] for r in rows
                          if r["channel"] == ch and r["method"] == key and r["variant"] == "default"]
                if not values:
                    continue
                mean, sd = M.mean(values), M.stdev(values)
                ax.plot([mean - sd, mean + sd], [y, y], color=BLUE, linewidth=2, solid_capstyle="round")
                ax.plot([mean], [y], "o", color=BLUE, markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.5)
            ax.set_yticks(range(len(order)))
            ax.set_yticklabels([SHORT[k] for k in order], fontsize=8, color=INK)
            if i == 0:
                ax.set_title(title, fontsize=8.5, color=INK, loc="left")
            if j == 0:
                n = len({r["source_file"] for r in rows if r["channel"] == ch})
                ax.set_ylabel(f"channel {ch} (n = {n})", fontsize=8.5, color=INK_2)
    fig.suptitle("Offline replay, identical input per method: mean and +/- 1 SD across recordings",
                 x=0.01, ha="left", fontsize=10, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def make_figures(recordings, out_dir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed - skipping figures (pip install matplotlib)")
        return []
    os.makedirs(out_dir, exist_ok=True)
    written = []
    by_axis = {}
    for rec in recordings:
        if rec.info["test_num"] == 1:
            by_axis.setdefault(sorted(rec.channels)[0], rec)
    for ch, rec in sorted(by_axis.items()):
        path = os.path.join(out_dir, f"replay_traces_{ch}.png")
        figure_replay_traces(rec, path, plt)
        written.append(path)
    rows = rp.compare(recordings, [METHODS[k] for k in COMPARISON_ORDER])
    path = os.path.join(out_dir, "replay_method_comparison.png")
    figure_replay_summary(rows, path, plt)
    written.append(path)
    return written


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data/recorded", help="directory searched recursively for test_*.csv")
    ap.add_argument("--out", default="analysis_output", help="output directory")
    ap.add_argument("--figures", action="store_true", help="also write figures (needs matplotlib)")
    ap.add_argument("--figures-dir", default=None, help="default: <out>/figures")
    args = ap.parse_args(argv)

    files = rp.find_run_csvs([args.data])
    if not files:
        print(f"ERROR: no test_*.csv files under '{args.data}'")
        return 1
    os.makedirs(args.out, exist_ok=True)

    recordings, all_metrics, skipped = [], [], []
    for path in files:
        try:
            rec = eio.read_recording(path)
            if rec.info is None:
                raise ValueError("file name does not match test_<NN>_<tag>_<CH>_<stamp>.csv")
            method_for_recorded_file(rec.info["test_num"], rec.info["tag"])
        except (ValueError, KeyError, OSError) as exc:
            skipped.append((path, str(exc)))
            continue
        recordings.append(rec)
        all_metrics.extend(extract_metrics(rec))

    aggregated = aggregate(all_metrics)
    master = os.path.join(args.out, "results_master_table.csv")
    summary = os.path.join(args.out, "results_summary.txt")
    write_master_table(aggregated, master)
    write_summary_txt(aggregated, summary, len(recordings))

    print(f"Read {len(recordings)} run files ({len(skipped)} skipped), {len(aggregated)} test/channel groups")
    for path, why in skipped:
        print(f"  skipped {path}: {why}")
    print(f"  {master}\n  {summary}")
    if args.figures:
        for path in make_figures(recordings, args.figures_dir or os.path.join(args.out, "figures")):
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
