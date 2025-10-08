#include <Arduino.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <TM1637Display.h>
#include <WiFi.h>            // native ESP32 Wi-Fi
#include <WebServer.h>       // >>> ADD: tiny HTTP server

// -------- ESP32 pinout (3.3V logic) --------
// DS18B20s need 4.7k pull-ups to 3V3 on DQ.
constexpr uint8_t PIN_BTN_S1     = 23; // Switch1
constexpr uint8_t PIN_BTN_S2     = 27; // Switch2 (safe on ESP32)
constexpr uint8_t PIN_OW_S1      = 18; // Sensor1 DQ
constexpr uint8_t PIN_TM1637_CLK = 21; // Display CLK
constexpr uint8_t PIN_TM1637_DIO = 22; // Display DIO
constexpr uint8_t PIN_OW_S2      = 19; // Sensor2 DQ

const char* WIFI_SSID = "NOAHORIANO9840";
const char* WIFI_PASS = "uu70778M";

// -------- Peripherals ----
OneWire ow1(PIN_OW_S1);
OneWire ow2(PIN_OW_S2);
DallasTemperature s1(&ow1);
DallasTemperature s2(&ow2);
TM1637Display display(PIN_TM1637_CLK, PIN_TM1637_DIO);

// >>> ADD: web server on port 80
WebServer server(80);

// -------- Sensor presence (addresses cached) --------
DeviceAddress a1, a2;
bool has1 = false, has2 = false;

// -------- Enable state (toggled by buttons) --------
bool en1 = false, en2 = false;

// -------- Debounce --------
struct DebouncedButton {
  uint8_t pin;
  bool lastStable;
  bool lastRead;
  unsigned long lastFlipMs;
  unsigned long settleMs;
  DebouncedButton(uint8_t p, unsigned long debounceMs = 20)
  : pin(p), lastStable(HIGH), lastRead(HIGH), lastFlipMs(0), settleMs(debounceMs) {}
};

DebouncedButton btnS1(PIN_BTN_S1);
DebouncedButton btnS2(PIN_BTN_S2);

bool updateDebounced(DebouncedButton &b) {
  bool raw = digitalRead(b.pin);
  if (raw != b.lastRead) { b.lastRead = raw; b.lastFlipMs = millis(); }
  if (millis() - b.lastFlipMs > b.settleMs) {
    if (b.lastStable != raw) { b.lastStable = raw; return true; }
  }
  return false;
}
inline bool fell(const DebouncedButton &b) { return b.lastStable == LOW; }

// --- Glyph helpers ---
static inline uint8_t glyphO(){ return (uint8_t)0x3F; } // O
static inline uint8_t glyphF(){ return (uint8_t)0x71; } // F
static inline uint8_t glyphS(){ return (uint8_t)0x6D; } // S
static inline uint8_t minus(){ return (uint8_t)0x40; }  // '-'

// --- Quick indicators ---
void showOFF() {
  uint8_t segs[4] = { glyphO(), glyphF(), glyphF(), 0x00 };
  display.setSegments(segs);
}
void flashS1(bool on) {
  uint8_t segs[4] = { glyphS(), display.encodeDigit(1), on ? 0x00 : minus(), 0x00 };
  display.setSegments(segs); delay(250);
}
void flashS2(bool on) {
  uint8_t segs[4] = { glyphS(), display.encodeDigit(2), on ? 0x00 : minus(), 0x00 };
  display.setSegments(segs); delay(250);
}

// --- Number rendering (Celsius only) ---
void showFloatC(TM1637Display &disp, float x) {
  if (isnan(x)) { uint8_t segs[4] = {0x79/*E*/,0x50/*r-ish*/,0x50,0x00}; disp.setSegments(segs); return; }
  bool oneDec = (x > -10.0f && x < 100.0f); // [-9.9, 99.9)
  uint8_t segs[4] = {0,0,0,0};
  if (oneDec) {
    int v10 = (int)roundf(x * 10.0f);
    bool neg = v10 < 0; if (neg) v10 = -v10;
    int d0 = v10 % 10, d1 = (v10/10) % 10, d2 = (v10/100) % 10, d3 = (v10/1000) % 10;
    segs[0] = d3 ? disp.encodeDigit(d3) : (neg ? minus() : 0);
    segs[1] = disp.encodeDigit(d2);
    segs[2] = disp.encodeDigit(d1) | 0x80; // decimal point
    segs[3] = disp.encodeDigit(d0);
  } else {
    int v = (int)roundf(x);
    bool neg = v < 0; if (neg) v = -v;
    int d0 = v % 10, d1 = (v/10) % 10, d2 = (v/100) % 10, d3 = (v/1000) % 10;
    segs[3] = disp.encodeDigit(d0);
    segs[2] = (v >= 10)  ? disp.encodeDigit(d1) : 0;
    segs[1] = (v >= 100) ? disp.encodeDigit(d2) : (neg ? minus() : 0);
    segs[0] = (v >= 1000)? disp.encodeDigit(d3) : 0;
  }
  disp.setSegments(segs);
}

// --- DS18B20 conversion cadence (parallel on two buses) ---
constexpr unsigned CONV_MS_12BIT = 750; // 12-bit worst-case
unsigned long convStartMs = 0;
bool convInFlight = false;

float lastC1 = NAN, lastC2 = NAN;

