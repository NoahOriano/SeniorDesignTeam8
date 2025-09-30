"""
Monitor UI (TCP Client) for ATmega328P + ESP32 (ESP-AT as TCP server) temperature sensing device
- Connects to the device (host:port) and reads newline-delimited JSON:
    {"t_c": 23.5, "sensor":"S1"}\n
- Displays live values and a rolling plot per sensor.

Usage:
    python main.py --host 192.168.1.50 --port 5000 --history 300
    # or, with mDNS if your device announces "esp-temp.local":
    python main.py --host esp-temp.local --port 5000

Requires: matplotlib, zeroconf (optional, for .local names)
"""
import argparse
import json
import queue
import socket
import threading
import time
from collections import defaultdict, deque

import tkinter as tk
from tkinter import ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


def resolve_mdns(host):
    # Try to resolve .local names using basic getaddrinfo first.
    try:
        info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        if info:
            return info[0][4][0]
    except Exception:
        pass
    # Optionally attempt zeroconf if installed.
    try:
        from zeroconf import Zeroconf
        zc = Zeroconf()
        try:
            # Zeroconf doesn't directly resolve hostnames; we can return host as-is;
            # many OSes already resolve .local via mDNSResponder/Avahi.
            return host
        finally:
            zc.close()
    except Exception:
        return host


class ReaderThread(threading.Thread):
    def __init__(self, host, port, data_q, status_q, reconnect_delay=3):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.data_q = data_q
        self.status_q = status_q
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def notify(self, msg):
        try:
            self.status_q.put(msg, block=False)
        except queue.Full:
            pass

    def run(self):
        while not self._stop.is_set():
            try:
                host_ip = resolve_mdns(self.host)
                self.notify(f"Connecting to {self.host} ({host_ip}):{self.port} ...")
                with socket.create_connection((host_ip, self.port), timeout=10) as sock:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.notify("Connected")
                    buf = b""
                    while not self._stop.is_set():
                        chunk = sock.recv(4096)
                        if not chunk:
                            raise ConnectionError("Peer closed")
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line.decode("utf-8", errors="replace"))
                                if "t_c" in obj:
                                    if "ts" not in obj:
                                        obj["ts"] = time.time()
                                    if "sensor" not in obj:
                                        obj["sensor"] = "S1"
                                    self.data_q.put(obj)
                                else:
                                    self.notify(f"Skipped line (no t_c): {line[:80]!r}")
                            except json.JSONDecodeError:
                                self.notify(f"Bad JSON: {line[:80]!r}")
            except Exception as e:
                self.notify(f"Disconnected: {e}. Reconnecting in {self.reconnect_delay}s ...")
                time.sleep(self.reconnect_delay)


class TempMonitorClientApp:
    def __init__(self, root, host, port, history_seconds):
        self.root = root
        self.root.title("Temperature Monitor (TCP client)")
        self.host = host
        self.port = port
        self.history_seconds = history_seconds

        # Queues
        self.data_queue = queue.Queue()
        self.status_queue = queue.Queue()

        # Data model
        self.series = defaultdict(lambda: deque(maxlen=history_seconds * 4))
        self.latest = {}
        self.lines = {}

        # Build UI
        self._build_widgets()

        # Start reader
        self.reader = ReaderThread(self.host, self.port, self.data_queue, self.status_queue)
        self.reader.start()

        # Poll queues
        self.root.after(100, self._drain_status)
        self.root.after(100, self._drain_data)

    def _build_widgets(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Label(top, text=f"Connecting to {self.host}:{self.port}", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(top, text="Quit", command=self._on_quit).pack(side="right")

        mid = ttk.LabelFrame(main, text="Current Readings")
        mid.pack(fill="x", pady=(10, 10))
        self.tree = ttk.Treeview(mid, columns=("sensor", "temp", "time"), show="headings", height=5)
        for c, w in (("sensor", 100), ("temp", 120), ("time", 200)):
            self.tree.heading(c, text=c.capitalize() if c != "temp" else "Temp (°C)")
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="x", padx=5, pady=5)

        bottom = ttk.LabelFrame(main, text="Live Plot")
        bottom.pack(fill="both", expand=True)

        self.fig = Figure(figsize=(7, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (s, recent)")
        self.ax.set_ylabel("Temperature (°C)")
        self.ax.grid(True, which="both", linestyle="--", alpha=0.4)

        self.canvas = FigureCanvasTkAgg(self.fig, master=bottom)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(8, 0))

    def _on_quit(self):
        try:
            self.reader.stop()
        except Exception:
            pass
        self.root.destroy()

    def _drain_status(self):
        try:
            while True:
                msg = self.status_queue.get_nowait()
                self.status_var.set(msg)
        except queue.Empty:
            pass
        finally:
            self.root.after(250, self._drain_status)

    def _drain_data(self):
        updated = False
        try:
            while True:
                obj = self.data_queue.get_nowait()
                t = obj.get("ts", time.time())
                sensor = str(obj.get("sensor", "S1"))
                temp = float(obj["t_c"])
                self.latest[sensor] = (t, temp)
                self.series[sensor].append((t, temp))
                updated = True
        except queue.Empty:
            pass

        if updated:
            self.tree.delete(*self.tree.get_children())
            for sensor, (t, temp) in sorted(self.latest.items()):
                timestr = time.strftime("%H:%M:%S", time.localtime(t))
                self.tree.insert("", "end", values=(sensor, f"{temp:.2f}", timestr))
            self._redraw_plot()
        self.root.after(200, self._drain_data)

    def _redraw_plot(self):
        now = time.time()
        tmin = now - self.history_seconds
        self.ax.clear()
        self.ax.set_xlabel("Time (s, recent)")
        self.ax.set_ylabel("Temperature (°C)")
        self.ax.grid(True, which="both", linestyle="--", alpha=0.4)
        for sensor, dq in sorted(self.series.items()):
            xs, ys = [], []
            for (t, y) in dq:
                if t >= tmin:
                    xs.append(t - now)
                    ys.append(y)
            if xs:
                self.ax.plot(xs, ys, label=sensor)
        if self.series:
            self.ax.legend(loc="upper left")
        self.canvas.draw_idle()


def main():
    parser = argparse.ArgumentParser(description="Temperature Monitor UI (TCP client)")
    parser.add_argument("--host", required=True, help="Device hostname or IP (supports .local if OS provides mDNS)")
    parser.add_argument("--port", type=int, default=5000, help="TCP port on device (default: 5000)")
    parser.add_argument("--history", type=int, default=300, help="History window in seconds (default: 300)")
    args = parser.parse_args()

    root = tk.Tk()
    app = TempMonitorClientApp(root, args.host, args.port, args.history)
    root.mainloop()


if __name__ == "__main__":
    main()
