// TARS's reach to a Soehnle AC500 air purifier too far from the Pi, on an M5Stack Atom Lite (ESP32-PICO-D4)
// beside it. examples/my_house/purifier.py speaks the purifier's protocol; this only carries its bytes.
//
// It keeps a Bluetooth connection to the purifier ready, unused: a command goes over a connection that's already
// up, and takes about a second and a half instead of four. What the purifier does, found on 2026-11-18:
//   - a command right after the hello frames, first on a connection, is taken; later ones on the same connection
//     often aren't. So each connection carries one request, and is replaced after.
//   - it drops a connection after about 13 seconds without a hello. So the ready one gets hellos every few seconds,
//     which keep it up and still leave it taking a command.
// Our own firmware on NimBLE, not ESPHome: through ESPHome's Bluetooth stack connections hung or never came up,
// while NimBLE on the same board connected 33 times out of 33.
//
// TARS connects on TCP port 8780 and proves it has the key: the Atom says "tars-bt 1 <nonce>", TARS answers
// "auth <HMAC-SHA256(key, nonce) in hex>". Then, a line each:
//   link        a connection for this request: "linked" at once if one is ready, after making one if not, or
//               "failed <why>"; the latest status follows, and each it reports, as "live <hex>"
//   send <hex>  write one frame to it: "sent", or "failed <why>"
//   unlink      done with it: "unlinked" (also when TARS hangs up); a fresh one is made ready
//   heard       "heard <ms>": how long since it last answered, -1 if never
// If the purifier drops the connection mid-request, "unlinked" comes unasked.
//
// Its light: red, no Wi-Fi; orange, the purifier hasn't answered for a minute; green, all well.
#include <Arduino.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <NimBLEDevice.h>
#include <WiFi.h>
#include <mbedtls/md.h>

#include "secrets.h"

static const uint16_t PORT = 8780;
static const uint32_t CONNECT_MS = 2000;  // one try
static const int TRIES = 5;               // when TARS is waiting for one
static const uint32_t STATUS_MS = 2500;   // a new connection counts once it's reported a status
static const uint32_t UNHEARD_MS = 60000;
static const uint32_t KEEP_MS = 5000;  // a hello this often keeps the ready connection up
static const uint8_t HELLO[] = {0xAA, 0x03, 0xAF, 0x00, 0x01, 0xB3, 0xEE};
static const uint8_t LED = 27;
static const char *SERVICE_UUID = "ffa0", *WRITE_UUID = "ef01", *LIVE_UUID = "ef02";

static NimBLEAddress purifier(std::string(PURIFIER), BLE_ADDR_PUBLIC);
static NimBLEClient *ble = nullptr;
static NimBLERemoteCharacteristic *writer = nullptr;
static bool used = false;          // the connection has carried a request: replaced after it
static uint32_t last_heard = 0;    // millis() of its last status; 0, never
static uint32_t next_ready = 0;    // millis() of the next try at a ready connection
static uint32_t kept = 0;          // millis() of the last hello keeping it up
static QueueHandle_t statuses;     // frames from the Bluetooth task, handled in the loop

struct Frame {
  uint8_t len;
  uint8_t data[32];
};
static Frame latest{0};  // its last status

static void on_live(NimBLERemoteCharacteristic *, uint8_t *data, size_t len, bool) {
  Frame f{(uint8_t)min(len, sizeof(f.data))};
  memcpy(f.data, data, f.len);
  xQueueSend(statuses, &f, 0);
}

static String hex(const uint8_t *data, size_t len) {
  static const char *digits = "0123456789abcdef";
  String out;
  for (size_t i = 0; i < len; i++) {
    out += digits[data[i] >> 4];
    out += digits[data[i] & 0xF];
  }
  return out;
}

static void drop() {
  if (ble) {
    ble->disconnect();
    NimBLEDevice::deleteClient(ble);
  }
  ble = nullptr;
  writer = nullptr;
  used = false;
}

static bool linked() { return ble && ble->isConnected(); }

// Connected, subscribed, and a status heard on it, within `tries`; or why not.
static String connect(int tries) {
  drop();
  String why = "it didn't answer";
  for (int attempt = 0; attempt < tries; attempt++) {
    NimBLEClient *client = NimBLEDevice::createClient();
    client->setConnectTimeout(CONNECT_MS);
    if (!client->connect(purifier)) {
      NimBLEDevice::deleteClient(client);
      continue;
    }
    NimBLERemoteService *service = client->getService(SERVICE_UUID);
    NimBLERemoteCharacteristic *w = service ? service->getCharacteristic(WRITE_UUID) : nullptr;
    NimBLERemoteCharacteristic *live = service ? service->getCharacteristic(LIVE_UUID) : nullptr;
    if (!(w && live && live->subscribe(true, on_live))) {
      why = "it doesn't have the purifier's characteristics";
      client->disconnect();
      NimBLEDevice::deleteClient(client);
      continue;
    }
    ble = client;
    writer = w;
    uint32_t since = millis();
    Frame f;
    while (millis() - since < STATUS_MS && client->isConnected())
      if (xQueueReceive(statuses, &f, pdMS_TO_TICKS(50)) == pdTRUE) {
        latest = f;
        last_heard = millis();
        return "";
      }
    why = "it sent no status";
    drop();
  }
  return why;
}

