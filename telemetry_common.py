"""
Shared definitions for the telemetry pipeline: frame layout, sanity checks,
alert thresholds and the alert engine (with hysteresis).

Used by car_sender.py (Raspberry Pi hub), server_and_ui.py (pit wall) and the tests.

Frame layout (JSON, one per update, about 2 per second):

{
  "ts": 1732450000.12,            # Pi clock, the master clock for the whole frame
  "seq": 1234,
  "session": {"car": "07", "driver": "PARIKSHIT", "lap": 7, "lap_frac": 0.42, "mode": "LIVE"},
  "car": {
    "speed_kmh": 212.0, "g_lat": -1.8, "g_long": 0.4,
    "tyres":  {"FL": {"pressure_psi": 21.3, "temp_c": 96.0, "wear_pct": 31.0}, "FR": {...}, "RL": {...}, "RR": {...}},
    "brakes": {"front_bar": 31.0, "rear_bar": 54.0, "disc_front_c": 640.0, "disc_rear_c": 520.0}
  },
  "driver": {"hr_bpm": 88, "spo2_pct": 97, "skin_temp_c": 35.7, "perspiration_pct": 42.0,
             "resp_bpm": 22, "g_force": 1.9, "age_s": 3.1},
  "sources": {"driver": "thingspeak", "car": "mock", "fields": {"hr_bpm": "sensor", "resp_bpm": "sensor", ...}}
}

Any driver value may be null: no sensor, or a reading that failed the sanity check.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

WHEELS = ("FL", "FR", "RL", "RR")

DRIVER_FIELDS = ("hr_bpm", "spo2_pct", "skin_temp_c", "perspiration_pct", "resp_bpm", "g_force")

# ThingSpeak channel layout written by firmware/driver_node/driver_node.ino
THINGSPEAK_FIELDS = {
    "field1": "hr_bpm",
    "field2": "spo2_pct",
    "field3": "skin_temp_c",
    "field4": "perspiration_pct",
    "field5": "resp_bpm",
    "field6": "g_force",
}

# Physically plausible ranges. Anything outside is treated as a sensor glitch and dropped (None),
# never shown as a real value. Consumer PPG modules produce exactly this kind of glitch
# (heart rate 0 or 230, SpO2 above 100 %) when the finger moves.
PLAUSIBLE = {
    "hr_bpm": (30.0, 220.0),
    "spo2_pct": (70.0, 100.0),
    "skin_temp_c": (20.0, 42.0),
    "perspiration_pct": (0.0, 100.0),
    "resp_bpm": (4.0, 60.0),
    "g_force": (0.0, 16.0),
}


def to_float(v: Any) -> Optional[float]:
    """ThingSpeak returns strings or null. Convert, rejecting blanks and NaN."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def sanitize_driver(values: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Return every DRIVER_FIELDS key, with implausible or missing values set to None."""
    out: Dict[str, Optional[float]] = {}
    for k in DRIVER_FIELDS:
        f = to_float(values.get(k))
        lo, hi = PLAUSIBLE[k]
        out[k] = f if f is not None and lo <= f <= hi else None
    return out


def get_path(frame: Dict[str, Any], path: str) -> Any:
    """get_path(frame, "car.tyres.FL.temp_c")"""
    cur: Any = frame
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


# ----------------------------------------------------------------------------- alerts
@dataclass
class Rule:
    """One threshold with hysteresis: raised when the value crosses `on`, cleared only when it
    comes back past `off`. That stops an alert flickering while a value sits on the threshold."""
    key: str
    path: str
    above: bool          # True: alert when value > on.  False: alert when value < on.
    on: float
    off: float
    level: str           # "WARN" or "CRIT"
    text: str            # message, {v} = value, {w} = wheel

    def tripped(self, v: float, active: bool) -> bool:
        if self.above:
            return v > self.off if active else v > self.on
        return v < self.off if active else v < self.on


def _wheel_rules(name, field_, above, on, off, level, text):
    return [Rule(f"{name}_{w}", f"car.tyres.{w}.{field_}", above, on, off, level, text.replace("{w}", w))
            for w in WHEELS]


