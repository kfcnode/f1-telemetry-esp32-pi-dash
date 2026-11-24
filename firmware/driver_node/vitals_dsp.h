// Signal processing for the driver node. Plain C++ with no Arduino dependencies,
// so it is unit-tested on a PC: tests/fw/test_dsp.cpp (run by pytest).
#pragma once

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace dsp {

// ---------------------------------------------------------------- basics
struct Ema {
  float alpha, y = 0.0f;
  bool primed = false;
  explicit Ema(float a) : alpha(a) {}
  float update(float x) {
    if (!primed) { y = x; primed = true; } else { y += alpha * (x - y); }
    return y;
  }
};

inline bool in_range(float v, float lo, float hi) { return !isnan(v) && v >= lo && v <= hi; }
inline float valid_or_nan(float v, float lo, float hi) { return in_range(v, lo, hi) ? v : NAN; }
inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

// ---------------------------------------------------------------- skin temperature (NTC thermistor)
// Divider: 3V3 -- R_series -- ADC pin -- NTC -- GND.  v_mv = voltage at the pin.
inline float ntc_celsius(float v_mv, float vcc_mv, float r_series, float r0, float t0_c, float beta) {
  if (!(v_mv > 1.0f) || !(v_mv < vcc_mv - 1.0f)) return NAN;      // open or shorted sensor
  float r_ntc = r_series * v_mv / (vcc_mv - v_mv);
  float inv_t = 1.0f / (t0_c + 273.15f) + logf(r_ntc / r0) / beta;
  return 1.0f / inv_t - 273.15f;
}

// ---------------------------------------------------------------- perspiration (GSR module)
// Galvanic skin response modules output a LOWER voltage as the skin gets wetter (lower resistance).
// The index is relative: 0 % = the driest reading seen in the calibration window after power-up,
// 100 % = `span_mv` below it. It is an index, not a physical unit.
struct GsrIndex {
  float span_mv;
  uint32_t calib_samples;
  uint32_t n = 0;
  float baseline = 0.0f;
  Ema smooth{0.1f};
  explicit GsrIndex(float span = 900.0f, uint32_t calib = 250) : span_mv(span), calib_samples(calib) {}
  float update(float v_mv) {
    float v = smooth.update(v_mv);
    if (n < calib_samples) { n++; if (v > baseline) baseline = v; return NAN; }
    if (v > baseline) baseline += 0.002f * (v - baseline);            // drift up slowly if it gets drier
    if (baseline < 50.0f) return NAN;                                 // module not connected
    return clampf(100.0f * (baseline - v) / span_mv, 0.0f, 100.0f);
  }
  bool calibrating() const { return n < calib_samples; }
};

// ---------------------------------------------------------------- respiration (stretch / piezo belt)
// Feed one sample at a fixed rate. Removes the slow baseline, tracks the breathing amplitude and counts
// a breath at each rising crossing of +threshold (Schmitt trigger, so noise around zero is ignored).
struct BreathDetector {
  float fs_hz;
  float min_amp;                     // below this the belt is off or not breathing: no rate
  Ema lp{0.25f}, base{0.01f}, env{0.02f};
  bool high = false;
  uint32_t n = 0, last_breath = 0;
  uint32_t intervals[5] = {0};
  uint8_t n_int = 0, head = 0;
  uint32_t breaths = 0;

  BreathDetector(float fs, float min_amplitude) : fs_hz(fs), min_amp(min_amplitude) {}

  // returns true when a breath was detected on this sample
  bool update(float x) {
    n++;
    float f = lp.update(x);
    float d = f - base.update(f);
    float e = env.update(fabsf(d));
    if (n < (uint32_t)(fs_hz * 4)) return false;                    // let the filters settle
    float thr = 0.4f * e;
    bool beat = false;
    if (!high && d > thr && e > min_amp) {
      high = true;
      uint32_t gap = n - last_breath;
      if (last_breath && gap > (uint32_t)(fs_hz * 1.2f)) {           // max 50 breaths/min
        intervals[head] = gap; head = (head + 1) % 5; if (n_int < 5) n_int++;
      }
      if (!last_breath || gap > (uint32_t)(fs_hz * 1.2f)) { last_breath = n; breaths++; beat = true; }
    } else if (high && d < -thr) {
      high = false;
    }
    return beat;
  }

  // breaths per minute, NAN if not enough recent breaths
  float rate() const {
    if (n_int < 2 || env.y < min_amp) return NAN;
    if (n - last_breath > (uint32_t)(fs_hz * 12)) return NAN;          // no breath for 12 s
    uint32_t sum = 0;
    for (uint8_t i = 0; i < n_int; i++) sum += intervals[i];
    return 60.0f * fs_hz * n_int / (float)sum;
  }
};

// ---------------------------------------------------------------- G-force (MPU6050, +-8 g range)
inline float g_magnitude(int16_t ax, int16_t ay, int16_t az, float lsb_per_g = 4096.0f) {
  float x = ax / lsb_per_g, y = ay / lsb_per_g, z = az / lsb_per_g;
  return sqrtf(x * x + y * y + z * z);
}

// Peak hold: the upload reports the highest G seen since the previous upload.
struct PeakHold {
  float peak = 0.0f;
  void update(float v) { if (!isnan(v) && v > peak) peak = v; }
  float take() { float p = peak; peak = 0.0f; return p; }
};

// ---------------------------------------------------------------- output formatting
inline int fmt_num(char* out, size_t n, float v, int decimals) {
  if (isnan(v)) return snprintf(out, n, "null");
  return snprintf(out, n, "%.*f", decimals, (double)v);
}

struct Vitals {
  float hr_bpm = NAN, spo2_pct = NAN, skin_temp_c = NAN, perspiration_pct = NAN, resp_bpm = NAN, g_force = NAN;
};

// JSON for GET /vitals. Keys match telemetry_common.DRIVER_FIELDS on the Python side.
inline int vitals_json(char* out, size_t n, const Vitals& v, uint32_t uptime_ms, uint32_t sample_age_ms,
                       uint32_t beats, int rssi, uint32_t ts_ok, uint32_t ts_fail) {
  char a[6][16];
  fmt_num(a[0], 16, v.hr_bpm, 0);
  fmt_num(a[1], 16, v.spo2_pct, 0);
  fmt_num(a[2], 16, v.skin_temp_c, 2);
  fmt_num(a[3], 16, v.perspiration_pct, 1);
  fmt_num(a[4], 16, v.resp_bpm, 1);
  fmt_num(a[5], 16, v.g_force, 2);
  return snprintf(out, n,
                  "{\"hr_bpm\":%s,\"spo2_pct\":%s,\"skin_temp_c\":%s,\"perspiration_pct\":%s,"
                  "\"resp_bpm\":%s,\"g_force\":%s,\"uptime_ms\":%lu,\"sample_age_ms\":%lu,\"beats\":%lu,"
                  "\"wifi_rssi\":%d,\"thingspeak_ok\":%lu,\"thingspeak_fail\":%lu,\"node\":\"driver_node\"}",
                  a[0], a[1], a[2], a[3], a[4], a[5], (unsigned long)uptime_ms, (unsigned long)sample_age_ms,
                  (unsigned long)beats, rssi, (unsigned long)ts_ok, (unsigned long)ts_fail);
}

// Body for POST http://api.thingspeak.com/update. Fields without a valid value are left out,
// so ThingSpeak stores a gap instead of a fake number.
inline int thingspeak_body(char* out, size_t n, const char* write_key, const Vitals& v) {
  int len = snprintf(out, n, "api_key=%s", write_key);
  const float vals[6] = {v.hr_bpm, v.spo2_pct, v.skin_temp_c, v.perspiration_pct, v.resp_bpm, v.g_force};
  const int dec[6] = {0, 0, 2, 1, 1, 2};
  for (int i = 0; i < 6 && len > 0 && (size_t)len < n; i++) {
    if (isnan(vals[i])) continue;
    char num[16];
    fmt_num(num, sizeof num, vals[i], dec[i]);
    len += snprintf(out + len, n - len, "&field%d=%s", i + 1, num);
  }
  return len;
}

}  // namespace dsp
