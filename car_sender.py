"""
Raspberry Pi telemetry hub.

  1. Gets the driver's vitals from one of:
       thingspeak  the cloud channel the ESP32 driver node writes (15 s per update on the free licence)
       local       the driver node's own /vitals endpoint on the same Wi-Fi (about 1 s old)
       mock        the simulated driver in mock_generator.py
  2. Gets the car data from the mock generator (optionally with brake pressure from real
     transducers on an ADS1115, see --car ads1115).
  3. Merges both into one frame stamped with the Pi's clock and POSTs it to the pit wall
     (server_and_ui.py) at --rate frames per second.

Network trouble never stops the loop: requests time out, failures are counted and logged,
and sending resumes on its own when the link comes back.

Examples
  python car_sender.py --server http://192.168.1.10:8050 --vitals thingspeak
  python car_sender.py --server http://192.168.1.10:8050 --vitals local --esp32 http://f1-driver.local
  python car_sender.py --server http://127.0.0.1:8050 --vitals mock --fault puncture@60
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from mock_generator import MockCar
from telemetry_common import DRIVER_FIELDS, THINGSPEAK_FIELDS, make_frame, sanitize_driver, to_float

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

log = logging.getLogger("car_sender")


# ----------------------------------------------------------------------------- driver sources
class ThingSpeakVitals:
    """Reads the newest entry of the driver channel. ThingSpeak's free licence takes one write per
    channel every 15 s, so there is nothing to gain from polling faster than every 5 s."""

    URL = "https://api.thingspeak.com/channels/{ch}/feeds/last.json"

    def __init__(self, channel_id: str, read_key: str = "", poll_s: float = 5.0, timeout: float = 4.0):
        self.url = self.URL.format(ch=channel_id)
        self.params = {"api_key": read_key} if read_key else {}
        self.poll_s, self.timeout = poll_s, timeout
        self._last_poll = 0.0
        self._values: Dict[str, Optional[float]] = {k: None for k in DRIVER_FIELDS}
        self._created: Optional[float] = None
        self.errors = 0

    @staticmethod
    def parse(entry: Dict[str, Any]) -> Tuple[Dict[str, Optional[float]], Optional[float]]:
        vals = {name: entry.get(f) for f, name in THINGSPEAK_FIELDS.items()}
        created = entry.get("created_at")
        ts = None
        if created:
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
        return sanitize_driver(vals), ts

    def read(self) -> Dict[str, Any]:
        now = time.time()
        if now - self._last_poll >= self.poll_s:
            self._last_poll = now
            try:
                r = requests.get(self.url, params=self.params, timeout=self.timeout)
                r.raise_for_status()
                entry = r.json()
                if isinstance(entry, dict) and entry:
                    self._values, self._created = self.parse(entry)
            except (requests.RequestException, ValueError) as e:
                self.errors += 1
                log.warning("ThingSpeak read failed (%s); keeping the last values", e.__class__.__name__)
        age = (now - self._created) if self._created else None
        return {**self._values, "age_s": None if age is None else round(age, 1)}


class LocalVitals:
    """Reads the driver node directly: GET http://<esp32>/vitals (see the firmware)."""

    def __init__(self, base_url: str, timeout: float = 0.8):
        self.url = base_url.rstrip("/") + "/vitals"
        self.timeout = timeout
        self._values: Dict[str, Optional[float]] = {k: None for k in DRIVER_FIELDS}
        self._got = 0.0
        self._sample_age = 0.0
        self.errors = 0

    def read(self) -> Dict[str, Any]:
        try:
            r = requests.get(self.url, timeout=self.timeout)
            r.raise_for_status()
            j = r.json()
            self._values = sanitize_driver(j)
            self._got = time.time()
            self._sample_age = (to_float(j.get("sample_age_ms")) or 0.0) / 1000.0
        except (requests.RequestException, ValueError) as e:
            self.errors += 1
            log.debug("driver node read failed: %s", e)
        age = (time.time() - self._got + self._sample_age) if self._got else None
        return {**self._values, "age_s": None if age is None else round(age, 1)}


class MockVitals:
    def __init__(self, car: MockCar):
        self.car = car

    def read(self) -> Dict[str, Any]:
        return self.car.driver_dict()


# ----------------------------------------------------------------------------- optional real car sensors
class Ads1115Brakes:
    """Brake line pressure from two 0.5-4.5 V ratiometric transducers on an ADS1115 (A0 front, A1 rear).
    Overwrites the mock brake pressures; everything else stays simulated.
    Needs: pip install adafruit-circuitpython-ads1x15 (and I2C enabled on the Pi)."""

    def __init__(self, full_scale_bar: float = 100.0, divider: float = 1.5):
        import board  # type: ignore
        import busio  # type: ignore
        import adafruit_ads1x15.ads1115 as ADS  # type: ignore
        from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore
        ads = ADS.ADS1115(busio.I2C(board.SCL, board.SDA))
        ads.gain = 1  # +-4.096 V
        self.ch = (AnalogIn(ads, ADS.P0), AnalogIn(ads, ADS.P1))
        self.fs, self.div = full_scale_bar, divider   # divider: 5 V sensor output scaled into the ADC range

    def bar(self, i: int) -> float:
        v = self.ch[i].voltage * self.div
        return round(max(0.0, (v - 0.5) / 4.0 * self.fs), 1)

    def apply(self, car: Dict[str, Any]) -> None:
        car["brakes"]["front_bar"] = self.bar(0)
        car["brakes"]["rear_bar"] = self.bar(1)


