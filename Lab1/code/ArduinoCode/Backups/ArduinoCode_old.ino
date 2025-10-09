#include <Arduino.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <TM1637Display.h>
#include <SoftwareSerial.h>

// ===================== USER CONFIG =====================
#define HOTSPOT_SSID  "NOAHORIANO 9840"
#define HOTSPOT_PASS  "^u70778M"

#define AP_SSID       "ThirdBoxWifi"
#define AP_PASS       "12345678"

#define TCP_PORT      5000

// ===================== Pinout ==========================
constexpr uint8_t PIN_BTN_S1     = 2;  // Button toggles Sensor 1 (active-LOW)
constexpr uint8_t PIN_BTN_S2     = 3;  // Button toggles Sensor 2 (active-LOW)
constexpr uint8_t PIN_OW_S1      = 4;  // DS18B20 #1 (4.7k pull-up to 5V)
constexpr uint8_t PIN_TM1637_CLK = 5;
constexpr uint8_t PIN_TM1637_DIO = 6;
constexpr uint8_t PIN_OW_S2      = 7;  // DS18B20 #2 (4.7k pull-up to 5V)

// UART to ESP32-AT (3.3V logic!)
constexpr uint8_t PIN_ESP_RX = 8;   // Arduino RX  <- ESP32 TX0
constexpr uint8_t PIN_ESP_TX = 9;   // Arduino TX  -> ESP32 RX0
SoftwareSerial esp(PIN_ESP_RX, PIN_ESP_TX); // RX, TX

// ===================== Peripherals =====================
OneWire ow1(PIN_OW_S1);
OneWire ow2(PIN_OW_S2);
DallasTemperature s1(&ow1);
DallasTemperature s2(&ow2);
TM1637Display display(PIN_TM1637_CLK, PIN_TM1637_DIO);

// ===================== Sensor presence =================
DeviceAddress a1, a2;
bool has1 = false, has2 = false;

// ===================== Enable state ====================
bool en1 = false, en2 = false;
bool systemOn = true;

// ===================== Debounce ========================
struct DebouncedButton {
  uint8_t pin;
  bool lastStable;
  bool lastRead;
  unsigned long lastFlipMs;
  unsigned long settleMs;
  DebouncedButton(uint8_t p, unsigned long d=20)
  : pin(p), lastStable(HIGH), lastRead(HIGH), lastFlipMs(0), settleMs(d) {}
};
DebouncedButton btnS1(PIN_BTN_S1), btnS2(PIN_BTN_S2);

bool updateDebounced(DebouncedButton &b) {
  bool raw = digitalRead(b.pin);
  if (raw != b.lastRead) { b.lastRead = raw; b.lastFlipMs = millis(); }
  if (millis() - b.lastFlipMs > b.settleMs) {
    if (b.lastStable != raw) { b.lastStable = raw; return true; }
  }
  return false;
}
inline bool fell(const DebouncedButton &b) { return b.lastStable == LOW; }

// ===================== Display glyphs ==================
static inline uint8_t glyphO(){ return (uint8_t)0x3F; } // O
static inline uint8_t glyphF(){ return (uint8_t)0x71; } // F
static inline uint8_t glyphS(){ return (uint8_t)0x6D; } // S
static inline uint8_t minus(){ return (uint8_t)0x40; }  // '-'

void showOFF() {
  uint8_t segs[4] = { glyphO(), glyphF(), glyphF(), 0x00 };
  display.setSegments(segs);
}
void flashS(bool s1, bool on) {
  uint8_t segs[4] = { glyphS(), display.encodeDigit(s1?1:2), on ? 0x00 : minus(), 0x00 };
  display.setSegments(segs); delay(220);
}
void showFloatC(TM1637Display &disp, float x) {
  if (isnan(x)) { uint8_t segs[4]={0x79,0x50,0x50,0x00}; disp.setSegments(segs); return; } // "Err"
  bool oneDec = (x > -10.0f && x < 100.0f);
  uint8_t segs[4]={0,0,0,0};
  if (oneDec) {
    int v10 = (int)roundf(x * 10.0f);
    bool neg = v10 < 0; if (neg) v10 = -v10;
    int d0=v10%10, d1=(v10/10)%10, d2=(v10/100)%10, d3=(v10/1000)%10;
    segs[0] = d3 ? disp.encodeDigit(d3) : (neg ? minus() : 0);
    segs[1] = disp.encodeDigit(d2);
    segs[2] = disp.encodeDigit(d1) | 0x80; // decimal point
    segs[3] = disp.encodeDigit(d0);
  } else {
    int v = (int)roundf(x);
    bool neg = v < 0; if (neg) v = -v;
    int d0=v%10, d1=(v/10)%10, d2=(v/100)%10, d3=(v/1000)%10;
    segs[3] = disp.encodeDigit(d0);
    segs[2] = (v>=10)? disp.encodeDigit(d1):0;
    segs[1] = (v>=100)?disp.encodeDigit(d2):(neg?minus():0);
    segs[0] = (v>=1000)?disp.encodeDigit(d3):0;
  }
  disp.setSegments(segs);
}

