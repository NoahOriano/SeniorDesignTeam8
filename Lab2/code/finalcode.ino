#include <Arduino.h>
#include <WiFi.h>
#include <ESP_Mail_Client.h>
#include <time.h>

// ======= USER CONFIG =======
#define ADC_PIN 34          // Amplifier output pin
#define LED_PIN 2           // Alert LED output (on when BLOCKED)
#define FS 5000.0           // Sampling rate (Hz)
#define F_SIGNAL 470        // Expected transmitter frequency (Hz)
#define N_SETS 20           // Number of sets stored
#define THRESHOLD_RATIO 0.25 // Scale for "unusually far from mean" test (in avgDev units)
#define constant_thresh_add 0.01 // Minimum voltage to create more stability in weird conditions w/ low signal

// --- Detection parameters ---
#define X_MIN_HITS 2      // Require at least this many samples in the last set to be "high"
#define COUNT_BOTH_SIDES 0  // 1 = count above or below; 0 = only above

// --- Blockage persistence parameters ---
#define Y_MAX_BLOCK_COUNT 7  // Number of consecutive blockage detections required
#define Y_DECAY_STEP 1       // Decrease per clear detection


// ======= DERIVED PARAMETERS =======
const int M_SAMPLES = (int)(FS / F_SIGNAL); // Samples per period
const unsigned long SAMPLE_PERIOD_US = (1e6 / FS);

float sets[N_SETS][M_SAMPLES];  // Circular buffer for samples
int currentSet = 0;
int currentSample = 0;
int setCycleCount = 0;

bool beamBlocked = false;
bool prevBeamBlocked = false;
int blockCounter = 0; // Hysteresis counter

// ==== TIMING ====
unsigned long lastSampleMicros = 0;
unsigned long lastComputeMillis = 0;


// ==== Wi-Fi ====
const char* ssid = "NOAHORIANO9840";
const char* password = "hotspotpassword";

// ==== Mail Config ====
SMTPSession smtp;
const char* smtpHost = "smtp.gmail.com";
const int smtpPort = 465;
const char* senderEmail = "senderemail@gmail.com";
const char* senderPassword = "password";
const char* recipientEmail = "recieveremail@gmail.com"; // or your regular email

// NTP time setup
const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = -21600; // CST
const int daylightOffset_sec = 3600;

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  analogReadResolution(12); // ESP32 12-bit ADC
  Serial.printf("Starting receiver: Fs=%.1f Hz, Fsig=%.1f Hz, M=%d, N=%d, Xmin=%d\n",
                FS, F_SIGNAL, M_SAMPLES, N_SETS, X_MIN_HITS);

  // Connect to Wi-Fi ===
  connectWiFi();

  // Configure NTP Time ===
  configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);

  // Print confirmation ===
  Serial.println("Receiver ready. Starting sampling/filtering loop...");
}

