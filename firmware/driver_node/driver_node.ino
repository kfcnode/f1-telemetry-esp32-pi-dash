/*
  F1 telemetry — driver node (ESP32)

  Reads the driver's vitals and publishes them two ways:
    1. ThingSpeak (cloud), fields 1-6, every 20 s   -> car_sender.py --vitals thingspeak
    2. GET http://f1-driver.local/vitals (JSON)      -> car_sender.py --vitals local  (about 1 s old)

  Sensors (wiring: docs/wiring.md)
    MAX30100 pulse oximeter (I2C 0x57)   heart rate, SpO2
    MPU6050 accelerometer   (I2C 0x68)   G-force magnitude (peak since the last upload)
    10k NTC thermistor      (GPIO35)     skin temperature
    GSR module              (GPIO32)     perspiration index (relative to a dry baseline at power-up)
    stretch / piezo belt    (GPIO34)     respiration rate

  Structure
    loop() on core 1 does nothing slow: it calls pox.update() as often as possible (the MAX30100 library
    needs that to detect beats), samples the analog channels and the IMU at 25 Hz, serves /vitals,
    and copies the results into a shared snapshot.
    uploadTask() on core 0 does the slow part: Wi-Fi reconnects and the ThingSpeak HTTP request, which can
    take a second and would otherwise make the pulse oximeter miss beats.

  Libraries: "MAX30100lib" by OXullo Intersecans (Library Manager). Board: ESP32 Dev Module, core 3.x.
*/
#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include "MAX30100_PulseOximeter.h"

#include "config.h"
#include "secrets.h"      // copy secrets.h.example to secrets.h and fill it in
#include "vitals_dsp.h"

// ---------------------------------------------------------------------------- state
PulseOximeter pox;
bool poxOk = false;
bool mpuOk = false;
volatile uint32_t lastBeatMs = 0;
volatile uint32_t beatCount = 0;

WebServer server(80);

dsp::BreathDetector breath(SAMPLE_HZ, RESP_MIN_AMP_MV);
dsp::GsrIndex gsr(GSR_SPAN_MV, GSR_CALIB_SAMPLES);
dsp::Ema skinSmooth(0.05f);
dsp::PeakHold gPeakLocal;     // for /vitals (reset every second)
dsp::PeakHold gPeakUpload;    // for ThingSpeak (reset at each upload)

// Shared between loop() (writer) and uploadTask() (reader)
struct Snapshot {
  dsp::Vitals v;            // g_force here = peak over the last second
  float gPeakSinceUpload;
  uint32_t takenMs;
};
Snapshot snap;
SemaphoreHandle_t snapLock;
volatile bool takeUploadPeak = false;

volatile uint32_t tsOk = 0, tsFail = 0;

// ---------------------------------------------------------------------------- MPU6050 (raw registers, no library)
const uint8_t MPU_ADDR = 0x68;

bool mpuWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool mpuInit() {
  return mpuWrite(0x6B, 0x00)        // PWR_MGMT_1: wake up
      && mpuWrite(0x1C, 0x10)        // ACCEL_CONFIG: +-8 g (4096 LSB/g)
      && mpuWrite(0x1A, 0x03);       // CONFIG: 44 Hz digital low-pass
}

bool mpuReadG(float& g) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);                  // ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDR, (uint8_t)6) != 6) return false;
  int16_t ax = (Wire.read() << 8) | Wire.read();
  int16_t ay = (Wire.read() << 8) | Wire.read();
  int16_t az = (Wire.read() << 8) | Wire.read();
  g = dsp::g_magnitude(ax, ay, az, 4096.0f);
  return true;
}

// ---------------------------------------------------------------------------- helpers
void onBeat() {
  lastBeatMs = millis();
  beatCount++;
  digitalWrite(PIN_STATUS_LED, !digitalRead(PIN_STATUS_LED));
}

float readMilliVolts(uint8_t pin, uint8_t n = 4) {
  uint32_t sum = 0;
  for (uint8_t i = 0; i < n; i++) sum += analogReadMilliVolts(pin);
  return sum / (float)n;
}

