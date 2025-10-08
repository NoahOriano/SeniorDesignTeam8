"""
Fake ESP Device Server (TCP)
--------------------------------
A simple TCP server that mimics device output:
- Accepts client connections on HOST:PORT
- Periodically sends newline-delimited JSON like: {"t_c": 23.7, "sensor":"S1"}\n
- Simulates multiple sensors using a random-walk around a base temperature.

Usage:
  cd Lab1/Code/Laptop
  python test_server.py --host 0.0.0.0 --port 5000 --sensors S1 S2 --hz 2

Then, in another terminal, run your client UI:
  python main.py --host 127.0.0.1 --port 5000

Notes:
- Multiple clients can connect; each gets its own stream.
"""
import argparse
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


class TempData:
    def __init__(self, sensors, base, jitter):
        self.sensors = sensors
        self.base_c = {"S1": 22.0, "S2": 21.7}  # S1 at 22°C, S2 at 21.7°C
        self.drift_c = {s: 0.0 for s in sensors}
        self.jitter = jitter
        self._lock = threading.Lock()
        self.current_temp = {s: self.base_c[s] for s in sensors} # Actual temperature reported by the sensor
        self.last_update_time = time.time()

        # For S1 bump
        self.s1_last_bump_time = time.time()
        self.s1_bump_target_temp = 0.0
        self.s1_bump_start_time = 0.0
        self.s1_bump_duration = 300.0  # Increased duration for slower change (5 minutes)
        self.s1_bump_active = False

        # For S2 bump
        self.s2_last_bump_trigger_time = time.time()
        self.s2_bump_target_temp = 26.0
        self.s2_bump_start_time = 0.0
        self.s2_bump_rise_duration = 15.0 # 15 seconds to rise
        self.s2_bump_fall_duration = 15.0 # 15 seconds to fall
        self.s2_bump_active = False

        self.smoothing_factor = 0.001 # Controls how quickly the sensor temp approaches the target (divided by 10)

    def get_temps(self):
        with self._lock:
            now = time.time()
            delta_time = now - self.last_update_time
            self.last_update_time = now
            temps = {}

            # Check for S1 bump trigger
            if now - self.s1_last_bump_time >= 60.0 and not self.s1_bump_active: # Every minute
                self.s1_last_bump_time = now
                self.s1_bump_start_time = now
                self.s1_bump_target_temp = random.uniform(24.0, 26.0)
                self.s1_bump_active = True

            # Check for S2 bump trigger
            if now - self.s2_last_bump_trigger_time >= 60.0 and not self.s2_bump_active: # Every minute
                self.s2_last_bump_trigger_time = now
                self.s2_bump_start_time = now
                self.s2_bump_active = True

            for sname in self.sensors:
                base_temp = self.base_c[sname]
                current_drift = self.drift_c[sname]

                # Apply random walk for general stability
                current_drift += random.uniform(-self.jitter, self.jitter)
                current_drift = max(-0.5, min(0.5, current_drift)) # Keep drift small for stability
                self.drift_c[sname] = current_drift

                target_temp = base_temp + current_drift

                # Apply S1 bump if active
                if sname == "S1" and self.s1_bump_active:
                    elapsed_time = now - self.s1_bump_start_time
                    if elapsed_time < self.s1_bump_duration:
                        # Calculate bump value using a sine wave for gradual rise and fall
                        progress = elapsed_time / self.s1_bump_duration
                        # Use a sine wave from 0 to pi to get a smooth curve from 0 up to 1 and back to 0
                        bump_factor = math.sin(progress * math.pi)
                        target_temp += (self.s1_bump_target_temp - base_temp) * bump_factor
                    else:
                        self.s1_bump_active = False # End the bump after duration

                # Apply S2 bump if active
                if sname == "S2" and self.s2_bump_active:
                    elapsed_time = now - self.s2_bump_start_time
                    total_s2_bump_duration = self.s2_bump_rise_duration + self.s2_bump_fall_duration
                    if elapsed_time < total_s2_bump_duration:
                        if elapsed_time < self.s2_bump_rise_duration:
                            # Rising phase
                            progress = elapsed_time / self.s2_bump_rise_duration
                            bump_value = (self.s2_bump_target_temp - base_temp) * progress
                        else:
                            # Falling phase
                            progress = (elapsed_time - self.s2_bump_rise_duration) / self.s2_bump_fall_duration
                            bump_value = (self.s2_bump_target_temp - base_temp) * (1 - progress)
                        target_temp += bump_value
                    else:
                        self.s2_bump_active = False # End the bump after duration

                # Slowly move current_temp towards target_temp
                self.current_temp[sname] += (target_temp - self.current_temp[sname]) * self.smoothing_factor * delta_time
                temps[sname] = round(self.current_temp[sname], 2)
            return temps


class HTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/temp":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            temps = self.server.temp_data.get_temps()
            response_data = {
                "c1": temps.get("S1"),
                "c2": temps.get("S2"),
                "ts": time.time()
            }
            self.wfile.write(json.dumps(response_data).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        # Suppress default logging to avoid clutter, or customize if needed
        pass


class ThreadedHTTPServer(HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, temp_data):
        super().__init__(server_address, RequestHandlerClass)
        self.temp_data = temp_data
        self._log_lock = threading.Lock()

    def log(self, msg):
        with self._log_lock:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Fake ESP Device Server (HTTP)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=80, help="Port to listen on (default: 80)")
    # Removed --base argument as it's now fixed in TempData
    parser.add_argument("--jitter", type=float, default=0.01, help="Random-walk step size (default: 0.01 °C)") # Increased jitter slightly
    args = parser.parse_args()

    # main.py expects S1 and S2
    temp_data = TempData(sensors=["S1", "S2"], base=22.0, jitter=args.jitter) # Base is now fixed in TempData

    server = ThreadedHTTPServer((args.host, args.port), HTTPRequestHandler, temp_data)
    server.log(f"Serving fake temps on http://{args.host}:{args.port}/temp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.log("Shutting down...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
