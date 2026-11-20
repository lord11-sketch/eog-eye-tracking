# Firmware

Two BioAmp EXG Pill outputs are sampled by an Arduino Uno R4 WiFi (Renesas RA4M1, 14-bit
ADC) and streamed as text over USB serial at 115200 baud.

| | v1 (archive) | v2 (archive) | **v3 (canonical)** |
|---|---|---|---|
| File | `archive/v1_50hz_labelled/` | `archive/v2_250hz_paced/` | `eog_acquisition/` |
| Dated | April | May | May |
| Board (from the source) | "Arduino Uno R4" | Uno R4 WiFi | Uno R4 WiFi |
| ADC | 14-bit, 4x oversample + dummy read per channel | 14-bit, single read | 14-bit, single read |
| Sample rate | 50 Hz (`millis()` interval, 20 ms) | 250 Hz (`micros()` paced, drift-free schedule) | 250 Hz (same pacing) |
| Wire format | `H:<h>,V:<v>` | `<h>,<v>` | `<h>,<v>,<micros>` |
| Timestamp | none (host clock) | none (host clock) | firmware `micros()` per sample |
| WiFi radio | untouched | `WiFi.end()` before sampling | `WiFi.end()` before sampling |
| Pins | A0 = horizontal, A1 = vertical | same | same |

Why v3 is canonical: it is the newest, it paces sampling on a drift-free `micros()` schedule
instead of a `millis()` interval, it samples above the 50 Hz mains frequency instead of
exactly at it, and it timestamps every sample on the microcontroller so that host/USB
buffering jitter no longer enters the velocity estimates. The Python package reads all three
formats and identifies the firmware from the first valid line.

Two things to know:

- **All data in `data/recorded` was captured with v1** (50 Hz, `H:..,V:..`). No v2/v3
  recording is included, so the 250 Hz path is exercised by unit tests only, not validated
  on hardware in this repository.
- The v1 sketch exists in the sources twice (commented and stripped); the logic is
  identical, so only the commented copy is archived. Sketch file names were changed to
  match their folders (an Arduino IDE requirement); code is unchanged.

## Build and upload

PlatformIO (configuration in `platformio.ini`, board `uno_r4_wifi`, platform `renesas-ra`):

```
pio run -d firmware              # build
pio run -d firmware -t upload    # build and flash
```

Arduino IDE: open `eog_acquisition/eog_acquisition.ino`, select *Arduino UNO R4 WiFi*
(board package "Arduino UNO R4 Boards"), upload.

Build status: all three sketches were compiled with PlatformIO Core 6.x and platform
`renesas-ra` 1.9.0 (v3: 46.7 kB flash, 4.9 kB RAM). Nothing was flashed or run on hardware
as part of preparing this repository.

## Wiring

| Signal | Arduino pin |
|---|---|
| EXG Pill (horizontal) OUT | A0 |
| EXG Pill (vertical) OUT | A1 |
| EXG Pill GND (both) | GND |
| EXG Pill VCC (both) | 5V |

Electrodes (from the v1 header comment): horizontal pair at the outer canthi of the left and
right eyes, vertical pair above and below the right eye, shared reference on the mastoid.
`<Add electrode type, skin preparation and a photo or diagram.>`