// ===================== DS18B20 cadence =================
constexpr unsigned CONV_MS_12BIT = 750;
unsigned long convStartMs = 0;
bool convInFlight = false;
float lastC1 = NAN, lastC2 = NAN;

void startConversions() {
  if (has1) s1.requestTemperatures();
  if (has2) s2.requestTemperatures();
  convStartMs = millis();
  convInFlight = true;
}
bool conversionsReady() { return (millis() - convStartMs) >= CONV_MS_12BIT; }
void readTemperatures() {
  if (has1) {
    float c1 = s1.getTempC(a1);
    lastC1 = (c1 <= DEVICE_DISCONNECTED_C) ? NAN : c1;
  } else lastC1 = NAN;
  if (has2) {
    float c2 = s2.getTempC(a2);
    lastC2 = (c2 <= DEVICE_DISCONNECTED_C) ? NAN : c2;
  } else lastC2 = NAN;
}

// ===================== ESP-AT helpers ==================
String espBuf;
uint8_t activeMask = 0; // link bitmask 0..4

void espFlushInput(){ while(esp.available()) esp.read(); }
bool espWaitFor(const char* token, uint32_t timeoutMs=6000) {
  uint32_t t0 = millis(); String acc;
  while (millis()-t0 < timeoutMs) {
    while (esp.available()) {
      char c = (char)esp.read();
      acc += c;
      // track connects/closes
      if (acc.endsWith("CONNECT\r\n")) {
        int comma = acc.lastIndexOf(','); if (comma>=1) { int id = acc.substring(comma-1,comma).toInt(); if(id>=0&&id<=4) activeMask |= (1<<id); }
      } else if (acc.endsWith("CLOSED\r\n")) {
        int comma = acc.lastIndexOf(','); if (comma>=1) { int id = acc.substring(comma-1,comma).toInt(); if(id>=0&&id<=4) activeMask &= ~(1<<id); }
      }
      // accumulate for +IPD parsing
      if (acc.indexOf("+IPD,")>=0) { espBuf += c; if (espBuf.length()>512) espBuf.remove(0, espBuf.length()-512); }
      if (acc.endsWith(token)) return true;
    }
  }
  return false;
}
bool espCmd(const char* cmd, const char* expect="OK", uint32_t to=6000) {
  esp.print(cmd); esp.print("\r\n");
  return espWaitFor(expect, to);
}

// Try STA (join laptop hotspot); return true if joined
bool espTrySTA() {
  if (!espCmd("AT")) return false;
  espCmd("ATE0");
  espCmd("AT+CWMODE=1"); // STA
  // store config
  espCmd("AT+SYSSTORE=1");
  // join hotspot
  String join = String("AT+CWJAP=\"") + HOTSPOT_SSID + "\",\"" + HOTSPOT_PASS + "\"";
  // Many builds print WIFI CONNECTED / GOT IP; also await OK as fallback
  if (!espCmd(join.c_str(), "WIFI GOT IP", 12000)) {
    // tolerate variants
    if (!espWaitFor("OK", 2000)) return false;
  }
  // TCP server
  espCmd("AT+CIPMUX=1");
  {
  String cmd = String("AT+CIPSERVER=1,") + TCP_PORT;
  espCmd(cmd.c_str());
}
  return true;
}

// Start AP fallback (no dependency on campus wifi)
bool espStartAP() {
  // AP only
  espCmd("AT+CWMODE=2");
  // SSID,PASS,channel(6),auth(3=WPA2-PSK),maxconn(4),hidden(0)
  String sap = String("AT+CWSAP=\"") + AP_SSID + "\",\"" + AP_PASS + "\",6,3,4,0";
  if (!espCmd(sap.c_str())) return false;
  espCmd("AT+CIPMUX=1");
  {
  String cmd = String("AT+CIPSERVER=1,") + TCP_PORT;
  espCmd(cmd.c_str());
}
  return true;
}

