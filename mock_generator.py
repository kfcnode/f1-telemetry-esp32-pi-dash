"""
Mock telemetry: a simple car on a virtual 4.4 km circuit, plus a simulated driver.

Used for everything that can't be measured safely on a bench: tyre wear over a stint,
brake and tyre temperatures, tyre pressure loss, and (when no driver node is running)
the driver's vitals.

Models (deliberately simple, see docs/design_notes.md):
  speed      corner-limited speed profile (braking and acceleration envelopes)
  g-forces   g_long from dv/dt, g_lat = v^2 / R in corners
  brakes     line pressure proportional to deceleration; disc temperature heats with
             brake power and cools with airflow (first-order model)
  tyres      temperature follows load and slip with an 8 s lag; pressure follows
             temperature (ideal gas); wear grows linearly per lap, faster on the more
             loaded tyre and when the tyre runs hot
  driver     heart rate and respiration follow sustained G load and stint fatigue

Faults can be injected: puncture, tyre overheat, brake fade, heart-rate spike,
driver-sensor dropout.

Run on its own to dump a stint:  python mock_generator.py --laps 20 --csv stint.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telemetry_common import WHEELS

G = 9.81
LAP_M = 4400.0
V_MAX = 320 / 3.6          # m/s
A_ACC = 9.0                # m/s^2 (traction-limited average)
A_BRK = 38.0               # m/s^2 (about 3.9 g peak braking)

# (distance of apex along the lap [m], apex speed [km/h], peak lateral g, direction: +1 left / -1 right, half-length [m])
CORNERS = [
    (520, 95, 2.6, -1, 40),
    (980, 185, 4.1, +1, 70),
    (1500, 250, 4.6, -1, 120),
    (2050, 80, 2.3, +1, 35),
    (2600, 150, 3.6, -1, 60),
    (3150, 210, 4.4, -1, 90),
    (3700, 110, 3.0, +1, 50),
    (4200, 170, 3.9, -1, 70),
]

COLD_PSI = 17.6            # set when cold (20 °C); about 22-23 psi at working temperature
AMBIENT_C = 28.0


def _speed_limit(s: float) -> float:
    """Highest speed at lap distance s allowed by the braking/acceleration envelopes of every corner."""
    v = V_MAX
    for d, vk, _r, _dir, _h in CORNERS:
        vc = vk / 3.6
        for dd in (d - LAP_M, d, d + LAP_M):           # wrap around the lap
            gap = dd - s
            if gap >= 0:   # corner ahead: must be able to brake down to vc
                v = min(v, math.sqrt(vc * vc + 2 * A_BRK * gap))
            else:          # corner behind: accelerating out of it
                v = min(v, math.sqrt(vc * vc + 2 * A_ACC * (-gap)))
    return v


def _lateral_g(s: float) -> float:
    """Signed lateral g: rises to the corner's peak at the apex and back (cos^2 shape)."""
    for d, _vk, g_peak, direction, half in CORNERS:
        x = abs(s - d) / half
        if x <= 1.0:
            return direction * g_peak * math.cos(math.pi / 2 * x) ** 2
    return 0.0


@dataclass
class Faults:
    puncture: Dict[str, float] = field(default_factory=dict)     # wheel -> leak rate psi/s
    overheat: Dict[str, float] = field(default_factory=dict)     # wheel -> extra °C target
    brake_fade: float = 0.0                                      # extra disc heating factor
    hr_spike_until: float = -1.0
    dropout_until: float = -1.0


