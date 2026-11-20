/*
 * EOG Dual-Channel Reader — Arduino Uno R4
 * ==========================================
 * 2x BioAmp EXG Pill:
 *   A0 = Horizontal (bitemporal: outer left eye, outer right eye)
 *   A1 = Vertical   (above/below right eye)
 *   Reference: mastoid (behind ear)
 *
 * Output format: H:<value>,V:<value>\n
 * Baud: 115200
 * Sample rate: ~50 Hz (20ms interval)
 *
 * Jitter reduction:
 *   1. Dummy read before each channel (ADC mux settling)
 *   2. 4x oversampling averaged per channel
 *   3. Fixed-interval sampling with millis() to avoid drift
 *
 * Arduino Uno R4: 14-bit ADC (0-16383), 5V reference
 */

const int PIN_H = A0;  // Horizontal channel
const int PIN_V = A1;  // Vertical channel

const unsigned long SAMPLE_INTERVAL_MS = 20;  // 50 Hz
const int OVERSAMPLE = 4;  // Average 4 reads per channel

unsigned long lastSampleTime = 0;

void setup() {
  Serial.begin(115200);
  
  // Set ADC resolution to 14-bit (Uno R4 feature)
  analogReadResolution(14);
  
  // Let ADC and serial settle
  delay(100);
  
  // Warm-up reads to stabilise ADC
  for (int i = 0; i < 20; i++) {
    analogRead(PIN_H);
    analogRead(PIN_V);
    delay(5);
  }
  
  lastSampleTime = millis();
}

int readChannel(int pin) {
  // Dummy read for mux settling — critical when switching between channels
  analogRead(pin);
  delayMicroseconds(200);  // Allow sample-and-hold to settle
  
  // Oversample and average to reduce noise
  long sum = 0;
  for (int i = 0; i < OVERSAMPLE; i++) {
    sum += analogRead(pin);
    delayMicroseconds(100);
  }
  return (int)(sum / OVERSAMPLE);
}

void loop() {
  unsigned long now = millis();
  
  // Fixed-interval sampling — don't use delay(), use timing
  if (now - lastSampleTime < SAMPLE_INTERVAL_MS) {
    return;
  }
  lastSampleTime = now;
  
  // Read both channels with settling
  int h = readChannel(PIN_H);
  int v = readChannel(PIN_V);
  
  // Output in labelled format for both GUI and test scripts
  Serial.print("H:");
  Serial.print(h);
  Serial.print(",V:");
  Serial.println(v);
}