#define _STR(x) #x
#define STRINGIFY(x) _STR(x)

// send JSON line to a link id
bool espSendTo(uint8_t linkId, const String& line) {
  String hdr = String("AT+CIPSEND=") + linkId + "," + line.length();
  if (!espCmd(hdr.c_str(), ">")) return false;
  esp.print(line);
  return espWaitFor("SEND OK", 1000);
}
// broadcast to all
void espBroadcast(const String& line) {
  for (uint8_t i=0;i<5;i++) if (activeMask & (1<<i)) espSendTo(i, line);
}

// parse +IPD payloads for single-line JSON commands
void espPollForCommands() {
  while (true) {
    int i = espBuf.indexOf("+IPD,"); if (i<0) break;
    int j = espBuf.indexOf(':', i);  if (j<0) break;
    int nl = espBuf.indexOf('\n', j+1); if (nl<0) break;
    String payload = espBuf.substring(j+1, nl); espBuf.remove(0, nl+1);
    payload.trim();
    if (payload.startsWith("{") && payload.endsWith("}")) {
      String low = payload; low.toLowerCase();
      if (low.indexOf("\"command\"")>=0 && low.indexOf("set_sensor")>=0) {
        bool isS1 = (low.indexOf("\"sensor\":\"s1\"")>=0);
        bool isS2 = (low.indexOf("\"sensor\":\"s2\"")>=0);
        bool on   = (low.indexOf("\"state\":\"on\"")>=0);
        bool off  = (low.indexOf("\"state\":\"off\"")>=0);
        if (isS1 && (on||off)) { en1 = on; flashS(true,  en1); }
        if (isS2 && (on||off)) { en2 = on; flashS(false, en2); }
      }
    }
  }
}

// ===================== Setup/Loop ======================
unsigned long lastTick = 0;

void setup() {
  pinMode(PIN_BTN_S1, INPUT_PULLUP);
  pinMode(PIN_BTN_S2, INPUT_PULLUP);

  display.setBrightness(0x0F);
  display.clear();
  showOFF();

  s1.begin(); s2.begin();
  s1.setResolution(12); s2.setResolution(12);
  s1.setWaitForConversion(false); s2.setWaitForConversion(false);

  has1 = s1.getAddress(a1, 0);
  has2 = s2.getAddress(a2, 0);
  startConversions();

  esp.begin(115200);
  delay(400);

  // 1) Try STA (join laptop hotspot). 2) Else AP fallback.
  bool sta = espTrySTA();
  if (!sta) {
    // clear any partial state and go AP
    espCmd("AT+RESTORE", "ready", 3000); // ignore failure
    delay(500);
    esp.begin(115200);
    espStartAP();
  }
}

void loop() {
  // Buttons
  if (updateDebounced(btnS1) && fell(btnS1)) { en1 = !en1; flashS(true,  en1); }
  if (updateDebounced(btnS2) && fell(btnS2)) { en2 = !en2; flashS(false, en2); }

  // Temperature pipeline
  if (convInFlight && conversionsReady()) { readTemperatures(); convInFlight = false; }
  if (!convInFlight) startConversions();

  // Local display (Celsius only)
  if (!en1 && !en2) {
    showOFF();
  } else {
    float shown = NAN;
    if (en1 && !en2) shown = lastC1;
    else if (!en1 && en2) shown = lastC2;
    else {
      bool v1 = !isnan(lastC1), v2 = !isnan(lastC2);
      if (v1 && v2) shown = 0.5f*(lastC1 + lastC2);
      else if (v1)  shown = lastC1;
      else          shown = lastC2;
    }
    showFloatC(display, shown);
  }

  // ESP RX -> buffer
  while (esp.available()) {
    char c = (char)esp.read();
    espBuf += c;
    if (espBuf.length()>512) espBuf.remove(0, espBuf.length()-512);
  }
  espPollForCommands();

  // 1 Hz JSON stream to all clients (omit NaNs to signal "missing data")
  unsigned long now = millis();
  if (systemOn && (now - lastTick >= 1000)) {
    lastTick = now;
    if (!isnan(lastC1)) {
      String j1 = String("{\"t_c\":") + String(lastC1,2) + ",\"sensor\":\"S1\"}\n";
      espBroadcast(j1);
    }
    if (!isnan(lastC2)) {
      String j2 = String("{\"t_c\":") + String(lastC2,2) + ",\"sensor\":\"S2\"}\n";
      espBroadcast(j2);
    }
  }
}
