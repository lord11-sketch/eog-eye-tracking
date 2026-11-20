/*
 * EOG_acquisition.ino  (paced sampling + Arduino timestamps, v3)
 *
 * Two-channel EOG streamer for the Arduino Uno R4 WiFi.
 *
 * Changes from v2:
 *   - Each output line now includes the Arduino's micros() timestamp.
 *     The Python side uses this as the authoritative sample time instead
 *     of the host's wall clock, which eliminates jitter introduced by
 *     bursty USB CDC delivery.
 *   - Wire format is now "<H>,<V>,<MICROS>\n" — three integers per line.
 *
 * Hardware:
 *   A0 <- BioAmp EXG Pill (horizontal) OUT
 *   A1 <- BioAmp EXG Pill (vertical)   OUT
 *   Both EXG Pill GNDs to Arduino GND. Shared mastoid REF electrode.
 *
 * Output: "<H>,<V>,<micros>\n" per sample at 115200 baud, paced to
 * SAMPLE_RATE_HZ. The micros() value is unsigned 32-bit and rolls over
 * every ~71 minutes; the host-side parser handles rollover.
 *
 * Author: Lord Monasterial
 */

#include <Arduino.h>
#include <WiFiS3.h>

// ---- Configuration -------------------------------------------------------

constexpr uint8_t  PIN_H            = A0;
constexpr uint8_t  PIN_V            = A1;
constexpr uint32_t SERIAL_BAUD      = 115200;
constexpr uint8_t  ADC_BITS         = 14;
constexpr uint8_t  LED_PIN          = LED_BUILTIN;

// Target sample rate. 250 Hz at 14-bit resolution gives ~8x oversampling
// over the EOG bandwidth (0-30 Hz) with comfortable USB headroom.
constexpr uint32_t SAMPLE_RATE_HZ   = 250;
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

  next_sample_us = micros();
}

// ---- Main loop -----------------------------------------------------------
//
// Read both channels, capture the Arduino timestamp, print "H,V,micros".
// The loop is paced to SAMPLE_PERIOD_US using a busy-wait on micros().
// next_sample_us is advanced unconditionally so that any small variation
// in ADC + Serial time does not accumulate into drift. If we ever fall
// behind by more than one period, we resync rather than try to catch up.

void loop() {
  uint32_t now = micros();
  while ((int32_t)(next_sample_us - now) > 0) {
    now = micros();
  }

  next_sample_us += SAMPLE_PERIOD_US;
  if ((int32_t)(next_sample_us - now) < 0) {
    next_sample_us = now + SAMPLE_PERIOD_US;
  }

  // Read both channels.
  const int h = analogRead(PIN_H);
  const int v = analogRead(PIN_V);

  // Capture the sample timestamp from the Arduino's monotonic clock.
  // This is the authoritative sample time; the host uses it directly.
  const uint32_t t_us = micros();

  // Emit one CSV line: "<H>,<V>,<MICROS>\n".
  Serial.print(h);
  Serial.print(',');
  Serial.print(v);
  Serial.print(',');
  Serial.println(t_us);
}
