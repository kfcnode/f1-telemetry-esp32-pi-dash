// Pins, rates and calibration for the driver node. Wiring: docs/wiring.md
#pragma once

// ---- pins (analog inputs on ADC1 only: ADC2 can't be read while Wi-Fi is on)
#define PIN_I2C_SDA        21      // MAX30100 + MPU6050
#define PIN_I2C_SCL        22
#define PIN_RESP_BELT      34      // stretch / piezo respiration belt (ADC1_CH6, input only)
#define PIN_SKIN_NTC       35      // 10k NTC divider (ADC1_CH7, input only)
#define PIN_GSR            32      // GSR module analog out (ADC1_CH4)
#define PIN_STATUS_LED     2       // on-board LED: blinks on each heartbeat

// ---- timing
#define SAMPLE_HZ          25      // analog channels + IMU
#define UPLOAD_PERIOD_MS   20000   // ThingSpeak free licence: at most one write per 15 s per channel
#define SERIAL_PERIOD_MS   1000
#define WIFI_RETRY_MS      5000

// ---- skin temperature: 10k NTC, B = 3950, 10k series resistor to 3V3
#define NTC_R_SERIES       10000.0f
#define NTC_R0             10000.0f
#define NTC_T0_C           25.0f
#define NTC_BETA           3950.0f
#define VCC_MV             3300.0f

// ---- perspiration index: dry baseline taken in the first 10 s after power-up
#define GSR_SPAN_MV        900.0f   // drop from the dry baseline that counts as 100 %
#define GSR_CALIB_SAMPLES  250      // 10 s at 25 Hz

// ---- respiration: minimum breathing amplitude (mV at the ADC) to report a rate
#define RESP_MIN_AMP_MV    15.0f

// ---- MAX30100
#define PPG_BEAT_TIMEOUT_MS 5000    // no beat for 5 s: report heart rate and SpO2 as missing

// ---- mDNS name: the Pi can use http://f1-driver.local/vitals
#define MDNS_NAME          "f1-driver"