RULES: List[Rule] = [
    *_wheel_rules("tyre_p_low", "pressure_psi", False, 18.0, 19.0, "CRIT", "CRITICAL: LOW PRESSURE {w}: {v:.1f} psi"),
    *_wheel_rules("tyre_t_high", "temp_c", True, 120.0, 112.0, "WARN", "Tyre temp HIGH {w}: {v:.0f} °C"),
    *_wheel_rules("tyre_wear_high", "wear_pct", True, 75.0, 74.0, "WARN", "Tyre wear HIGH {w}: {v:.0f}% used"),
    *_wheel_rules("tyre_wear_crit", "wear_pct", True, 90.0, 89.0, "CRIT", "Tyre wear CRITICAL {w}: {v:.0f}% used"),
    Rule("brake_disc_hot_f", "car.brakes.disc_front_c", True, 950.0, 880.0, "WARN", "Front brake discs HOT: {v:.0f} °C"),
    Rule("brake_disc_hot_r", "car.brakes.disc_rear_c", True, 950.0, 880.0, "WARN", "Rear brake discs HOT: {v:.0f} °C"),
    Rule("brake_p_high", "car.brakes.front_bar", True, 110.0, 100.0, "WARN", "Excessive front brake pressure: {v:.0f} bar"),
    Rule("hr_high", "driver.hr_bpm", True, 140.0, 130.0, "CRIT", "DRIVER PHYSIOLOGICAL ALERT: HR {v:.0f} bpm"),
    Rule("hr_low", "driver.hr_bpm", False, 45.0, 50.0, "WARN", "Driver HR low: {v:.0f} bpm"),
    Rule("spo2_low", "driver.spo2_pct", False, 92.0, 94.0, "CRIT", "Driver SpO2 low: {v:.0f}%"),
    Rule("resp_high", "driver.resp_bpm", True, 30.0, 27.0, "WARN", "Respiration HIGH (hyperventilation?): {v:.0f} br/min"),
    Rule("skin_hot", "driver.skin_temp_c", True, 38.0, 37.5, "WARN", "Driver skin temp HIGH: {v:.1f} °C"),
    Rule("g_high", "driver.g_force", True, 6.5, 6.0, "WARN", "High G-force on driver: {v:.1f} g"),
]

DRIVER_STALE_S = 45.0   # ThingSpeak free licence: one update per 15 s, so allow a few missed ones


@dataclass
class Alert:
    key: str
    level: str
    text: str
    since: float


@dataclass
class AlertEngine:
    rules: List[Rule] = field(default_factory=lambda: list(RULES))
    active: Dict[str, Alert] = field(default_factory=dict)
    log: List[Alert] = field(default_factory=list)      # newest last, raised alerts only
    log_max: int = 50

    def update(self, frame: Dict[str, Any], now: Optional[float] = None) -> List[Alert]:
        """Evaluate every rule against a frame. Returns the alerts that are active now."""
        now = time.time() if now is None else now
        for r in self.rules:
            v = to_float(get_path(frame, r.path))
            was = r.key in self.active
            if v is None:                       # no data: keep the current state, don't guess
                continue
            if r.tripped(v, was):
                text = r.text.format(v=v)
                if not was:
                    a = Alert(r.key, r.level, text, now)
                    self.active[r.key] = a
                    self.log.append(a)
                    del self.log[:-self.log_max]
                else:
                    self.active[r.key].text = text
            elif was:
                del self.active[r.key]
        # driver data age
        age = to_float(get_path(frame, "driver.age_s"))
        stale = age is not None and age > DRIVER_STALE_S
        if stale and "driver_stale" not in self.active:
            a = Alert("driver_stale", "WARN", f"Driver data stale: {age:.0f} s old", now)
            self.active["driver_stale"] = a
            self.log.append(a)
        elif not stale:
            self.active.pop("driver_stale", None)
        shown = [a for k, a in self.active.items()
                 if not (k.startswith("tyre_wear_high_") and "tyre_wear_crit_" + k[-2:] in self.active)]
        return sorted(shown, key=lambda a: (a.level != "CRIT", -a.since))


def make_frame(seq: int, session: Dict[str, Any], car: Dict[str, Any], driver: Dict[str, Any],
               sources: Dict[str, Any], ts: Optional[float] = None) -> Dict[str, Any]:
    return {"ts": time.time() if ts is None else ts, "seq": seq, "session": session,
            "car": car, "driver": driver, "sources": sources}
