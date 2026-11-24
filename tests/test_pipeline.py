"""Tests for the Python side: sanity checks, alert hysteresis, the mock model, ThingSpeak parsing
and the hub -> dashboard path end to end (Flask test client, no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import car_sender  # noqa: E402
import server_and_ui  # noqa: E402
from mock_generator import LAP_M, MockCar  # noqa: E402
from telemetry_common import AlertEngine, make_frame, sanitize_driver  # noqa: E402


def frame_with(driver=None, tyres=None):
    car = MockCar(seed=1)
    c = car.car_dict()
    if tyres:
        for w, vals in tyres.items():
            c["tyres"][w].update(vals)
    d = {**car.driver_dict(), **(driver or {})}
    return make_frame(1, car.session_dict(), c, d, {})


# ---------------------------------------------------------------- sanity checks
def test_sanitize_rejects_glitches():
    v = sanitize_driver({"hr_bpm": "230", "spo2_pct": "165", "skin_temp_c": "35.2", "perspiration_pct": None,
                         "resp_bpm": "nan", "g_force": "1.2"})
    assert v == {"hr_bpm": None, "spo2_pct": None, "skin_temp_c": 35.2, "perspiration_pct": None,
                 "resp_bpm": None, "g_force": 1.2}


def test_thingspeak_parse_maps_fields_and_time():
    vals, ts = car_sender.ThingSpeakVitals.parse(
        {"created_at": "2026-01-01T00:00:15Z", "field1": "88", "field2": "97", "field3": "35.7",
         "field4": "40", "field5": "18", "field6": "1.10"})
    assert vals["hr_bpm"] == 88 and vals["resp_bpm"] == 18 and vals["g_force"] == 1.1
    assert ts == 1767225615.0


# ---------------------------------------------------------------- alerts
def test_hr_alert_has_hysteresis():
    eng = AlertEngine()
    assert not eng.update(frame_with({"hr_bpm": 139}))
    assert [a.key for a in eng.update(frame_with({"hr_bpm": 145}))] == ["hr_high"]
    assert [a.key for a in eng.update(frame_with({"hr_bpm": 135}))] == ["hr_high"]   # still above the 130 release
    assert not eng.update(frame_with({"hr_bpm": 128}))


def test_missing_value_keeps_state():
    eng = AlertEngine()
    eng.update(frame_with({"hr_bpm": 150}))
    assert "hr_high" in eng.active
    eng.update(frame_with({"hr_bpm": None}))          # dropout: don't clear, don't raise anything new
    assert "hr_high" in eng.active


def test_low_pressure_and_wear_dedupe():
    eng = AlertEngine()
    act = eng.update(frame_with(tyres={"RL": {"pressure_psi": 12.0}, "FR": {"wear_pct": 95.0}}))
    keys = {a.key for a in act}
    assert "tyre_p_low_RL" in keys and "tyre_wear_crit_FR" in keys
    assert "tyre_wear_high_FR" not in keys            # the CRITICAL alert hides the lesser one
    assert act[0].level == "CRIT"


def test_stale_driver_data():
    eng = AlertEngine()
    assert "driver_stale" in {a.key for a in eng.update(frame_with({"age_s": 120}))}
    assert "driver_stale" not in {a.key for a in eng.update(frame_with({"age_s": 3}))}


# ---------------------------------------------------------------- mock model
def test_stint_is_plausible():
    car = MockCar(seed=3, stint_laps=20)
    peak_disc = peak_glat = 0.0
    while car.lap <= 20:
        car.step(0.5)
        c = car.car_dict()
        peak_disc = max(peak_disc, c["brakes"]["disc_front_c"])
        peak_glat = max(peak_glat, abs(c["g_lat"]))
        assert 60 <= c["speed_kmh"] <= 330
        for w in c["tyres"].values():
            assert 18 < w["pressure_psi"] < 26 and 60 < w["temp_c"] < 125
    assert 600 < peak_disc < 950          # hot but below the fade alert without a fault
    assert 3.5 < peak_glat < 5.5
    wear = car.car_dict()["tyres"]
    assert wear["FL"]["wear_pct"] > wear["RR"]["wear_pct"]       # mostly right-handers: left tyres work harder
    assert 50 < wear["FL"]["wear_pct"] < 95


def test_puncture_drops_pressure():
    car = MockCar(seed=4)
    car.step(30)
    car.inject("puncture", "RL", amount=0.5)
    car.step(30)
    assert car.car_dict()["tyres"]["RL"]["pressure_psi"] < 10
    assert car.car_dict()["tyres"]["FR"]["pressure_psi"] > 20


def test_hr_spike_and_dropout():
    car = MockCar(seed=5)
    car.step(20)
    base = car.driver_dict()["hr_bpm"]
    car.inject("hr_spike", duration=40)
    car.step(30)
    assert car.driver_dict()["hr_bpm"] > base + 30
    car.inject("dropout", duration=5)
    assert car.driver_dict()["hr_bpm"] is None


def test_lap_counter():
    car = MockCar(seed=6)
    while car.lap < 3:
        car.step(1.0)
    assert car.lap == 3 and 0 <= car.s < LAP_M


# ---------------------------------------------------------------- hub -> dashboard
class _FakeSession:
    """Routes the hub's POSTs into the Flask test client instead of the network."""

    def __init__(self, client):
        self.client = client

    def post(self, url, json, timeout):
        r = self.client.post("/api/telemetry", json=json)

        class R:
            status_code = r.status_code

            def raise_for_status(self):
                if r.status_code >= 400:
                    raise car_sender.requests.HTTPError(str(r.status_code))
        return R()