bool conversionsReady() { return (millis() - convStartMs) >= CONV_MS_12BIT; }

void startConversions() {
  // Only kick conversions for sensors that are present AND enabled
  if (has1 && en1) s1.requestTemperaturesByAddress(a1);
  if (has2 && en2) s2.requestTemperaturesByAddress(a2);
  convStartMs = millis();
  convInFlight = ( (has1 && en1) || (has2 && en2) );
}

void readTemperatures() {
  if (has1 && en1) {
    float c1 = s1.getTempC(a1);
    if (c1 == 85.0f || c1 <= DEVICE_DISCONNECTED_C || c1 < -55.0f || c1 > 125.0f) {
      lastC1 = NAN;
      // try a fresh conversion for this address
      s1.requestTemperaturesByAddress(a1);
      convStartMs = millis();
      convInFlight = true;
    } else {
      lastC1 = c1;
    }
  } else {
    // disabled or missing → no data
    lastC1 = NAN;
  }

  if (has2 && en2) {
    float c2 = s2.getTempC(a2);
    if (c2 == 85.0f || c2 <= DEVICE_DISCONNECTED_C || c2 < -55.0f || c2 > 125.0f) {
      lastC2 = NAN;
      s2.requestTemperaturesByAddress(a2);
      convStartMs = millis();
      convInFlight = true;
    } else {
      lastC2 = c2;
    }
  } else {
    lastC2 = NAN;
  }
}

// >>> FIX: only one setup() definition
void setup() {
  Serial.begin(115200);

  pinMode(PIN_BTN_S1, INPUT_PULLUP);
  pinMode(PIN_BTN_S2, INPUT_PULLUP);

  display.setBrightness(0x0F);
  display.clear();

  s1.begin();
  s2.begin();
  s1.setResolution(12);
  s2.setResolution(12);
  s1.setWaitForConversion(false);
  s2.setWaitForConversion(false);

  // Detect one device on each bus (index 0) & cache addresses
  has1 = s1.getAddress(a1, 0);
  has2 = s2.getAddress(a2, 0);

  // Start first conversion (on whatever is present)
  startConversions();

  // Initial screen
  showOFF();
  delay(300);

  // >>> Wi-Fi connect (native)
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(250);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\nWiFi connected, IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWiFi connect timed out (continuing without network).");
  }

  // >>> ADD: HTTP endpoint(s)
  server.on("/temp", [](){
    // Optional: CORS so a browser app on your laptop can fetch
    server.sendHeader("Access-Control-Allow-Origin", "*");
    server.sendHeader("Access-Control-Allow-Methods", "GET");
    // Build JSON with current readings and enabled flags
    // Note: JSON numbers or null when NaN
    String json = "{";
    json += "\"en1\":"; json += (en1 ? "true" : "false"); json += ",";
    json += "\"en2\":"; json += (en2 ? "true" : "false"); json += ",";
    json += "\"c1\":";  json += (isnan(lastC1) ? "null" : String(lastC1, 2)); json += ",";
    json += "\"c2\":";  json += (isnan(lastC2) ? "null" : String(lastC2, 2)); json += ",";
    // Show what you display when both enabled
    float shown = NAN;
    if (!en1 && !en2) shown = NAN;
    else if (en1 && !en2) shown = lastC1;
    else if (!en1 && en2) shown = lastC2;
    else {
      bool v1 = !isnan(lastC1), v2 = !isnan(lastC2);
      if (v1 && v2) shown = 0.5f*(lastC1 + lastC2);
      else if (v1)  shown = lastC1;
      else          shown = lastC2;
    }
    json += "\"shown\":"; json += (isnan(shown) ? "null" : String(shown, 2)); json += ",";
    json += "\"ip\":\"";  json += WiFi.localIP().toString(); json += "\"";
    json += "}";
    server.send(200, "application/json", json);
  });

  // Optional: simple root page to sanity-check from a browser
  server.on("/", [](){
    String page = "<!doctype html><meta charset='utf-8'><title>ESP32 Temps</title>"
                  "<pre>Try <a href=\"/temp\">/temp</a> for JSON.</pre>";
    server.send(200, "text/html", page);
  });

  server.begin(); // >>> start server
}

void loop() {
  // >>> handle HTTP requests
  server.handleClient();

  // Toggle S1
  if (updateDebounced(btnS1) && fell(btnS1)) {
    en1 = !en1;
    flashS1(en1);
  }
  // Toggle S2
  if (updateDebounced(btnS2) && fell(btnS2)) {
    en2 = !en2;
    flashS2(en2);
  }

  // Conversion cadence
  if (convInFlight && conversionsReady()) {
    readTemperatures();
    convInFlight = false;
  }
  if (!convInFlight) startConversions();

  // Decide what to display (Celsius only)
  if (!en1 && !en2) {
    showOFF();
    delay(50);
    return;
  }

  float shown = NAN;
  if (en1 && !en2) {
    shown = lastC1;                    // may be NAN -> Err
  } else if (!en1 && en2) {
    shown = lastC2;                    // may be NAN -> Err
  } else { // both enabled
    bool v1 = !isnan(lastC1);
    bool v2 = !isnan(lastC2);
    if (v1 && v2) shown = 0.5f*(lastC1 + lastC2);
    else if (v1)  shown = lastC1;
    else          shown = lastC2;      // may still be NAN -> Err
  }

  showFloatC(display, shown);
}