void wifiEnsure() {
  static uint32_t lastTry = 0;
  if (WiFi.status() == WL_CONNECTED) return;
  if (lastTry && millis() - lastTry < WIFI_RETRY_MS) return;
  lastTry = millis();
  Serial.println("[wifi] connecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// ---------------------------------------------------------------------------- web server (local fast path)
void handleVitals() {
  Snapshot s;
  xSemaphoreTake(snapLock, portMAX_DELAY);
  s = snap;
  xSemaphoreGive(snapLock);
  char buf[512];
  dsp::vitals_json(buf, sizeof buf, s.v, millis(), millis() - s.takenMs, beatCount, WiFi.RSSI(), tsOk, tsFail);
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", buf);
}

void handleRoot() {
  String msg = "F1 driver node\n";
  msg += "MAX30100: " + String(poxOk ? "ok" : "NOT FOUND") + "\n";
  msg += "MPU6050:  " + String(mpuOk ? "ok" : "NOT FOUND") + "\n";
  msg += "GSR: " + String(gsr.calibrating() ? "calibrating (keep hands dry and still)" : "ready") + "\n";
  msg += "ThingSpeak uploads ok/failed: " + String(tsOk) + "/" + String(tsFail) + "\n";
  msg += "JSON: /vitals\n";
  server.send(200, "text/plain", msg);
}

// ---------------------------------------------------------------------------- ThingSpeak upload task (core 0)
void uploadTask(void*) {
  uint32_t next = millis() + 8000;          // give the sensors time to settle after boot
  for (;;) {
    wifiEnsure();
    if ((int32_t)(millis() - next) >= 0) {
      next += UPLOAD_PERIOD_MS;
      takeUploadPeak = true;                // loop() hands over the peak G since the last upload
      for (int i = 0; i < 20 && takeUploadPeak; i++) vTaskDelay(pdMS_TO_TICKS(50));
      Snapshot s;
      xSemaphoreTake(snapLock, portMAX_DELAY);
      s = snap;
      xSemaphoreGive(snapLock);
      dsp::Vitals up = s.v;
      up.g_force = s.gPeakSinceUpload > 0 ? s.gPeakSinceUpload : NAN;

      if (WiFi.status() == WL_CONNECTED) {
        char body[256];
        dsp::thingspeak_body(body, sizeof body, THINGSPEAK_WRITE_KEY, up);
        HTTPClient http;
        http.setTimeout(4000);
        http.begin("http://api.thingspeak.com/update");
        http.addHeader("Content-Type", "application/x-www-form-urlencoded");
        int code = http.POST(body);
        String resp = http.getString();     // entry id, or "0" when the write was rejected (rate limit)
        http.end();
        if (code == 200 && resp.toInt() > 0) {
          tsOk++;
          Serial.printf("[thingspeak] entry %s\n", resp.c_str());
        } else {
          tsFail++;
          Serial.printf("[thingspeak] failed: HTTP %d, body '%s'\n", code, resp.c_str());
        }
      } else {
        tsFail++;
        Serial.println("[thingspeak] skipped: no Wi-Fi");
      }
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

// ---------------------------------------------------------------------------- setup / loop (core 1)
void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(PIN_STATUS_LED, OUTPUT);
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);           // full 0-3.3 V range on every analog pin

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire.setClock(400000);
  mpuOk = mpuInit();
  poxOk = pox.begin();                      // the library calls Wire.begin() again with the default pins (21/22)
  if (poxOk) {
    pox.setIRLedCurrent(MAX30100_LED_CURR_7_6MA);
    pox.setOnBeatDetectedCallback(onBeat);
  }
  Serial.printf("[boot] MAX30100 %s, MPU6050 %s\n", poxOk ? "ok" : "NOT FOUND", mpuOk ? "ok" : "NOT FOUND");
  Serial.println("[boot] GSR calibrating for 10 s: keep hands dry and still");

  snapLock = xSemaphoreCreateMutex();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                     // lower latency for /vitals
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 10000) {
    if (poxOk) pox.update();
    delay(5);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] %s, RSSI %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
    if (MDNS.begin(MDNS_NAME)) MDNS.addService("http", "tcp", 80);
  }
  server.on("/", handleRoot);
  server.on("/vitals", handleVitals);
  server.begin();

  xTaskCreatePinnedToCore(uploadTask, "upload", 8192, nullptr, 1, nullptr, 0);
}

void loop() {
  static uint32_t nextSample = 0, nextPublish = 0, nextSerial = 0;
  static float gNow = NAN;
  static float skin = NAN, persp = NAN;

  if (poxOk) pox.update();                  // as often as possible
  server.handleClient();

  uint32_t now = millis();
  if ((int32_t)(now - nextSample) >= 0) {
    nextSample = now + 1000 / SAMPLE_HZ;
    breath.update(readMilliVolts(PIN_RESP_BELT, 2));
    float t = dsp::ntc_celsius(readMilliVolts(PIN_SKIN_NTC), VCC_MV, NTC_R_SERIES, NTC_R0, NTC_T0_C, NTC_BETA);
    skin = isnan(t) ? NAN : skinSmooth.update(t);
    persp = gsr.update(readMilliVolts(PIN_GSR));
    float g;
    if (mpuOk && mpuReadG(g)) {
      gNow = g;
      gPeakLocal.update(g);
      gPeakUpload.update(g);
    } else {
      gNow = NAN;
    }
  }

  if ((int32_t)(now - nextPublish) >= 0) {   // refresh the shared snapshot 4 times a second
    nextPublish = now + 250;
    bool beatRecent = poxOk && lastBeatMs && (now - lastBeatMs) < PPG_BEAT_TIMEOUT_MS;
    dsp::Vitals v;
    v.hr_bpm = beatRecent ? dsp::valid_or_nan(pox.getHeartRate(), 30, 220) : NAN;
    v.spo2_pct = beatRecent ? dsp::valid_or_nan(pox.getSpO2(), 70, 100) : NAN;
    v.skin_temp_c = dsp::valid_or_nan(skin, 20, 42);
    v.perspiration_pct = persp;
    v.resp_bpm = breath.rate();
    static uint32_t lastPeakReset = 0;
    float gShown = gPeakLocal.peak > 0 ? gPeakLocal.peak : gNow;
    if (now - lastPeakReset >= 1000) { gPeakLocal.take(); lastPeakReset = now; }
    v.g_force = gShown;

    xSemaphoreTake(snapLock, portMAX_DELAY);
    snap.v = v;
    snap.takenMs = now;
    if (takeUploadPeak) {
      snap.gPeakSinceUpload = gPeakUpload.take();
      takeUploadPeak = false;
    }
    xSemaphoreGive(snapLock);

    if ((int32_t)(now - nextSerial) >= 0) {
      nextSerial = now + SERIAL_PERIOD_MS;
      Serial.printf("HR %.0f  SpO2 %.0f  skin %.2f  persp %.1f  resp %.1f  g %.2f  | beats %lu  breaths %lu\n",
                    v.hr_bpm, v.spo2_pct, v.skin_temp_c, v.perspiration_pct, v.resp_bpm, v.g_force,
                    (unsigned long)beatCount, (unsigned long)breath.breaths);
    }
  }
}
