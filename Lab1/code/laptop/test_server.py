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
import socket
import socketserver
import threading
import time


class ClientHandler(socketserver.BaseRequestHandler):
    def handle(self):
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self.server.log(f"Client connected: {peer}")
        sock = self.request
        try:
            # Send loop
            interval = 1.0 / max(0.1, self.server.hz)
            # Give each client its own phase so streams aren't identical
            phase = random.random() * 2 * math.pi
            last = time.time()
            while True:
                now = time.time()
                if now - last >= interval:
                    last = now
                    for sname in self.server.sensors:
                        # Random-walk with a tiny sinusoid for prettiness
                        base = self.server.base_c[sname]
                        drift = self.server.drift_c[sname]
                        self.server.drift_c[sname] += random.uniform(-self.server.jitter, self.server.jitter)
                        self.server.drift_c[sname] = max(-1.5, min(1.5, self.server.drift_c[sname]))
                        wave = 0.4 * math.sin(now * 0.15 + phase)
                        t_c = base + drift + wave
                        msg = {"t_c": round(t_c, 2), "sensor": sname, "ts": now}
                        line = (json.dumps(msg) + "\n").encode("utf-8")
                        sock.sendall(line)
                # Non-blocking poll for client liveness
                sock.settimeout(0.0)
                try:
                    if sock.recv(1, socket.MSG_PEEK) == b"":
                        raise ConnectionError("peer closed")
                except (BlockingIOError, InterruptedError):
                    pass
                except socket.error:
                    # If any socket error on peek, consider disconnected
                    raise
                time.sleep(0.01)
        except Exception as e:
            self.server.log(f"Client {peer} disconnected: {e}")
        finally:
            try:
                sock.close()
            except Exception:
                pass


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, sensors, hz, base, jitter):
        super().__init__(server_address, RequestHandlerClass)
        self.sensors = sensors
        self.hz = hz
        self.jitter = jitter
        # Initialize bases and drift per sensor
        self.base_c = {s: base + idx * 0.6 for idx, s in enumerate(sensors)}
        self.drift_c = {s: 0.0 for s in sensors}
        self._log_lock = threading.Lock()

    def log(self, msg):
        with self._log_lock:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Fake ESP Device Server (TCP)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--sensors", nargs="+", default=["S1", "S2"], help="Sensor names")
    parser.add_argument("--hz", type=float, default=2.0, help="Samples per second per sensor (default: 2.0)")
    parser.add_argument("--base", type=float, default=23.5, help="Base temperature in °C (default: 23.5)")
    parser.add_argument("--jitter", type=float, default=0.02, help="Random-walk step size (default: 0.02 °C)")
    args = parser.parse_args()

    server = ThreadedServer((args.host, args.port), ClientHandler,
                            sensors=args.sensors, hz=args.hz, base=args.base, jitter=args.jitter)
    server.log(f"Serving fake temps on {args.host}:{args.port} | sensors={args.sensors} | {args.hz} Hz")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.log("Shutting down...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