static bool authentic(const String &nonce, const String &answer) {
  uint8_t mac[32];
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), (const uint8_t *)TARS_KEY, strlen(TARS_KEY),
                  (const uint8_t *)nonce.c_str(), nonce.length(), mac);
  String expected = hex(mac, sizeof(mac));
  if (answer.length() != expected.length()) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < expected.length(); i++) diff |= answer[i] ^ expected[i];
  return diff == 0;
}

static void light() {
  static uint32_t last = 0;
  if (millis() - last < 250) return;
  last = millis();
  if (WiFi.status() != WL_CONNECTED) rgbLedWrite(LED, 20, 0, 0);
  else if (!last_heard || millis() - last_heard > UNHEARD_MS) rgbLedWrite(LED, 20, 8, 0);
  else rgbLedWrite(LED, 0, 12, 0);
}

static WiFiServer server(PORT);
static WiFiClient tars;
static bool authed = false, requesting = false;  // requesting: TARS holds the connection for a request
static String nonce, line;

static void answer(const String &what) { tars.print(what + "\n"); }

static void command(const String &cmd) {
  if (!authed) {
    authed = cmd.startsWith("auth ") && authentic(nonce, cmd.substring(5));
    if (!authed) {
      answer("refused");
      tars.stop();
    } else {
      answer("ok");
    }
    return;
  }
  if (cmd == "link") {
    String why = linked() && !used ? "" : connect(TRIES);
    if (!why.isEmpty()) return answer("failed " + why);
    requesting = used = true;
    answer("linked");
    answer("live " + hex(latest.data, latest.len));
  } else if (cmd == "unlink") {
    requesting = false;
    drop();
    next_ready = 0;
    answer("unlinked");
  } else if (cmd == "heard") {
    answer("heard " + String(last_heard ? (long)(millis() - last_heard) : -1L));
  } else if (cmd.startsWith("send ")) {
    String h = cmd.substring(5);
    uint8_t bytes[32];
    size_t n = h.length() / 2;
    if (!requesting || !linked()) return answer("failed not linked");
    if (h.length() % 2 || n > sizeof(bytes)) return answer("failed not a frame");
    for (size_t i = 0; i < n; i++) bytes[i] = strtoul(h.substring(2 * i, 2 * i + 2).c_str(), nullptr, 16);
    answer(writer->writeValue(bytes, n, true) ? "sent" : "failed the write wasn't taken");
  } else {
    answer("failed no such command");
  }
}

void setup() {
  Serial.begin(115200);
  statuses = xQueueCreate(16, sizeof(Frame));
  rgbLedWrite(LED, 20, 0, 0);
  WiFi.setHostname(NAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  MDNS.begin(NAME);
  ArduinoOTA.setHostname(NAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([] { drop(); });
  ArduinoOTA.begin();
  server.begin();
  NimBLEDevice::init("");
  NimBLEDevice::setPower(9);
  NimBLEDevice::deleteAllBonds();  // pairing was tried once and changed nothing: none kept
}

void loop() {
  ArduinoOTA.handle();
  light();
  if (tars && !tars.connected()) {
    tars.stop();
    if (requesting) drop();
    requesting = false;
  }
  if (!tars) {
    tars = server.available();
    if (tars) {
      authed = false;
      line = "";
      uint8_t raw[16];
      esp_fill_random(raw, sizeof(raw));
      nonce = hex(raw, sizeof(raw));
      answer("tars-bt 1 " + nonce);
    }
  }
  while (tars && tars.available()) {
    char c = tars.read();
    if (c == '\n') {
      line.trim();
      command(line);
      line = "";
    } else if (line.length() < 200) {
      line += c;
    }
  }
  Frame f;
  while (xQueueReceive(statuses, &f, 0) == pdTRUE) {
    latest = f;
    last_heard = millis();
    if (requesting && tars && authed) answer("live " + hex(f.data, f.len));
  }
  if (ble && !ble->isConnected()) {  // the purifier let go: about 13 s after the last, if unused
    drop();
    if (requesting && tars && authed) answer("unlinked");
    requesting = false;
  }
  // A ready connection, kept: made again whenever it's gone, with a pause after a failed try.
  if (!ble && !requesting && WiFi.status() == WL_CONNECTED && millis() >= next_ready) {
    if (!connect(1).isEmpty()) next_ready = millis() + 1000;
    kept = 0;
  }
  if (linked() && !requesting && millis() - kept >= KEEP_MS) {
    writer->writeValue(HELLO, sizeof(HELLO), true);
    writer->writeValue(HELLO, sizeof(HELLO), true);
    kept = millis();
  }
  delay(2);
}
