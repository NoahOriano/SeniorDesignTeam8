/*
  ESP32-AT Wi-Fi Tester — RAM-Lite, Serial-Verbose (UNO friendly)
  - No Arduino String; small fixed buffers
  - STA join -> fallback to AP (ch6 -> OPEN)
  - TCP server on port 5000
  - Minimal +IPD parser to ECHO payload back

  If UART is flaky, set ESP to 57600 first (via USB terminal):
    AT+UART_DEF=57600,8,1,0,0
  Then change ESP_BAUD to 57600 below.
*/

#include <Arduino.h>
#include <AltSoftSerial.h>

// ---------- User Wi-Fi ----------
const char HOTSPOT_SSID[] PROGMEM = "NOAHORIANO9840";
const char HOTSPOT_PASS[] PROGMEM = "uu70778M";
const char AP_SSID[]      PROGMEM = "ThirdBoxWifi";
const char AP_PASS[]      PROGMEM = "12345678";
const int  TCP_PORT = 5000;

// ---------- Serial speeds ----------
static const uint32_t PC_BAUD  = 115200;
static const uint32_t ESP_BAUD = 115200; // change to 57600 if you changed ESP with AT+UART_DEF

// ---------- ESP link ----------
AltSoftSerial esp; // UNO: RX=D8, TX=D9

// ---------- Small buffers (keep RAM low) ----------
static char  acc[128];   // rolling token buffer
static uint8_t accLen = 0;

static char  line[128];  // line logger
static uint8_t lineLen = 0;

static char  tmp[96];    // scratch for prints/commands

// +IPD parse state (tiny)
static bool    ipdActive = false;
static int16_t ipdId = -1;
static int16_t ipdLen = 0;
static int16_t ipdRead = 0;

static bool apMode = false;

// ---------- Tiny logging helpers ----------
inline void logPrefix(const __FlashStringHelper* pfx) {
  Serial.print('['); Serial.print(millis()); Serial.print(F("] "));
  Serial.print(pfx); Serial.print(' ');
}
inline void logInfo(const __FlashStringHelper* msg)  { logPrefix(F("INFO ")); Serial.println(msg); }
inline void logStep(const __FlashStringHelper* msg)  { logPrefix(F("STEP ")); Serial.println(msg); }
inline void logOK  (const __FlashStringHelper* msg)  { logPrefix(F("OK   ")); Serial.println(msg); }
inline void logErr (const __FlashStringHelper* msg)  { logPrefix(F("ERR  ")); Serial.println(msg); }

// Write program-memory string to esp
void espPrintPGM(const char* p) {
  while (true) {
    char c = pgm_read_byte(p++);
    if (!c) break;
    esp.write(c);
  }
}

// Clear small buffers
inline void clearAcc(){ accLen = 0; }
inline void clearLine(){ lineLen = 0; }

// Push one byte into rolling buffer and keep NUL-terminated
inline void pushAcc(char c) {
  if (accLen >= sizeof(acc)-1) {
    // shift left by half to keep recent data
    memmove(acc, acc + (sizeof(acc)/2), (sizeof(acc)/2));
    accLen = (sizeof(acc)/2);
  }
  acc[accLen++] = c;
  acc[accLen] = '\0';
}
inline void pushLine(char c) {
  if (lineLen < sizeof(line)-1) {
    line[lineLen++] = c;
    line[lineLen] = '\0';
  }
}

// Print a received line neatly
void printRxLine() {
  // trim CR/LF
  while (lineLen && (line[lineLen-1] == '\r' || line[lineLen-1] == '\n' || line[lineLen-1] == ' ')) line[--lineLen] = 0;
  if (!lineLen) return;
  Serial.print('['); Serial.print(millis()); Serial.print(F("] "));
  Serial.print(F("<< "));
  Serial.println(line);
}

