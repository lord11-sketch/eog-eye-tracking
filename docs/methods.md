# Methods

Each method is a pure per-sample class in `eog/filters.py`; its parameters and detector
settings are registered in `eog/methods.py`. Bracketed numbers are citations from the
project report (see the references section of the README).

Notation: `x[n]` raw ADC sample, `fs` sample rate, `dt = 1/fs`. An EMA with coefficient `a`
is `y[n] = a*x[n] + (1-a)*y[n-1]` (larger `a` = less smoothing). Constants are quoted as
tuned at 50 Hz; at other rates they are converted to keep the same time behaviour
(`--no-rate-compensation` disables that).

## Common stages

**Calibration.** The first 3 s (and at least 30 samples) are not recorded. For Tests 01-03
the median of the method output over that window becomes a fixed baseline. For Tests 04-07
the method tracks its own baseline and the window only lets it settle.

**Direction detector** (`eog/detector.py`). `d = value - baseline`, negated when `invert`
(default). `d < -dead_zone` gives LEFT/UP, `d > dead_zone` gives RIGHT/DOWN, otherwise
CENTER. The output changes only after the new state has been seen on `hold_frames`
consecutive samples.

**Velocity** (Tests 04, 06, 07). `v = |x[n] - x[n-4]| / (t[n] - t[n-4])` in counts/s, on
the raw signal (5-sample window = 80 ms at 50 Hz).

## The seven tests

| # | Key | Method | Dead zone / hold |
|---|---|---|---|
| 01 | `unfiltered` | none; raw compared with the calibration median | 300 / 5 |
| 02 | `rc_bandpass` | first-order high-pass 0.1 Hz + first-order low-pass 15 Hz [3] | 300 / 8 |
| 03 | `soltani` | two cascaded first-order high-pass sections at 0.05 Hz [15] + first-order low-pass 20 Hz [9] | 300 / 8 |
| 04 | `adaptive` | EMA baseline with velocity-gated fast adaptation (novel) | 300 / 8 |
| 05 | `blink_reset` | blink-locked baseline reset (novel; negative result) | 300 / 8 |
| 06 | `velocity` | velocity-domain dual-mode smoothing (novel) | 300 / 8 |
| 07 | `combined` | Test 04 baseline + Test 06 smoothing + EMA low-pass (novel); results by offline replay | 250 / 6 |

### 02 / 03 - fixed filters from the literature

```
RC = 1 / (2*pi*fc)
high-pass section:  y[n] = a * (y[n-1] + x[n] - x[n-1]),   a = RC / (RC + dt)
low-pass:           y[n] = b * x[n] + (1 - b) * y[n-1],     b = dt / (RC + dt)
```

The first output is 0 and the first sample initialises the high-pass memory, so the DC
offset produces no start-up step. These are first-order RC sections, not Butterworth
designs; at 50 Hz the low-pass -3 dB points are 9.4 Hz (nominal 15) and 11.7 Hz (nominal
20), and the two-section cascade of Test 03 is 6 dB down at 0.05 Hz. `--butterworth`
substitutes true 2nd-order Butterworth high- and low-pass sections.

### 04 - adaptive baseline with velocity gate

```
dev = |x - B|
a   = 0.01  if dev > 100 and v < 800      (large, slow deviation: treat as drift)
      0.001 otherwise                      (time constants 2 s and 20 s at 50 Hz)
B  <- a*x + (1-a)*B
out = EMA(x - B, 0.3)                      (-3 dB at 2.9 Hz)
```

### 05 - blink-locked baseline reset (negative result)

A "blink" is an excursion `|x - B| > 800` that falls below 400 after 100-400 ms. For 200 ms
after a valid blink the baseline EMA coefficient rises from 0.01 to 0.3, and between 100 and
160 ms it is additionally blended 30 % towards the current sample. Output is
`EMA(x - B, 0.3)`.

Why it failed is analysed in `methods-review.md` section 4.3: the rule accepts saccade
transients as blinks, and its decisions depend on sub-millisecond timestamp jitter.

### 06 - velocity-domain dual-mode smoothing

```
B   <- 0.01*x + 0.99*B
mode = SACCADE if v > 2000 else FIXATION
out <- a*(x - B) + (1-a)*out,   a = 0.1 in FIXATION (heavy smoothing, time constant 0.19 s)
                                a = 0.9 in SACCADE  (light smoothing, follows the input)
```

The original scripts had the two coefficients the other way round (saccades smoothed
heavily, fixation hardly at all). The assignment above is the corrected default;
`--original-smoothing` reproduces the as-recorded behaviour (`methods-review.md` section
4.4, with before/after numbers).

### 07 - combined system

Test 04 baseline (fast coefficient 0.05, deviation threshold 80, gate 800), then Test 06
smoothing (threshold 1500; coefficient 0.15 in fixation, 0.85 in saccade - corrected
assignment, as in Test 06), then a final EMA with coefficient 0.2 (-3 dB at 1.8 Hz).

There is no live recording of this system: every Test 07 result in this repository is
computed by offline replay over the raw data of the recorded sessions.

**Historical variant.** `full_system_april` is the earlier "full system" (Tests 04 + 05 +
06: it also ran the blink reset, with the original smoothing assignment). The recorded
`test_07_full_*` files were produced by it. It is kept so those files can be verified and
is not part of the comparison.

## Running

```
python -m eog methods
python -m eog live    --method combined --channel H --port COM5        # hardware
python -m eog replay  --input <run.csv> --method combined              # no hardware
python -m eog compare --input data/recorded                            # all methods, same input
```
