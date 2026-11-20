# Methods review

An engineering review of the original test suite, carried out while restructuring it into
this repository. It records what was found, what was changed, what was deliberately left
alone, and what should be done next. File and line references are to the original scripts
(the "latest" snapshot unless marked *April*), which are not part of this repository.

Rules followed:

- The mathematics of each method is unchanged, with one deliberate exception adopted by the
  author: the dual-mode smoothing coefficients of Tests 06/07 are now assigned the right
  way round by default (section 4.4). `--original-smoothing` restores the as-recorded
  behaviour. `tests/test_original_equivalence.py` feeds identical input to the original
  classes and to this package (with original smoothing for Tests 06/07) and requires
  bit-identical output for all seven methods, the April "full system", and the detector.
- Recorded numbers are not edited. Where a metric was mislabelled it is **renamed**, with the
  same arithmetic, and the old name is kept in the docstring and table headers.
- Anything recomputed by offline replay is labelled "recomputed".
- Other improvements that change a method's behaviour are opt-in flags.

---

## 1. What the recordings show first

Three things in `data/recorded` shape everything below.

**1.1 The recorded Test 07 is not the project's Test 07.** The files are named
`test_07_full_*` and carry `Blinks` / `Resets` columns: they were produced by the April
"full system" (Tests 04 + 05 + 06), which still applied the blink reset. The project's
Test 07 is the combined system of Tests 04 + 06 only, and it has no recorded run of its
own. *Decision:* Test 07 is presented everywhere as the 04 + 06 system, with results
computed by offline replay over the raw columns of the recorded sessions and labelled as
such. The April files and their JSON summaries are kept unmodified as a historical variant
(`full_system_april`) and are used only to verify the replay code.

**1.2 The signal reaching the ADC is already high-pass filtered.** Every gaze shift appears
as a transient of several thousand counts that returns to mid-scale (about 8200 counts)
within roughly half a second, and the mean of every recording sits within about 100 counts
of mid-scale apart from two vertical runs
(`docs/figures/replay_traces_H.png`). This is the behaviour of an AC-coupled front end, not
of a DC-coupled EOG. Consequences:

- there is very little slow drift in these recordings for any software method to remove;
- a level-threshold detector on this signal reports *gaze transitions*, not gaze position: a
  look to one side and the return to centre produce two opposite pulses;
- the recorded "blinks" (16-27 per 30 s run) are mostly these transients (section 4.3).

The front-end corner frequency is not documented in the project sources - see the open
questions.

**1.3 There is no documented cue protocol.** The traces show a regular, repeated sequence
of gaze shifts, but the look sequence was not written down and no cue or label was logged
with the samples. Direction-detection accuracy therefore **cannot be computed from these
recordings**, and none is reported. `eog.metrics.direction_accuracy` is provided (and
tested on synthetic signals) for future cued recordings; a cued protocol is the first
recommendation in section 7.

---

## 2. Metric definitions