# ----------------------------------------------------------------------------- hub
def parse_faults(specs: List[str]) -> List[Tuple[float, str, str]]:
    """--fault puncture@60  --fault overheat:RL@90  --fault hr_spike@30"""
    out = []
    for s in specs:
        what, _, at = s.partition("@")
        kind, _, wheel = what.partition(":")
        out.append((float(at or 0), kind, wheel or "FR"))
    return sorted(out)


class Hub:
    def __init__(self, server: str, vitals_mode: str, vitals_src, car: MockCar, rate: float,
                 car_hw: Optional[Ads1115Brakes] = None, faults=(), driver_name: str = "DRIVER"):
        self.url = server.rstrip("/") + "/api/telemetry"
        self.vitals_mode, self.vitals = vitals_mode, vitals_src
        self.car, self.car_hw = car, car_hw
        self.period = 1.0 / rate
        self.faults = list(faults)
        self.driver_name = driver_name
        self.session = requests.Session()
        self.seq = 0
        self.sent = self.failed = 0
        self.latency_ms: List[float] = []
        self.running = True

    def sources(self) -> Dict[str, Any]:
        sensor = self.vitals_mode in ("thingspeak", "local")
        fields = {k: ("sensor" if sensor else "sim") for k in DRIVER_FIELDS}
        car_fields = {"tyres": "sim", "brakes": "sensor" if self.car_hw else "sim", "speed": "sim"}
        return {"driver": self.vitals_mode, "car": "ads1115+mock" if self.car_hw else "mock",
                "fields": {**fields, **car_fields}}

    def build_frame(self) -> Dict[str, Any]:
        car = self.car.car_dict()
        if self.car_hw:
            self.car_hw.apply(car)
        driver = self.vitals.read()
        mode = "LIVE" if self.vitals_mode in ("thingspeak", "local") else "SIM"
        session = self.car.session_dict(mode)
        session["driver"] = self.driver_name
        self.seq += 1
        return make_frame(self.seq, session, car, driver, self.sources())

    def send(self, frame: Dict[str, Any]) -> bool:
        t0 = time.perf_counter()
        try:
            r = self.session.post(self.url, json=frame, timeout=1.0)
            r.raise_for_status()
        except requests.RequestException as e:
            self.failed += 1
            if self.failed in (1, 10) or self.failed % 100 == 0:
                log.warning("send failed (%d so far): %s", self.failed, e.__class__.__name__)
            return False
        self.sent += 1
        self.latency_ms.append((time.perf_counter() - t0) * 1000)
        return True

    def run(self, duration: Optional[float] = None) -> None:
        t_start = time.monotonic()
        next_t = t_start
        last_report = t_start
        last = t_start
        while self.running:
            now = time.monotonic()
            self.car.step(now - last)
            last = now
            while self.faults and self.faults[0][0] <= now - t_start:
                _, kind, wheel = self.faults.pop(0)
                log.info("injecting fault %s %s", kind, wheel)
                self.car.inject(kind, wheel)
            self.send(self.build_frame())
            if now - last_report >= 10:
                lat = sorted(self.latency_ms) or [0.0]
                log.info("sent %d, failed %d, median latency %.0f ms, p95 %.0f ms",
                         self.sent, self.failed, lat[len(lat) // 2], lat[int(len(lat) * 0.95) - 1 if len(lat) > 1 else 0])
                self.latency_ms.clear()
                last_report = now
            if duration and now - t_start >= duration:
                break
            next_t += self.period
            time.sleep(max(0.0, next_t - time.monotonic()))
            if time.monotonic() - next_t > 1.0:          # fell behind (e.g. long timeout): resync
                next_t = time.monotonic()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Raspberry Pi telemetry hub")
    ap.add_argument("--server", default=os.getenv("DASH_SERVER_URL", "http://127.0.0.1:8050"))
    ap.add_argument("--vitals", choices=("thingspeak", "local", "mock"), default="thingspeak")
    ap.add_argument("--esp32", default=os.getenv("ESP32_URL", "http://f1-driver.local"),
                    help="driver node base URL for --vitals local")
    ap.add_argument("--channel", default=os.getenv("THINGSPEAK_CHANNEL_ID", ""))
    ap.add_argument("--read-key", default=os.getenv("THINGSPEAK_READ_KEY", ""))
    ap.add_argument("--car", choices=("mock", "ads1115"), default="mock")
    ap.add_argument("--rate", type=float, default=2.0, help="frames per second")
    ap.add_argument("--speedup", type=float, default=1.0, help="run the simulated car faster than real time")
    ap.add_argument("--fault", action="append", default=[], help="kind[:wheel]@seconds, e.g. puncture:FR@60")
    ap.add_argument("--driver-name", default=os.getenv("DRIVER_NAME", "DRIVER"))
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    car = MockCar()
    if a.speedup != 1.0:
        step = car.step
        car.step = lambda dt: step(dt * a.speedup)  # type: ignore[method-assign]

    if a.vitals == "thingspeak":
        if not a.channel:
            ap.error("--vitals thingspeak needs THINGSPEAK_CHANNEL_ID (in .env) or --channel")
        vitals = ThingSpeakVitals(a.channel, a.read_key)
    elif a.vitals == "local":
        vitals = LocalVitals(a.esp32)
    else:
        vitals = MockVitals(car)

    car_hw = Ads1115Brakes() if a.car == "ads1115" else None
    hub = Hub(a.server, a.vitals, vitals, car, a.rate, car_hw, parse_faults(a.fault), a.driver_name)

    def stop(*_):
        hub.running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log.info("sending to %s at %.1f Hz, driver vitals from %s", hub.url, a.rate, a.vitals)
    hub.run(a.duration)
    log.info("stopped: sent %d, failed %d", hub.sent, hub.failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
