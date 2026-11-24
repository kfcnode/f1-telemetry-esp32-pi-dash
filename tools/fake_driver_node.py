"""Pretends to be the ESP32 driver node's local endpoint, so the hub's `--vitals local` path can be
tested without hardware. Serves GET /vitals with the same JSON keys as the firmware.

    python tools/fake_driver_node.py --port 8081
    python car_sender.py --vitals local --esp32 http://127.0.0.1:8081
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mock_generator import MockCar  # noqa: E402

CAR = MockCar(seed=11)
LOCK = threading.Lock()
START = time.monotonic()


def ticker():
    last = time.monotonic()
    while True:
        time.sleep(0.1)
        now = time.monotonic()
        with LOCK:
            CAR.step(now - last)
        last = now


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/vitals":
            self.send_error(404)
            return
        with LOCK:
            d = CAR.driver_dict()
        body = {k: v for k, v in d.items() if k != "age_s"}
        body.update({"uptime_ms": int((time.monotonic() - START) * 1000), "sample_age_ms": 40,
                     "beats": 0, "wifi_rssi": -55, "node": "fake"})
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    a = ap.parse_args()
    threading.Thread(target=ticker, daemon=True).start()
    print(f"fake driver node on http://127.0.0.1:{a.port}/vitals")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