| Original name | Where | What it computes | Problem | Action |
|---|---|---|---|---|
| "Drift µV/min" (`drift_uv_min`, `drift_rate_uv_per_min`) | `test_01:83`, `test_07:195`, `eog_analysis:189` | (max − min of the *baseline* column) / duration × 60 | Unit is ADC counts, never µV. It measures how far the baseline estimate moved, so it is identically 0 for Tests 01-03 (fixed calibration baseline) and grows the more an adaptive baseline tracks. It cannot rank methods. | **Renamed** `baseline_excursion_counts_per_min` (same arithmetic). **Added** `residual_drift_counts_per_min`. |
| "Stability %" (`stab_pct`, `stability_pct`) | `test_07:198`, `eog_analysis:201` | % of samples labelled CENTER | Measures time at centre. A detector that never leaves CENTER scores 100 %. Depends on the protocol's duty cycle. | **Renamed** `centre_pct`. |
| "direction_stability_pct" | `test_01:72` | % of the last 100 samples whose label is CENTER or unchanged | A different quantity under the same name (reads 95-98 % for every run). | **Renamed** `non_transition_pct_last_100`. |
| "SNR dB" (`snr_db`) | `test_07:197`, `eog_analysis:177` | 20·log10(std(filtered) / std(raw − filtered)) | `raw − filtered` is everything the filter removed: DC offset, baseline, noise. It does not separate eye movement from noise; a filter that does nothing scores +∞. | **Renamed** `removed_component_ratio_db`. **Added** `peak_to_noise_db`. |
| "Noise floor" | `test_07:213` vs `eog_analysis:180` | `raw_std − filt_std` in the JSON, `std(raw − filtered)` in the analysis | Two definitions, neither a noise floor. | **Renamed** `removed_component_std` (analysis definition). |
| `sampling_rate_hz` / `hz` | `test_01:89`, `test_07:206`, `eog_analysis:137` | recorded samples / duration | Duration includes the 3 s calibration, samples do not: every JSON reports ~45.4 Hz for a stream that is 50.0 Hz by its own timestamps. | **Fixed**: (n − 1) / (t_last − t_first). |
| "vs literature" (`drift_imp_pct`, `stab_imp_pct`), `verdict` | `test_07:49-50,199-200,231`, `eog_analysis:341-354` | compares the two figures above with 200 "µV/min" and 85 % | Counts compared against a µV target; a centre-fraction compared against an accuracy figure from a different protocol. The recorded values (−1559 % to −7774 % "improvement") show the comparison is not meaningful. | **Removed** from new outputs. Recorded JSON files are left untouched. |
| `usability_score` | `test_01:97` | UNUSABLE/POOR from the drift figure | Drift figure is always 0 for Test 01, so this only depends on the direction-change count. | **Removed** from new outputs. |

### Units

Everything is computed in raw ADC counts. `eog/units.py` converts:

- counts → volts **at the ADC pin**: 14-bit ADC on the default 5 V reference (the EXG
  Pills are also powered from 5 V) ⇒ 0.305 mV per count. This is a nominal figure: the
  reference was not measured. The detector dead zone of 300 counts is therefore about
  92 mV at the pin. (The original visualiser labelled the same 300 as "µV".)
- counts → µV **at the electrodes** needs the total front-end gain, which is still
  unconfirmed. Electrode-referred microvolts cannot be stated until it is; there is
  deliberately no default, and `--gain` enables the conversion once the value is known.

### Added metrics (label-free, same definition for every method)

- `residual_drift_counts_per_min`: slope of a least-squares line through the 1-second
  medians of the detector input (filtered − baseline). Medians suppress the brief movement
  transients; what is left is the trend of the resting level the dead zone is compared with.
- `noise_sigma_counts`: 1.4826 · MAD(first difference) / √2 - the sample-to-sample noise.
- `peak_to_noise_db`: 20·log10(P95 |x − median| / noise sigma) - how large the movement
  excursions are relative to the sample noise of the same signal.

These are proxies. None of them replaces accuracy against a cued protocol.

---

## 3. Sampling rate and time base

**3.1 Hard-coded 50 Hz.** `SAMPLING_RATE = 50` (`test_02:30`, `test_03:29`) and every EMA
alpha, the 5-sample velocity window and the hold-frame counts assume 50 Hz. The canonical
firmware (v3) samples at 250 Hz. Run unchanged against it, every time constant would be 5×
shorter, the velocity window 16 ms instead of 80 ms, and the detector hold 32 ms instead of
160 ms - a different set of methods.
*Action:* the original constants are kept as the 50 Hz reference definition and converted
to the actual rate (`eog.filters.scale_alpha`, `scale_window`, `eog.detector.scale_hold_frames`).
At 50 Hz the conversion is the identity. `--no-rate-compensation` gives the literal
per-sample behaviour. The rate comes from the wire format (50 Hz for `H:..,V:..`, 250 Hz for
CSV lines) or `--fs`, and a mismatch with the measured rate is reported.
*Not validated on hardware:* no 250 Hz recording exists in the sources.

**3.2 Wall-clock timestamps.** All velocity and blink timing used `time.time()` at serial
arrival (`test_04:160` and equivalents). *Action:* the pipeline takes time from a sample
clock: firmware `micros()` timestamps when present (v3, rollover handled), otherwise sample
count / fs; `--clock host` restores the original behaviour. In the recorded 50 Hz runs the
host intervals are 19-21 ms, so jitter was small there - but see 4.3.

