// Host tests for firmware/driver_node/vitals_dsp.h.  Built and run by tests/test_firmware_dsp.py:
//   g++ -std=c++17 -I firmware/driver_node tests/fw/test_dsp.cpp -o test_dsp && ./test_dsp
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>

#include "vitals_dsp.h"

static int failures = 0;
#define CHECK(cond)                                                    \
  do {                                                                 \
    if (!(cond)) { std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); failures++; } \
  } while (0)
#define NEAR(a, b, tol) CHECK(std::fabs((a) - (b)) <= (tol))

static float noise(float amp) { return amp * (2.0f * std::rand() / (float)RAND_MAX - 1.0f); }

static void test_ntc() {
  // at 25 °C the NTC equals the series resistor: half the supply
  NEAR(dsp::ntc_celsius(1650.0f, 3300.0f, 10000, 10000, 25, 3950), 25.0f, 0.01f);
  // 35 °C: R = 10k * exp(3950 * (1/308.15 - 1/298.15)) = 6.507k -> v = 3300 * 6.507 / 16.507
  NEAR(dsp::ntc_celsius(3300.0f * 6.5067f / 16.5067f, 3300.0f, 10000, 10000, 25, 3950), 35.0f, 0.05f);
  CHECK(std::isnan(dsp::ntc_celsius(0.0f, 3300.0f, 10000, 10000, 25, 3950)));      // shorted
  CHECK(std::isnan(dsp::ntc_celsius(3300.0f, 3300.0f, 10000, 10000, 25, 3950)));   // open
}

static void test_breath_rate(float bpm_true) {
  const float fs = 25.0f;
  dsp::BreathDetector b(fs, 15.0f);
  for (int i = 0; i < (int)(fs * 60); i++) {
    float t = i / fs;
    float x = 1500.0f + 0.4f * t + 120.0f * std::sin(2 * M_PI * bpm_true / 60.0f * t) + noise(15.0f);
    b.update(x);
  }
  float r = b.rate();
  std::printf("  breathing %.0f/min -> detected %.1f/min (%lu breaths)\n", bpm_true, r, (unsigned long)b.breaths);
  NEAR(r, bpm_true, 1.0f);
}

static void test_breath_flat_is_nan() {
  dsp::BreathDetector b(25.0f, 15.0f);
  for (int i = 0; i < 25 * 30; i++) b.update(1500.0f + noise(3.0f));
  CHECK(std::isnan(b.rate()));
}

static void test_breath_stops() {
  const float fs = 25.0f;
  dsp::BreathDetector b(fs, 15.0f);
  int i = 0;
  for (; i < (int)(fs * 30); i++) b.update(1500 + 120 * std::sin(2 * M_PI * 0.3 * i / fs));
  CHECK(!std::isnan(b.rate()));
  for (int k = 0; k < (int)(fs * 20); k++, i++) b.update(1500 + noise(2.0f));   // belt removed
  CHECK(std::isnan(b.rate()));
}

static void test_gsr() {
  dsp::GsrIndex g(900.0f, 250);
  for (int i = 0; i < 250; i++) CHECK(std::isnan(g.update(2000.0f + noise(5.0f))));
  CHECK(!g.calibrating());
  float v = 0;
  for (int i = 0; i < 200; i++) v = g.update(2000.0f);
  NEAR(v, 0.0f, 1.5f);
  for (int i = 0; i < 200; i++) v = g.update(1550.0f);   // 450 mV wetter = 50 %
  NEAR(v, 50.0f, 2.0f);
  for (int i = 0; i < 200; i++) v = g.update(500.0f);
  NEAR(v, 100.0f, 0.01f);
}

static void test_g() {
  NEAR(dsp::g_magnitude(0, 0, 4096), 1.0f, 1e-6f);
  NEAR(dsp::g_magnitude(4096 * 3, 4096 * 4, 0), 5.0f, 1e-5f);
  dsp::PeakHold p;
  p.update(1.0f); p.update(2.4f); p.update(NAN); p.update(1.3f);
  NEAR(p.take(), 2.4f, 1e-6f);
  NEAR(p.take(), 0.0f, 1e-6f);
}

static void test_formatting() {
  dsp::Vitals v;
  v.hr_bpm = 88; v.skin_temp_c = 35.714f; v.g_force = 1.234f;
  char buf[512];
  dsp::vitals_json(buf, sizeof buf, v, 1000, 40, 12, -60, 3, 1);
  CHECK(std::strstr(buf, "\"hr_bpm\":88,") != nullptr);
  CHECK(std::strstr(buf, "\"spo2_pct\":null,") != nullptr);
  CHECK(std::strstr(buf, "\"skin_temp_c\":35.71,") != nullptr);
  CHECK(std::strstr(buf, "\"sample_age_ms\":40,") != nullptr);
  std::printf("  %s\n", buf);

  char body[256];
  dsp::thingspeak_body(body, sizeof body, "KEY", v);
  CHECK(std::strcmp(body, "api_key=KEY&field1=88&field3=35.71&field6=1.23") == 0);
  std::printf("  %s\n", body);
}

int main() {
  std::srand(42);
  test_ntc();
  for (float bpm : {8.0f, 14.0f, 20.0f, 32.0f}) test_breath_rate(bpm);
  test_breath_flat_is_nan();
  test_breath_stops();
  test_gsr();
  test_g();
  test_formatting();
  if (failures) { std::printf("%d failure(s)\n", failures); return 1; }
  std::printf("all firmware DSP tests passed\n");
  return 0;
}