// Pump bytes from ESP: log lines; fill acc; detect +IPD header
void pumpESP() {
  while (esp.available()) {
    char c = (char)esp.read();
    pushAcc(c);

    if (c == '\n') { // complete line for logging
      printRxLine();
      clearLine();
    } else if (c != '\r') {
      pushLine(c);
    }

    // detect start of +IPD,<id>,<len>:
    if (!ipdActive) {
      // very small check to avoid strstr cost
      if (accLen >= 5 && acc[accLen-5] == '+' && acc[accLen-4] == 'I' && acc[accLen-3] == 'P' && acc[accLen-2] == 'D' && acc[accLen-1] == ',') {
        // read ahead minimally: +IPD,<id>,<len>:
        // We'll parse from the end by scanning backwards to last "+IPD,"
        // Simpler: reset state and parse fresh using the 'line' as soon as ':' arrives
      }
    }

    // If we are not inside a payload yet, try to parse header when ':' arrives
    if (!ipdActive && c == ':') {
      // find "+IPD," in acc
      char *start = strstr(acc, "+IPD,");
      if (start) {
        // format: +IPD,<id>,<len>:<payload...
        // parse id
        char *p = start + 5;
        ipdId = atoi(p);
        char *comma2 = strchr(p, ',');
        if (comma2) {
          ipdLen = atoi(comma2 + 1);
          ipdRead = 0;
          ipdActive = (ipdId >= 0 && ipdLen > 0);
          if (ipdActive) {
            logInfo(F("Begin +IPD frame"));
          }
        }
      }
    }

    // If inside payload, count bytes read
    if (ipdActive) {
      ipdRead++;
      if (ipdRead >= ipdLen) {
        // payload fully in acc, but we don't want to store it; we'll re-read from ESP for future frames
        // Note: we cannot easily slice the exact payload without a larger buffer.
        // We'll do echo in a different way: ask ESP to echo back last payload using transparent send right away.
        // Minimal practical approach: send back a static ack since we kept RAM low.
        // (If you need full payload echo, see the variant below that reads the payload actively.)
        ipdActive = false;
      }
    }

    // Bound acc to avoid runaway RAM
    if (accLen > sizeof(acc)-8) { accLen = 0; acc[0] = '\0'; }
  }
}

// Wait for token expect1 OR (optional) expect2; error/timeout distinct
bool waitToken(const char* expect1, const char* expect2, uint32_t timeoutMs) {
  uint32_t t0 = millis();
  clearAcc();
  while (millis() - t0 < timeoutMs) {
    pumpESP();
    if (expect1 && strstr(acc, expect1)) return true;
    if (expect2 && strstr(acc, expect2)) return true;
    if (strstr(acc, "\r\nERROR\r\n") || (accLen >= 5 && strstr(acc, "ERROR"))) return false;
  }
  return false; // timeout
}

// Send AT command; log it; wait for token(s)
bool atCmd(const char* cmd, const char* expect1 = "OK", const char* expect2 = NULL, uint32_t to = 8000) {
  Serial.print('['); Serial.print(millis()); Serial.print(F("] >> "));
  Serial.println(cmd);
  esp.print(cmd); esp.print("\r\n");
  bool ok = waitToken(expect1, expect2, to);
  if (ok) logOK(F("Matched expected token"));
  else    logErr(F("Timeout or ERROR from ESP"));
  return ok;
}

bool joinSTA() {
  logStep(F("STA join..."));
  if (!atCmd("AT")) return false;
  atCmd("ATE0");
  atCmd("AT+SYSSTORE=1");
  if (!atCmd("AT+CWMODE=1")) return false;

  // Build CWJAP command into tmp (to avoid String)
  snprintf_P(tmp, sizeof(tmp), PSTR("AT+CWJAP=\"%s\",\"%s\""), HOTSPOT_SSID, HOTSPOT_PASS);
  Serial.print('['); Serial.print(millis()); Serial.print(F("] >> "));
  Serial.println(tmp);
  esp.print(tmp); esp.print("\r\n");
  if (!waitToken("WIFI GOT IP", "OK", 25000)) {
    logErr(F("CWJAP failed"));
    return false;
  }
  logOK(F("Joined AP"));

  atCmd("AT+CIFSR"); // print IP lines
  atCmd("AT+CIPMUX=1");
  snprintf(tmp, sizeof(tmp), "AT+CIPSERVER=1,%d", TCP_PORT);
  if (!atCmd(tmp)) return false;

  apMode = false;
  logOK(F("STA mode ready (see IP above)"));
  return true;
}

