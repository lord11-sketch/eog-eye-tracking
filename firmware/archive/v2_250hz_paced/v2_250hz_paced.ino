/*
 * EOG_acquisition.ino  (paced sampling, v2)
 *
 * Two-channel EOG streamer for the Arduino Uno R4 WiFi.
 *
 * Change from v1: the loop is paced to a fixed sample period using micros().
 * In v1 the loop ran flat-out at ~1 kHz, which produced large jitter because
 * the USB CDC buffer periodically stalled for ~9 ms while flushing. At a
 * fixed 250 Hz the USB stream has plenty of headroom and the inter-sample
 * interval becomes deterministic.
 *
 * Hardware:
 *   A0 <- BioAmp EXG Pill (horizontal) OUT
 *   A1 <- BioAmp EXG Pill (vertical)   OUT
 *   Both EXG Pill GNDs to Arduino GND. Shared mastoid REF electrode.
 *
 * Output: "<H>,<V>\n" per sample at 115200 baud, paced to SAMPLE_RATE_HZ.
 *
 * Author: Lord Monasterial
 */

#include <Arduino.h>
#include <WiFiS3.h>

// ---- Configuration -------------------------------------------------------

constexpr uint8_t  PIN_H          = A0;
constexpr uint8_t  PIN_V          = A1;
constexpr uint32_t SERIAL_BAUD    = 115200;
constexpr uint8_t  ADC_BITS       = 14;
constexpr uint8_t  LED_PIN        = LED_BUILTIN;

// Target sample rate. 250 Hz gives ~8x oversampling over the 0-30 Hz EOG
// bandwidth, plenty of headroom for the velocity classifier in Condition B
// (5-sample window = 20 ms), and USB CDC throughput is well within the
// host's comfortable read budget. Increase to 500 Hz only if you have a
// specific reason — at 250 Hz jitter should be well under 1 %.
constexpr uint32_t SAMPLE_RATE_HZ = 250;
constexpr uint32_t SAMPLE_PERIOD_US = 1000000UL / SAMPLE_RATE_HZ;  // 4000 us

// ---- State ---------------------------------------------------------------

uint32_t next_sample_us = 0;

// ---- Setup ---------------------------------------------------------------

void setup() {
  Serial.begin(SERIAL_BAUD);

  // CRITICAL: disable WiFi before any analogRead.
  WiFi.end();

  analogReadResolution(ADC_BITS);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  delay(200);

  // Prime the ADC. The first few reads after a resolution change can be
  // noisy on some boards; discard them.
  for (int i = 0; i < 8; i++) {
    analogRead(PIN_H);
    analogRead(PIN_V);
  }

  // Initialise the pacing reference to "now".
  next_sample_us = micros();
}

// ---- Main loop -----------------------------------------------------------
//
// The loop reads both channels, prints them, then sleeps until the next
// scheduled sample tick. micros() rollover (~70 minutes) is handled by the
// unsigned arithmetic: (next_sample_us - now) wraps correctly as long as
// the gap is less than 2^31 us, which it always is at 250 Hz.

void loop() {
  // Wait (busy-loop) until the next scheduled sample tick. Busy-wait is
  // appropriate here — the period is 4 ms, well below any sensible delay()
  // resolution, and there are no other tasks competing for CPU.
  uint32_t now = micros();
  while ((int32_t)(next_sample_us - now) > 0) {
    now = micros();
  }

  // Schedule the next tick BEFORE doing the work, so any small variation
  // in ADC + Serial time does not accumulate into drift.
  next_sample_us += SAMPLE_PERIOD_US;

  // If we have already fallen behind by more than one period (which would
  // only happen on a serious stall), resync rather than try to "catch up"
  // by firing samples back-to-back.
  if ((int32_t)(next_sample_us - now) < 0) {
    next_sample_us = now + SAMPLE_PERIOD_US;
  }

  // Read both channels.
  const int h = analogRead(PIN_H);
  const int v = analogRead(PIN_V);

  // Emit one CSV line.
  Serial.print(h);
  Serial.print(',');
  Serial.println(v);
}