// === Initialize Wi-Fi and SMTP ===
void connectWiFi() {
  Serial.println("Connecting to Wi-Fi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  unsigned long startAttemptTime = millis();
  const unsigned long timeout = 20000; // 20 seconds

  while (WiFi.status() != WL_CONNECTED && millis() - startAttemptTime < timeout) {
    Serial.print(".");
    delay(500);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n✅ Wi-Fi connected!");
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n❌ Failed to connect to Wi-Fi.");
    Serial.print("Last WiFi status: ");
    Serial.println(WiFi.status());
  }
}

void sendAlertEmail(const char* messageBody) {
  smtp.debug(1);
  ESP_Mail_Session session;
  session.server.host_name = smtpHost;
  session.server.port = smtpPort;
  session.login.email = senderEmail;
  session.login.password = senderPassword;
  session.login.user_domain = "";

  SMTP_Message message;
  message.sender.name = "Beam Safety System";
  message.sender.email = senderEmail;
  message.subject = "Critical Safety Event";
  message.addRecipient("Alert", recipientEmail);

  message.text.content = messageBody;
  message.text.transfer_encoding = Content_Transfer_Encoding::enc_7bit;

  if (!smtp.connect(&session)) {
    Serial.println("SMTP connect failed.");
    return;
  }

  if (!MailClient.sendMail(&smtp, &message)) {
    Serial.printf("Error sending Email, %s\n", smtp.errorReason().c_str());
  } else {
    Serial.println("Alert email sent successfully!");
  }
  smtp.closeSession();
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
  mean = sum / (float)count;

  float devSum = 0;
  for (int n = 0; n < N_SETS; n++)
    for (int m = 0; m < M_SAMPLES; m++)
      devSum += fabsf(sets[n][m] - mean);

  avgDev = devSum / (float)count;
}

static inline int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

// Proccess data
void processData() {
  float mean = 0, avgDev = 0;
  computeStats(mean, avgDev);

  const float thrHigh = mean + THRESHOLD_RATIO * avgDev + constant_thresh_add;
  const float thrLow  = mean - THRESHOLD_RATIO * avgDev - constant_thresh_add;

  // Inspect the most recent completed set
  const int lastSet = (currentSet - 1 + N_SETS) % N_SETS;
  int countHigh = 0, countLow = 0;

  for (int m = 0; m < M_SAMPLES; m++) {
    float v = sets[lastSet][m];
    if (v > thrHigh) countHigh++;
    if (COUNT_BOTH_SIDES && (v < thrLow)) countLow++;
  }

  const bool signalPresent =
      (countHigh >= X_MIN_HITS) || (COUNT_BOTH_SIDES && (countLow >= X_MIN_HITS));
  const bool blockedNow = !signalPresent;

  // Hysteresis logic: latch at Y_MAX_BLOCK_COUNT, clear at 0
  bool prev = beamBlocked;

  if (!beamBlocked) {
    // Not currently blocked: count up toward latch or decay otherwise
    if (blockedNow) {
      blockCounter = clampi(blockCounter + 1, 0, Y_MAX_BLOCK_COUNT);
      if (blockCounter >= Y_MAX_BLOCK_COUNT) {
        beamBlocked = true;             // latch ON
        blockCounter = Y_MAX_BLOCK_COUNT;
      }
    } else {
      blockCounter = clampi(blockCounter - Y_DECAY_STEP, 0, Y_MAX_BLOCK_COUNT);
    }
  } else {
    // Currently blocked: only clear when counter returns to 0
    if (blockedNow) {
      blockCounter = Y_MAX_BLOCK_COUNT; // maintain saturation
    } else {
      blockCounter = clampi(blockCounter - Y_DECAY_STEP, 0, Y_MAX_BLOCK_COUNT);
      if (blockCounter == 0) beamBlocked = false;
    }
  }

  // LED output follows the latched state
  digitalWrite(LED_PIN, beamBlocked ? HIGH : LOW);

  // Email only on rising edge
  if (beamBlocked && !prev) {
    struct tm timeinfo;
    if (getLocalTime(&timeinfo)) {
      char msg[128];
      strftime(msg, sizeof(msg),
               "Critical Safety Event at %I:%M %p on\n%m/%d/%Y", &timeinfo);
      sendAlertEmail(msg);
    } else {
      sendAlertEmail("Critical Safety Event (time unavailable)");
    }
  }

  prevBeamBlocked = beamBlocked;
}


// === Sampling state machine ===
void loop() {
  unsigned long now = micros();
  int phase = (setCycleCount % 3);

  // === Sampling phase (two sets) ===
  if (phase < 2) {
    if (now - lastSampleMicros >= SAMPLE_PERIOD_US) {
      lastSampleMicros = now;
      float val = analogRead(ADC_PIN) / 4095.0f;
      sets[currentSet][currentSample] = val;
      currentSample++;

      if (currentSample >= M_SAMPLES) {
        currentSample = 0;
        currentSet = (currentSet + 1) % N_SETS;
        setCycleCount++;
      }
    }
  }

  // === Lapse phase (no sampling; process) ===
  else {
    if (millis() - lastComputeMillis > (1000.0 * M_SAMPLES / FS)) {
      lastComputeMillis = millis();
      processData();
      setCycleCount++;
    }
  }
}