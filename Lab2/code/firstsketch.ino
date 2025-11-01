#include <Arduino.h>

// ===== User Config =====
#define ADC_PIN 34        // Analog input from amplifier
#define ALERT_LED 2       // LED pin for "beam blocked"
#define FS 5000.0         // Sampling rate (Hz)
#define N_AVG 100         // RMS window size
#define THRESHOLD 0.05    // Relative RMS drop threshold

// ===== Filter Coefficients =====
// Bandpass: 2nd order, fs=5kHz, f0=425Hz, Q=5
// Designed via biquad formulas
float b0, b1, b2, a1, a2;

// Filter state
float x1=0, x2=0, y1=0, y2=0;

// Moving RMS buffer
float rmsBuffer[N_AVG];
int rmsIndex = 0;
float rmsSum = 0;

// Detection variables
unsigned long lastSampleMicros = 0;
bool beamBlocked = false;

void setup() {
  Serial.begin(115200);
  pinMode(ALERT_LED, OUTPUT);

  // Compute coefficients (precomputed values)
  // For stability: b0=0.073, b1=0, b2=-0.073, a1=-1.733, a2=0.855
  b0 = 0.073; b1 = 0.0; b2 = -0.073; a1 = -1.733; a2 = 0.855;

  // Clear RMS buffer
  for (int i=0; i<N_AVG; i++) rmsBuffer[i] = 0;
  Serial.println("Receiver filter initialized.");
}

void loop() {
  unsigned long now = micros();
  static const unsigned long samplePeriod = 1e6 / FS;

  if (now - lastSampleMicros >= samplePeriod) {
    lastSampleMicros = now;

    // === Sample input ===
    float vin = analogRead(ADC_PIN) / 4095.0;  // normalized 0–1

    // === Apply bandpass filter ===
    float vout = b0*vin + b1*x1 + b2*x2 - a1*y1 - a2*y2;
    x2 = x1; x1 = vin;
    y2 = y1; y1 = vout;

    // === Update RMS window ===
    float v2 = vout*vout;
    rmsSum -= rmsBuffer[rmsIndex];
    rmsBuffer[rmsIndex] = v2;
    rmsSum += v2;
    rmsIndex = (rmsIndex + 1) % N_AVG;
    float rms = sqrt(rmsSum / N_AVG);

    // === Beam detection ===
    static float baseline = 0;
    static int initCount = 0;

    // establish baseline RMS during first second
    if (initCount < FS) {
      baseline += rms;
      initCount++;
      if (initCount == FS) baseline /= FS;
    } else {
      if (rms < baseline * THRESHOLD) beamBlocked = true;
      else beamBlocked = false;
    }

    digitalWrite(ALERT_LED, beamBlocked ? HIGH : LOW);

    // Optional serial debug every ~100ms
    static unsigned long lastPrint = 0;
    if (millis() - lastPrint > 100) {
      lastPrint = millis();
      Serial.printf("RMS: %.4f  | Beam: %s\n", rms, beamBlocked ? "BLOCKED" : "CLEAR");
    }
  }
}