def test_hub_to_dashboard_end_to_end():
    server_and_ui.STORE = server_and_ui.TelemetryStore()
    app = server_and_ui.build_app()
    client = app.server.test_client()
    car = MockCar(seed=8)
    hub = car_sender.Hub("http://pitwall", "mock", car_sender.MockVitals(car), car, rate=50.0,
                         faults=car_sender.parse_faults(["hr_spike@0"]))
    hub.session = _FakeSession(client)
    hub.run(duration=1.0)
    assert hub.sent >= 20 and hub.failed == 0
    j = client.get("/api/latest").get_json()
    assert j["frame"]["seq"] == hub.seq
    assert j["frame"]["sources"]["fields"]["hr_bpm"] == "sim"
    # the dashboard callback renders without errors
    assert client.get("/").status_code == 200


def test_bad_frame_rejected():
    app = server_and_ui.build_app()
    r = app.server.test_client().post("/api/telemetry", json={"hello": 1})
    assert r.status_code == 400


def test_parse_faults():
    assert car_sender.parse_faults(["overheat:RL@90", "puncture@60"]) == [(60.0, "puncture", "FR"), (90.0, "overheat", "RL")]


def test_dashboard_callback_renders_with_and_without_data():
    server_and_ui.STORE = server_and_ui.TelemetryStore()
    app = server_and_ui.build_app()
    client = app.server.test_client()
    client.get("/")                                    # lets Dash finish its setup
    key = next(iter(app.callback_map))
    outputs = [{"id": o.split(".")[0], "property": o.split(".")[1]} for o in key.strip(".").split("...")]
    body = {"output": key, "outputs": outputs, "inputs": [{"id": "tick", "property": "n_intervals", "value": 1}],
            "changedPropIds": ["tick.n_intervals"]}
    r = client.post("/_dash-update-component", json=body)
    assert r.status_code == 200, r.data[:300]          # no data yet: "NO DATA" view
    assert b"NO DATA" in r.data
    car = MockCar(seed=2)
    car.inject("puncture", "RL", amount=2.0)
    car.step(20)
    server_and_ui.STORE.ingest(make_frame(1, car.session_dict("LIVE"), car.car_dict(),
                                          {**car.driver_dict(), "hr_bpm": None}, {"fields": {"hr_bpm": "sensor"}}))
    r = client.post("/_dash-update-component", json=body)
    assert r.status_code == 200, r.data[:300]
    assert b"LOW PRESSURE RL" in r.data and b"SENSOR" in r.data