class MockCar:
    """Advance with step(dt); read with car_dict() / session_dict() / driver_dict()."""

    def __init__(self, seed: Optional[int] = None, stint_laps: int = 20,
                 wear_per_lap: Optional[Dict[str, float]] = None):
        self.rng = random.Random(seed)
        self.t = 0.0
        self.s = 0.0                 # distance into the lap
        self.lap = 1
        self.v = _speed_limit(0.0)
        self.g_long = 0.0
        self.g_lat = 0.0
        self.stint_laps = stint_laps
        # % wear per lap; this circuit is mostly right-handers, so the left tyres work harder
        self.wear_per_lap = wear_per_lap or {"FL": 3.9, "FR": 3.3, "RL": 3.4, "RR": 2.9}
        self.wear = {w: 0.0 for w in WHEELS}
        self.tyre_t = {w: 70.0 for w in WHEELS}          # tyres come out of blankets at about 70 °C
        self.leak = {w: 0.0 for w in WHEELS}             # psi lost to punctures
        self.disc_f = 300.0
        self.disc_r = 250.0
        self.brake_f = 0.0
        self.brake_r = 0.0
        self.faults = Faults()
        # driver state
        self.hr = 92.0
        self.resp = 16.0
        self.skin = 34.8
        self.sweat = 12.0
        self._g_avg = 1.0

    # ------------------------------------------------------------------ faults
    def inject(self, kind: str, wheel: str = "FR", duration: float = 20.0, amount: float = 0.0):
        f = self.faults
        if kind == "puncture":
            f.puncture[wheel] = amount or 0.35
        elif kind == "overheat":
            f.overheat[wheel] = amount or 45.0
        elif kind == "brake_fade":
            f.brake_fade = amount or 0.6
        elif kind == "hr_spike":
            f.hr_spike_until = self.t + duration
        elif kind == "dropout":
            f.dropout_until = self.t + duration
        elif kind == "clear":
            self.faults = Faults()
        else:
            raise ValueError(f"unknown fault {kind!r}")

    # ------------------------------------------------------------------ model
    def step(self, dt: float) -> None:
        n = max(1, int(math.ceil(dt / 0.05)))
        h = dt / n
        for _ in range(n):
            self._substep(h)

    def _substep(self, h: float) -> None:
        self.t += h
        v_target = _speed_limit((self.s + self.v * h) % LAP_M)
        dv = max(-A_BRK * h, min(A_ACC * h, v_target - self.v))
        v_new = max(15.0, self.v + dv)
        self.g_long = (v_new - self.v) / h / G
        self.v = v_new
        self.s += self.v * h
        if self.s >= LAP_M:
            self.s -= LAP_M
            self.lap += 1
            for w in WHEELS:
                self.wear[w] = min(100.0, self.wear[w] + self.wear_per_lap[w] * (1 + max(0.0, self.tyre_t[w] - 105) / 40))
        self.g_lat = _lateral_g(self.s) + self.rng.gauss(0, 0.05)

        # brakes
        braking = self.g_long < -0.3
        self.brake_f = min(120.0, 24.0 * -self.g_long) if braking else 0.0
        self.brake_r = 0.55 * self.brake_f
        heat = (1 + self.faults.brake_fade) * 0.066 * self.brake_f * self.v
        cool = (0.02 + 0.0011 * self.v)
        self.disc_f += h * (heat - cool * (self.disc_f - AMBIENT_C))
        self.disc_r += h * (0.75 * heat - cool * (self.disc_r - AMBIENT_C))

        # tyres: load transfer. g_lat > 0 = left-hand corner, loads the RIGHT tyres
        for w in WHEELS:
            outside = (w.endswith("R") and self.g_lat > 0) or (w.endswith("L") and self.g_lat < 0)
            lat = abs(self.g_lat) * (1.3 if outside else 0.7)
            front = w.startswith("F")
            lon = (-self.g_long * (1.3 if front else 0.7)) if self.g_long < 0 else (self.g_long * (0.6 if front else 1.4))
            target = 80 + 5.0 * lat + 4.0 * max(0.0, lon) + 0.04 * self.v + self.faults.overheat.get(w, 0.0)
            target += 0.12 * self.wear[w]                # worn tyres run hotter
            self.tyre_t[w] += h / 8.0 * (target - self.tyre_t[w])
            self.leak[w] = min(COLD_PSI, self.leak[w] + h * self.faults.puncture.get(w, 0.0))

        # driver: responds to sustained load (8 s average), fatigue grows over the stint
        g_tot = math.sqrt(self.g_lat ** 2 + self.g_long ** 2 + 1.0)
        self._g_avg += h / 8.0 * (g_tot - self._g_avg)
        fatigue = min(1.0, (self.lap - 1 + self.s / LAP_M) / self.stint_laps)
        hr_target = 98 + 9 * (self._g_avg - 1) + 16 * fatigue
        if self.t < self.faults.hr_spike_until:
            hr_target += 55
        self.hr += h / 6.0 * (hr_target - self.hr)
        self.resp += h / 10.0 * ((15 + 2.5 * (self._g_avg - 1) + 6 * fatigue + (10 if self.t < self.faults.hr_spike_until else 0)) - self.resp)
        self.skin += h / 120.0 * ((35.0 + 1.6 * fatigue) - self.skin)
        self.sweat += h / 90.0 * ((15 + 60 * fatigue) - self.sweat)

    # ------------------------------------------------------------------ outputs
    def tyre_pressure(self, w: str) -> float:
        p = COLD_PSI * (self.tyre_t[w] + 273.15) / (20 + 273.15)
        return max(0.0, p - self.leak[w] * (self.tyre_t[w] + 273.15) / (20 + 273.15))

    def car_dict(self) -> Dict:
        n = self.rng.gauss
        return {
            "speed_kmh": round(self.v * 3.6, 1),
            "g_lat": round(self.g_lat, 2),
            "g_long": round(self.g_long, 2),
            "tyres": {w: {"pressure_psi": round(self.tyre_pressure(w) + n(0, 0.03), 2),
                          "temp_c": round(self.tyre_t[w] + n(0, 0.4), 1),
                          "wear_pct": round(self.wear[w] + self.wear_per_lap[w] * self.s / LAP_M, 1)}
                      for w in WHEELS},
            "brakes": {"front_bar": round(self.brake_f, 1), "rear_bar": round(self.brake_r, 1),
                       "disc_front_c": round(self.disc_f), "disc_rear_c": round(self.disc_r)},
        }

    def driver_dict(self) -> Dict:
        if self.t < self.faults.dropout_until:
            return {"hr_bpm": None, "spo2_pct": None, "skin_temp_c": None, "perspiration_pct": None,
                    "resp_bpm": None, "g_force": None, "age_s": 0.0}
        n = self.rng.gauss
        return {
            "hr_bpm": round(self.hr + n(0, 1.2)),
            "spo2_pct": round(min(100.0, 97.6 + n(0, 0.5))),
            "skin_temp_c": round(self.skin + n(0, 0.03), 1),
            "perspiration_pct": round(self.sweat + n(0, 0.4), 1),
            "resp_bpm": round(self.resp + n(0, 0.4)),
            "g_force": round(math.sqrt(self.g_lat ** 2 + self.g_long ** 2 + 1.0), 2),
            "age_s": 0.0,
        }

    def session_dict(self, mode: str = "SIM") -> Dict:
        return {"car": "07", "driver": "DRIVER", "lap": self.lap, "lap_frac": round(self.s / LAP_M, 3),
                "stint_laps": self.stint_laps, "mode": mode}