**3.3 Sampling at exactly the mains frequency.** The v1 firmware samples at 50 Hz with four
ADC reads spread over roughly 0.4 ms. In a 50 Hz mains country, mains pickup aliases to near
0 Hz and is indistinguishable from baseline wander. The 250 Hz firmware removes the alias
(mains then sits at 50 Hz, below Nyquist, where it can be notched). *Documented, not
changed.* How to check: record a few seconds at 250 Hz with electrodes attached and look at
the 50 Hz bin.

---

## 4. Filter correctness

**4.1 Tests 02/03 are first-order RC sections, labelled "Butterworth".** One stage (Test
02) is a first-order response - which is all a first-order Butterworth is, so the label is
only misleading if [3] used a higher order. Two identical cascaded stages
(Test 03, described as a "second-order Butterworth") are critically damped (Q = 0.5): 6 dB
down at the nominal 0.05 Hz, with the −3 dB point at 0.078 Hz. Unit tests measure both.
*Action:* renamed in code and docs; default unchanged. `--butterworth` selects true
2nd-order Butterworth sections (coefficients tested against `scipy.signal.butter`).
Whether [3] and [15] used first- or second-order filters needs checking against the papers.

**4.2 Low-pass cutoffs are lower than labelled.** With `alpha = dt / (RC + dt)` at 50 Hz the
nominal 15 Hz and 20 Hz low-passes have their −3 dB points at 9.4 Hz and 11.7 Hz (the
approximation degrades near Nyquist). `LPF_ALPHA = 0.3` is commented "~15Hz at 50Hz
sampling" (`test_04:32`); its −3 dB point is 2.9 Hz. `LPF_ALPHA = 0.2` in Test 07 is 1.8 Hz.
*Documented; values unchanged.* `eog.filters.ema_cutoff_hz` computes these.

**4.3 Test 05: why blink detection failed.** The detector accepts any excursion beyond 800
counts that falls below 400 within 100-400 ms. Nothing in that rule is specific to a blink,
and (1.2) every saccade produces exactly such a pulse - and on the horizontal channel too,
where blinks are weakest. The recordings count 17-27 "blinks" per 30 s Test 05 run, against
an instruction to blink 3-5 times; if that instruction was followed, at least four in five
detections were not blinks, consistent with the ">50 % false positive" conclusion in the
project report. Two further defects:

- *Timing sensitivity.* The duration limits (100, 400 ms) and the reset window (100-160 ms
  after a blink) are multiples of the 20 ms sample period, so whether a 5-sample pulse is
  "≥ 100 ms" was decided by sub-millisecond arrival jitter. Recomputed
  (`analysis/timing_sensitivity.py`, `docs/results/timing_sensitivity.csv`): adding 0-1 ms
  of jitter to the timestamps changes the blink count by up to 6 per run and the direction
  output on up to 27 % of samples, while Tests 01-04, 06 and 07 change on at most 2.0 %.
  The method's output was not reproducible run-to-run even on identical signal.
- `last_blink_time = 0` (`test_05:51`) only works because `time.time()` is large; with a
  clock starting at zero it opens a spurious reset window. *Fixed* (`None` until the first
  blink). `Resets` counts samples inside the reset window (about three per blink), not
  resets. *Renamed* `blink_reset_samples`.

This is kept as a negative result. A blink detector for this front end would need a feature
that separates blinks from saccades (vertical-channel dominance, symmetric bi-phasic shape,
or a dedicated electrode pair).

**4.4 Tests 06/07: the two smoothing coefficients were the wrong way round (fixed).** The
original smoother is
`y = a·x + (1 − a)·y`, in which a larger `a` means *less* smoothing. `FIXATION_SMOOTHING =
0.9  # Heavy smoothing during fixation` and `SACCADE_SMOOTHING = 0.1  # Light smoothing
during saccade` (`test_06:24-25`; `test_07:39-40` likewise) therefore did the opposite of the
method description: fixation samples pass through essentially unsmoothed (−3 dB at Nyquist)
and saccade samples are smoothed with a 0.19 s time constant.
*Action - fix adopted as the default.* Fixation samples now use the small coefficient
(heavy smoothing: 0.1 in Test 06, 0.15 in Test 07) and saccade samples the large one (light
smoothing: 0.9 / 0.85). `--original-smoothing` restores the as-recorded assignment;
`python -m eog verify` and the equivalence tests select it automatically, so the recordings
are still reproduced. A regression test asserts that fixation is smoothed more heavily than
saccades by default.

