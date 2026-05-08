/**
 * Smart Street Light IoT — ESP32 Firmware v2
 * ===========================================
 * Hardware : ESP32 DevKit + IR Sensor (digital OUT pin)
 *
 * Features:
 *   1. IR sensor detects vehicle → POST /api/detect to Flask
 *   2. Built-in web server handles commands FROM Flask:
 *        GET /cmd?action=light_on
 *        GET /cmd?action=light_off
 *        GET /cmd?action=light_auto
 *   3. Auto light logic (night/day) runs locally as fallback
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <ArduinoJson.h>

// ── WiFi credentials ──────────────────────────────────────────
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// ── Flask server  (your PC's LAN IP) ──────────────────────────
// NOTE: The server's ALLOWED_IPS must include this ESP32's IP!
const char* SERVER_URL = "http://192.168.1.6:5000/api/detect";
const char* API_KEY    = "esp32-secret-key-2024";

// ── Hardware pins ─────────────────────────────────────────────
const int IR_SENSOR_PIN = 14;   // GPIO14 — IR sensor OUT (active-LOW)
const int LIGHT_PIN     = 26;   // GPIO26 — Relay / LED (HIGH = ON)
const int STATUS_LED    = 2;    // Built-in LED

// ── Debounce ──────────────────────────────────────────────────
const unsigned long DEBOUNCE_MS = 2000;
unsigned long lastDetectionTime = 0;
bool          lastSensorState   = HIGH;

// ── Light control mode ────────────────────────────────────────
enum LightMode { AUTO, FORCE_ON, FORCE_OFF };
LightMode lightMode = AUTO;

// ── Built-in web server for receiving Flask commands ──────────
WebServer server(80);

// ─────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  pinMode(IR_SENSOR_PIN, INPUT);
  pinMode(LIGHT_PIN,     OUTPUT);
  pinMode(STATUS_LED,    OUTPUT);

  connectWiFi();
  setupWebServer();

  Serial.println("[ESP32] Ready. IP: " + WiFi.localIP().toString());
}

// ─────────────────────────────────────────────────────────────
void loop() {
  server.handleClient();   // Handle local commands (if on same network)

  handleIRSensor();        // Check vehicle detection
  
  // Cloud Sync: Ask the server for the light status every 5 seconds
  static unsigned long lastSync = 0;
  if (millis() - lastSync > 5000) {
    lastSync = millis();
    syncWithServer();
  }

  updateLightOutput();     // Apply current light mode
}

// ════════════════════════════════════════════════════════════
// IR Sensor
// ════════════════════════════════════════════════════════════
void handleIRSensor() {
  bool sensorState = digitalRead(IR_SENSOR_PIN);
  unsigned long now = millis();

  if (sensorState == LOW && lastSensorState == HIGH &&
      (now - lastDetectionTime) > DEBOUNCE_MS) {

    lastDetectionTime = now;
    Serial.println("[IR] Vehicle detected!");
    digitalWrite(STATUS_LED, HIGH);
    sendDetectionEvent();
    delay(200);
    digitalWrite(STATUS_LED, LOW);
  }
  lastSensorState = sensorState;
}

// ════════════════════════════════════════════════════════════
// Light control
// ════════════════════════════════════════════════════════════
bool isNightTime() {
  // Simple fallback: use ESP32 millis-based hours if NTP not set up.
  // For real deployment, use NTP (see WiFiUDP + NTPClient library).
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) return false;
  int h = timeinfo.tm_hour;
  return (h >= 18 || h < 6);
}

void updateLightOutput() {
  bool shouldBeOn = false;

  switch (lightMode) {
    case FORCE_ON:  shouldBeOn = true;  break;
    case FORCE_OFF: shouldBeOn = false; break;
    case AUTO:
    default:        shouldBeOn = isNightTime(); break;
  }

  digitalWrite(LIGHT_PIN, shouldBeOn ? HIGH : LOW);
}

// ════════════════════════════════════════════════════════════
// Built-in web server — receives commands FROM Flask
// ════════════════════════════════════════════════════════════
void setupWebServer() {
  // Flask will call: GET http://<esp32_ip>/cmd?action=light_on
  server.on("/cmd", HTTP_GET, []() {
    String action = server.arg("action");
    Serial.println("[CMD] Received: " + action);

    if (action == "light_on") {
      lightMode = FORCE_ON;
      server.send(200, "application/json", "{\"status\":\"light forced ON\"}");

    } else if (action == "light_off") {
      lightMode = FORCE_OFF;
      server.send(200, "application/json", "{\"status\":\"light forced OFF\"}");

    } else if (action == "light_auto") {
      lightMode = AUTO;
      server.send(200, "application/json", "{\"status\":\"light set to AUTO\"}");

    } else {
      server.send(400, "application/json", "{\"error\":\"unknown action\"}");
    }
  });

  // Health check endpoint
  server.on("/health", HTTP_GET, []() {
    server.send(200, "application/json", "{\"status\":\"ok\"}");
  });

  server.begin();
  Serial.println("[WebServer] ESP32 listening on port 80");
}

// ════════════════════════════════════════════════════════════
// Send detection event to Flask
// ════════════════════════════════════════════════════════════
void sendDetectionEvent() {
  if (WiFi.status() != WL_CONNECTED) { connectWiFi(); return; }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);

  StaticJsonDocument<128> doc;
  doc["sensor_id"]    = "sensor-01";
  doc["location"]     = "Main Road";
  doc["vehicle_type"] = "car";

  String payload;
  serializeJson(doc, payload);

  int code = http.POST(payload);
  if (code > 0) {
    Serial.printf("[HTTP] Sent → %d\n", code);
  } else {
    Serial.printf("[HTTP] Error: %s\n", http.errorToString(code).c_str());
  }
  http.end();
}

// ════════════════════════════════════════════════════════════
// Sync with Server (Cloud Polling)
// ════════════════════════════════════════════════════════════
void syncWithServer() {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  // Use your Cloud URL here!
  String url = String(SERVER_URL);
  url.replace("/detect", "/esp32/sync"); 
  
  http.begin(url);
  http.addHeader("X-API-Key", API_KEY);
  
  int code = http.GET();
  if (code == 200) {
    String payload = http.getString();
    StaticJsonDocument<256> doc;
    deserializeJson(doc, payload);
    
    String serverLight = doc["light"]; // "ON" or "OFF"
    
    if (serverLight == "ON") {
      lightMode = FORCE_ON;
    } else {
      lightMode = FORCE_OFF;
    }
    Serial.println("[Sync] Server says light should be: " + serverLight);
  }
  http.end();
}

// ════════════════════════════════════════════════════════════
// WiFi
// ════════════════════════════════════════════════════════════
void connectWiFi() {
  Serial.print("[WiFi] Connecting to ");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500); Serial.print("."); attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    // Sync time via NTP for accurate night/day detection
    configTime(5 * 3600, 0, "pool.ntp.org");   // UTC+5, adjust to your timezone
    Serial.println("\n[WiFi] Connected! IP: " + WiFi.localIP().toString());
  } else {
    Serial.println("\n[WiFi] FAILED — restarting…");
    ESP.restart();
  }
}
