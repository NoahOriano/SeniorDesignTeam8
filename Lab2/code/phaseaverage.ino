#include <Arduino.h>

// ======= USER CONFIG =======
#define ADC_PIN 34         // Amplifier output pin
#define LED_PIN 2          // Alert LED output
#define FS 5000.0          // Sampling rate (Hz)
#define F_SIGNAL 425.6     // Expected transmitter frequency (Hz)
#define N_SETS 60          // Number of sets stored
#define THRESHOLD_RATIO 0.4 // Fraction of average deviation required for detection

// ======= DERIVED PARAMETERS =======
const int M_SAMPLES = (int)(FS / F_SIGNAL); // Samples per period
const unsigned long SAMPLE_PERIOD_US = (1e6 / FS);

float sets[N_SETS][M_SAMPLES];  // Circular buffer for samples
int currentSet = 0;             // Which set is being filled
int currentSample = 0;          // Sample index within current set
int setCycleCount = 0;          // Counts 1, 2, 3 (sample, sample, lapse)
bool beamBlocked = false;

// ======= TIMING =======
unsigned long lastSampleMicros = 0;
unsigned long lastComputeMillis = 0;

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  analogReadResolution(12); // ESP32 12-bit ADC
  Serial.printf("Starting receiver: Fs=%.1f Hz, Fsig=%.1f Hz, M=%d, N=%d\n",
                FS, F_SIGNAL, M_SAMPLES, N_SETS);
}

// === Helper: Compute mean and average deviation ===
void computeStats(float &mean, float &avgDev) {
  float sum = 0;
  int count = 0;
  for (int n = 0; n < N_SETS; n++) {
    for (int m = 0; m < M_SAMPLES; m++) {
      sum += sets[n][m];
      count++;
    }
  }
  mean = sum / count;

  float devSum = 0;
  for (int n = 0; n < N_SETS; n++)
    for (int m = 0; m < M_SAMPLES; m++)
      devSum += fabs(sets[n][m] - mean);

  avgDev = devSum / count;
}

// === Helper: compute average of Mth (last) samples across all sets ===
float computePhaseAverage(int phaseIndex) {
  float sum = 0;
  for (int n = 0; n < N_SETS; n++)
    sum += sets[n][phaseIndex];
  return sum / N_SETS;
}

// === Core processing ===
void processData() {
  float mean, avgDev;
  computeStats(mean, avgDev);

  // Evaluate coherence in last sample position (phase aligned)
  int phaseIndex = M_SAMPLES - 1;
  float phaseAvg = computePhaseAverage(phaseIndex);
  float deviation = fabs(phaseAvg - mean);

  // Decision with hysteresis
  static bool prevState = false;
  float threshold = avgDev * THRESHOLD_RATIO;
  bool signalPresent = deviation > threshold;

  beamBlocked = !signalPresent;

  digitalWrite(LED_PIN, beamBlocked ? HIGH : LOW);

  Serial.printf("[Process] Mean=%.3f Dev=%.3f PhaseAvg=%.3f Δ=%.3f -> %s\n",
                mean, avgDev, phaseAvg, deviation,
                beamBlocked ? "BLOCKED" : "CLEAR");

  prevState = beamBlocked;
}

// === Sampling state machine ===
void loop() {
  unsigned long now = micros();

  // Determine phase: 1=record set, 2=record set, 3=lapse (compute)
  int phase = (setCycleCount % 3);

  // === Sampling phase ===
  if (phase < 2) {  // Sample for two sets
    if (now - lastSampleMicros >= SAMPLE_PERIOD_US) {
      lastSampleMicros = now;
      float val = analogRead(ADC_PIN) / 4095.0; // normalize 0–1
      sets[currentSet][currentSample] = val;
      currentSample++;

      // Set complete?
      if (currentSample >= M_SAMPLES) {
        currentSample = 0;
        currentSet = (currentSet + 1) % N_SETS;
        setCycleCount++;
      }
    }
  }

  // === Lapse phase (no sampling, process) ===
  else if (phase == 2) {
    // Only process once per lapse
    if (millis() - lastComputeMillis > (1000.0 * M_SAMPLES / FS)) {
      lastComputeMillis = millis();
      processData();
      setCycleCount++;
    }
  }
}