Before / after, recomputed by replay over the same 42 recordings (mean ± SD over 21
recordings per channel; `docs/results/replay_comparison_summary.csv`, variants
`original_smoothing` and `default`):

| Method, channel | Assignment | Noise sigma (counts) | Peak-to-noise (dB) | Direction changes | Centre % |
|---|---|---|---|---|---|
| 06 Velocity, H | original | 90.6 ± 14.6 | 31.8 ± 0.9 | 28.0 ± 5.5 | 38.1 ± 8.4 |
| 06 Velocity, H | **corrected** | 135.8 ± 45.6 | 31.7 ± 3.1 | 28.1 ± 5.8 | 36.6 ± 8.8 |
| 06 Velocity, V | original | 69.8 ± 14.1 | 30.0 ± 1.6 | 27.0 ± 3.0 | 40.5 ± 15.2 |
| 06 Velocity, V | **corrected** | 105.9 ± 23.8 | 29.7 ± 2.3 | 26.2 ± 3.7 | 41.1 ± 15.8 |
| 07 Combined, H | original | 70.9 ± 22.1 | 34.4 ± 2.2 | 28.8 ± 5.2 | 29.6 ± 8.7 |
| 07 Combined, H | **corrected** | 78.5 ± 27.2 | 34.8 ± 2.1 | 29.3 ± 5.6 | 30.8 ± 8.5 |
| 07 Combined, V | original | 50.3 ± 16.3 | 32.7 ± 1.7 | 27.2 ± 2.4 | 35.2 ± 12.3 |
| 07 Combined, V | **corrected** | 61.6 ± 15.2 | 32.6 ± 1.9 | 28.1 ± 2.4 | 35.9 ± 12.8 |

Reading this honestly: the fix makes the filter do what its description says, but on these
recordings it does not improve the numbers. Direction metrics and peak-to-noise are
unchanged within their spread, and the overall noise sigma is *higher* after the fix. The
reason is 4.5: about two-thirds to three-quarters of samples are classified SACCADE, so
after the fix the majority of samples receive the light smoothing, where before they
received the heavy one. The benefit the method is designed for - quiet fixations with
crisp saccades - depends on fixing the saccade classifier first.

**4.5 The saccade classifier is saturated.** Velocity is |x[n] − x[n−4]| / 80 ms on the raw
signal. The thresholds (2000 and 1500 counts/s) correspond to 160 and 120 counts across the
window, comparable to the sample noise. The recorded April-variant summaries classify 68-76 % of
samples as SACCADE, and replay of the current combined system gives the same fraction. The
"dual mode" is therefore mostly one mode. *Documented.* To validate a fix: compute velocity
on a lightly low-passed signal, or set the threshold from the measured noise (for example
5 × noise sigma / window), and check that the SACCADE fraction falls to the protocol's actual
movement duty cycle.

**4.6 Test 04: the velocity gate protects only the saccade itself.** Fast adaptation is
allowed when |deviation| > threshold and velocity is low. After a saccade ends, a held gaze
has exactly that signature, so on a DC-coupled signal the baseline would absorb a held gaze
within a few seconds (`test_held_gaze_is_eventually_absorbed`). With the AC-coupled front
end the hardware does this anyway. *Documented.* A candidate fix is to freeze adaptation for
a hold-off period after a detected saccade and release it on the return saccade; it needs a
DC-coupled or cued recording to evaluate.

**4.7 Smaller items.** The calibration "median" is `sorted[n // 2]` (upper-middle for even
n) - kept, tested, harmless. The detector's hold logic is correct; with `hold_frames = N`
the output switches on the N-th consecutive sample. Detector settings differ between tests
(dead zone 300/250, hold 5/8/6), which is one more confound in the original cross-test
comparison; they are preserved per method.

---

## 5. Experimental design

