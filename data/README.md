# Recorded data

`recorded/` holds six sessions captured on 10-11 April 2026 with the v1 firmware
(50 Hz, `H:<h>,V:<v>`) and the April version of the test scripts. Files are unmodified.

| Folder | Eye-movement axis tested | Raw column |
|---|---|---|
| `Results_1_Horizontal` ... `Results_3_Horizontal` | left / right | `Raw_H` (input A0) |
| `Results_4_Vertical` ... `Results_6_Vertical` | up / down | `Raw_V` (input A1) |

Each session contains one recording per test (seven separate ~30 s recordings, made one
after another) plus JSON summaries for Tests 01 and 07:

```
test_01_unfiltered_<CH>_<YYYYMMDD_HHMMSS>.csv      test_01_summary_<stamp>.json
test_02_butterworth_<CH>_<stamp>.csv
test_03_low_cutoff_<CH>_<stamp>.csv
test_04_adaptive_<CH>_<stamp>.csv
test_05_blink_reset_<CH>_<stamp>.csv
test_06_velocity_<CH>_<stamp>.csv
test_07_full_<CH>_<stamp>.csv                      test_07_summary_<CH>_<stamp>.json
```

## Conditions

- One evening of recording. `<Add: number of participants and description as permitted by
  your ethics approval, electrode type and placement, lighting, viewing distance.>`
- Each recording starts after a 3 s calibration that was **not** written to the file, so
  `Time_ms` begins at about 3020-3040 ms. Recordings auto-stopped after 30 s (the first
  session has files a few seconds longer).
- Sample interval 20 ms (19-21 ms as timestamped by the host); 1498-1974 rows per file.
- **Protocol: not documented.** The look sequence used in these sessions was not written
  down, and no cue or label was logged with the samples. The traces show a regular,
  repeated sequence of gaze shifts, but the files carry no ground truth, so **direction
  accuracy cannot be computed from them**. Future recordings should use a cued protocol
  (timed on-screen targets with logged cue times).
- **Test 07.** The project's Test 07 is the combined system (Tests 04 + 06). It has no
  recording of its own: its results are computed by offline replay over the `Raw_<CH>`
  columns of these sessions (`python -m eog compare`). The `test_07_full_*` files here are
  a historical variant, the April "full system" (Tests 04 + 05 + 06, i.e. with the blink
  reset), kept unmodified and used only to verify the replay code.
- Tests 06 and 07 were recorded with the original dual-mode coefficient assignment, which
  has since been corrected (`docs/methods-review.md` section 4.4). `python -m eog verify`
  uses the original assignment automatically when checking these files.

## Columns

All amplitudes are raw 14-bit ADC counts (0-16383; nominally 0.305 mV per count at the ADC
pin on the default 5 V reference). Electrode-referred microvolts cannot be stated until the
EXG Pill gain is confirmed. `<CH>` is `H` or `V`.

Common to every file:

| Column | Meaning |
|---|---|
| `Time_ms` | Host time since the script started reading, ms (integer) |
| `Raw_<CH>` | Raw ADC value |
| `Direction` | Detector output: `LEFT`/`CENTER`/`RIGHT` (H) or `UP`/`CENTER`/`DOWN` (V) |

Per test:

| Test | Extra columns |
|---|---|
| 01 | `Baseline` (calibration median, constant), `Drift_Rate` (always 0 - see the methods review), `Stability` (% of the last 100 samples that were CENTER or unchanged) |
| 02 | `Filtered_<CH>`, `Baseline` (calibration median of the filter output, constant), `Drift` (= filtered - baseline), `HPF_out`, `LPF_out` |
| 03 | `Filtered_<CH>`, `Baseline`, `Drift` (as Test 02) |
| 04 | `Filtered_<CH>`, `Adaptive_Baseline` (raw units), `Drift` (= raw - baseline), `Velocity` (counts/s), `Adapt_Events` (cumulative fast-adaptation samples) |
| 05 | `Filtered_<CH>`, `Baseline` (raw units), `Blink_Status` (`START`/`VALID`/`INVALID`/`-`), `Blink_Count`, `Resets` (cumulative samples in the reset window) |
| 06 | `Filtered_<CH>`, `Baseline`, `Velocity`, `Mode` (`SACCADE`/`FIXATION`) |
| 07 (April variant, 04 + 05 + 06) | `Filtered_<CH>`, `Baseline`, `Velocity`, `Mode`, `Blinks`, `Resets`, `Drift` and `SNR` (running values of the legacy metrics) |

The JSON summaries use the original metric names (`drift_uv_min`, `stab_pct`, `snr_db`,
`verdict`, ...). Those names overstate what was measured; `docs/methods-review.md` section 2
maps each one to its definition. The reported `hz` / `sampling_rate_hz` of about 45.4 is an
artefact (calibration time included in the denominator); the stream is 50 Hz.

Files written by the current package use one uniform layout for every method - see
`docs/data-format.md`.

## Privacy

The files contain timestamps and ADC values only: no names, paths or identifiers.