bool startAP() {
  logStep(F("AP fallback..."));
  if (!atCmd("AT")) return false;
  atCmd("ATE0");
  if (!atCmd("AT+CWMODE=2")) return false;

  // WPA2 ch6
  snprintf_P(tmp, sizeof(tmp), PSTR("AT+CWSAP=\"%s\",\"%s\",6,3,4,0"), AP_SSID, AP_PASS);
  if (!atCmd(tmp)) {
    logErr(F("CWSAP ch6 failed; trying OPEN on ch1"));
    // OPEN ch1 (no password)
    snprintf_P(tmp, sizeof(tmp), PSTR("AT+CWSAP=\"%s\",\"\",1,0,4,0"), AP_SSID);
    if (!atCmd(tmp)) {
      logErr(F("CWSAP open ch1 failed"));
      return false;
    }
  }

  atCmd("AT+CIFSR"); // should show 192.168.4.1
  if (!atCmd("AT+CIPMUX=1")) return false;
  snprintf(tmp, sizeof(tmp), "AT+CIPSERVER=1,%d", TCP_PORT);
  if (!atCmd(tmp)) return false;

  apMode = true;
  logOK(F("AP mode up (connect to SSID, usually 192.168.4.1)"));
  return true;
}

// --------- Minimal +IPD echo (payload-aware, RAM-lite) ---------
// This actively reads a payload of length L from ESP and echoes it back.
// Because we didn’t buffer the entire +IPD, we re-detect headers and then
// read exactly L bytes into a small chunk and send back in pieces.
void pollAndEchoIPD() {
  // Look for "+IPD," followed by id,len:
  static enum { S_IDLE, S_HAVE_HDR, S_READING, S_ECHOING } st = S_IDLE;
  static int  curId = -1;
  static int  curLen = 0;
  static int  got = 0;

  while (esp.available()) {
    char c = (char)esp.read();

    // Assemble a small header buffer (reuse acc as small rolling buffer)
    pushAcc(c);

    if (st == S_IDLE) {
      char *h = strstr(acc, "+IPD,");
      char *colon = h ? strchr(h, ':') : NULL;
      if (h && colon) {
        // parse id and len
        curId = atoi(h + 5);
        char *comma2 = strchr(h + 5, ',');
        curLen = (comma2 ? atoi(comma2 + 1) : -1);
        got = 0;
        clearAcc(); // reuse as payload sink
        st = (curId >= 0 && curLen > 0) ? S_READING : S_IDLE;
        if (st == S_READING) {
          logInfo(F("IPD hdr parsed"));
        }
      }
    } else if (st == S_READING) {
      // Read exactly curLen bytes
      if (accLen < sizeof(acc)-1) { acc[accLen++] = c; acc[accLen] = 0; }
      got++;
      if (got >= curLen) {
        // echo back: "ECHO: " + payload
        // Send header
        const char prefix[] = "ECHO: ";
        int total = (int)sizeof(prefix)-1 + curLen + 1; // + '\n'
        snprintf(tmp, sizeof(tmp), "AT+CIPSEND=%d,%d", curId, total);
        if (atCmd(tmp, ">", NULL, 2000)) {
          esp.write((const uint8_t*)prefix, sizeof(prefix)-1);
          esp.write((const uint8_t*)acc, curLen);
          esp.write('\n');
          waitToken("SEND OK", NULL, 2000);
          logOK(F("Echoed payload"));
        } else {
          logErr(F("CIPSEND prompt failed"));
        }
        // reset
        clearAcc();
        st = S_IDLE;
      }
    }
  }
}

void setup() {
  Serial.begin(PC_BAUD);
  delay(200);
  logInfo(F("ESP32-AT WiFi Test (RAM-Lite)"));
  logInfo(F("UNO D8<=ESP TX0, D9=>ESP RX0 (3.3V!), common GND"));
  logInfo(F("If unstable, set ESP to 57600 and change ESP_BAUD."));

  esp.begin(ESP_BAUD);
  delay(400);

  if (!joinSTA()) {
    logErr(F("STA failed; trying RESTORE and AP fallback"));
    atCmd("AT+RESTORE", "ready", NULL, 5000);
    delay(600);
    esp.begin(ESP_BAUD);
    if (!startAP()) {
      logErr(F("AP fallback failed — check power/level shifting/wiring/AT firmware"));
    }
  }

  logInfo(F("Ready. Connect a TCP client to port 5000 and type; you should get ECHO."));
}

void loop() {
  pumpESP();       // log incoming lines
  pollAndEchoIPD();// echo any incoming payloads
}