**Each method was tested on a different recording.** The seven tests of a session are seven
separate 30 s recordings, so differences between methods include differences in what the
participant did. The spread between runs of the same test (for example centre % SD of 30 for
Test 01 vertical) is larger than most differences between tests.

*Action:* `python -m eog compare` replays every method over the same recordings. Recomputed
over all 42 recordings (21 per channel), the methods are much closer than the per-session
tables suggest: direction-change counts overlap within one SD for every method, centre
fractions mostly do (Test 05 on the horizontal channel is the exception), and the clear
separation is in noise - the novel methods' detector input has a
peak-to-noise ratio about 4-7 dB above the unfiltered signal and about 1-5 dB above the two
literature filters (`docs/figures/replay_method_comparison.png`). With one participant, one
evening and no labels, this is a description of these recordings, not evidence of better
detection.

**BOTH mode.** Tests 02-06 ran H then V one after the other (`test_02:201`), not
simultaneously. *Fixed:* every method now processes both channels from each sample. In the
dual paths of Tests 01/07 the V calibrator was filled by hand without the duration or
minimum-sample checks (`test_01:203`, `test_07:359`). *Fixed:* each channel has its own
calibrator.

---

## 6. Software defects fixed

| Original location | Defect | Fix |
|---|---|---|
| `eog_analysis.py:311,323` | Summary file opened without an encoding and written with box-drawing characters: `UnicodeEncodeError` on Windows, so `results_summary.txt` was never completed there. | UTF-8 everywhere. |
| `eog_analysis.py:37,366` | Looked for CSVs only in the current directory, while results live in `Results_N` folders. | Recursive search, `--data`. |
| `eog_analysis.py:149` | Baseline column assumed to be `Baseline`; Test 04 writes `Adaptive_Baseline`, so Test 04 "drift" was reported as 0.0. | Both names handled (Test 04: 967 / 460 counts/min for H / V). |
| `eog_analysis.py:1-18` | Docstring promises figures; none are produced. | `--figures`. |
| `common.py:31` | Port keyword list includes "serial" and "usb": on Windows this matches "Standard Serial over Bluetooth link". | Match Arduino USB vendor ID first, skip Bluetooth ports, `--port`. |
| `common.py:53,62` | Library code calls `sys.exit`. | Raises `SerialError`. |
| `common.py:66-94` | A CSV-format line truncated at its start (`34,5678` left over from `1234,5678`) parses as a valid, wrong sample; no range check; failures are silent. | Strict parser, first line discarded, range check, format lock, rejected-line counts in the summary. |
| all `test_0x` read loops | A port error raises out of the loop and loses the summary; an empty stream spins forever. | Stream ends with a reason; files are closed and the summary written on Ctrl+C, port error or data timeout. |
| `test_02`-`test_06` | No JSON summary; sequential BOTH. | Uniform CSV + JSON for every method; simultaneous channels. |
| latest vs *April* | The latest copy dropped the 30 s auto-stop and the no-data warning that the recorded runs used. | Restored as `--duration` (default 30) and a generalised warning. |
| *April* `visualizer_ball.py:73,123,21` | Reader thread swallows every exception in a tight loop; calibration is fed the latest value once per video frame (duplicates/skips samples); thresholds labelled µV; end screen says "30 seconds recorded" though nothing is recorded. | Queue of validated samples, shared calibrator, counts, text removed. |

---

## 7. Suggested next steps (not implemented)

1. **Cued protocol with labels.** The existing recordings have no documented look
   sequence. Drive the sequence from the PC - timed on-screen targets (left / centre /
   right, up / centre / down) held for fixed intervals - and log the cue and its onset time
   with every sample. Then report accuracy, latency and confusion per method using
   `direction_accuracy`. This is the single change that would turn the comparison into
   evidence.
2. **Record once, compare by replay.** Record raw data only (Test 01) and evaluate every
   method offline, so all methods always see identical input.
3. **Confirm the front-end pass band** and decide whether the study is about drift removal
   (needs a DC-coupled signal) or transient classification (then the detector should work
   on pulses - sign and order - rather than levels).
4. **Re-tune at 250 Hz with firmware timestamps**, add a 50 Hz notch, and re-derive the
   velocity thresholds from measured noise.
5. **More than one participant and session**, with electrode placement recorded.