def main() -> None:
    ap = argparse.ArgumentParser(description="Dump a simulated stint to CSV.")
    ap.add_argument("--laps", type=int, default=20)
    ap.add_argument("--rate", type=float, default=5.0, help="samples per simulated second")
    ap.add_argument("--csv", default="stint.csv")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--puncture-lap", type=float, default=None, help="inject a slow puncture on FR at this lap")
    a = ap.parse_args()

    car = MockCar(seed=a.seed, stint_laps=a.laps)
    rows: List[Dict] = []
    punctured = False
    while car.lap <= a.laps:
        car.step(1.0 / a.rate)
        if a.puncture_lap and not punctured and car.lap + car.s / LAP_M >= a.puncture_lap:
            car.inject("puncture", "FR", amount=0.05)
            punctured = True
        c, d = car.car_dict(), car.driver_dict()
        row = {"t": round(car.t, 2), "lap": car.lap + car.s / LAP_M, "speed_kmh": c["speed_kmh"],
               "g_lat": c["g_lat"], "g_long": c["g_long"], "brake_front_bar": c["brakes"]["front_bar"],
               "disc_front_c": c["brakes"]["disc_front_c"], "disc_rear_c": c["brakes"]["disc_rear_c"]}
        for w in WHEELS:
            for k, v in c["tyres"][w].items():
                row[f"{w}_{k}"] = v
        row.update({k: v for k, v in d.items() if k != "age_s"})
        rows.append(row)
    with open(a.csv, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    print(f"{len(rows)} rows, {car.t / 60:.1f} simulated minutes -> {a.csv}")


if __name__ == "__main__":
    main()
