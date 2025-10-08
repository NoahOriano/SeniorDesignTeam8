
"""
Temperature Monitor UI (HTTP client) for ESP32 firmware exposing /temp JSON
- Polls http://<host>[:port]/temp and updates readings/plot
- Compatible with the ESP32 sketch that registers:
    server.on("/temp", ... JSON: {"en1":bool,"en2":bool,"c1":<float|null>,"c2":<float|null>,"shown":<float|null>,"ip":"x.x.x.x"} )

Usage:
    python main_http.py --host 192.168.1.50 --port 80 --history 300 --interval 0.5
    # or, with mDNS:
    python main_http.py --host esp-temp.local --port 80
"""
import argparse
import json
import queue
import threading
import time
from collections import defaultdict, deque
import math

import tkinter as tk
from tkinter import ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# HTTP
import socket
try:
    import requests
except Exception:
    requests = None


def resolve_mdns(host):
    # Try OS resolution first (supports .local on most systems)
    try:
        info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        if info:
            return info[0][4][0]
    except Exception:
        pass
    return host


class HTTPPollerThread(threading.Thread):
    def __init__(self, host, port, path, data_q, status_q, trigger_alert_callback,
                 max_temp_threshold=None, min_temp_threshold=None, interval=0.5, timeout=5.0):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.path = path
        self.data_q = data_q
        self.status_q = status_q
        self.trigger_alert_callback = trigger_alert_callback
        self.max_temp_threshold = max_temp_threshold
        self.min_temp_threshold = min_temp_threshold
        self.interval = interval
        self.timeout = timeout
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def notify(self, msg):
        try:
            self.status_q.put(msg, block=False)
        except queue.Full:
            pass

    def run(self):
        if requests is None:
            self.notify("requests package not available. Install with: pip install requests")
            return
        base_host = resolve_mdns(self.host)
        url = f"http://{base_host}:{self.port}{self.path}"
        self.notify(f"Polling {url}")
        last_ok = False
        while not self._stop.is_set():
            try:
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
                obj = resp.json()
                # Expect: c1, c2, en1, en2, shown, ip
                now = time.time()
                # Push each sensor as its own sample if present
                if obj.get("c1") is not None:
                    self.data_q.put({"ts": now, "sensor": "S1", "t_c": float(obj["c1"])})
                    self._check_alert("S1", float(obj["c1"]))
                if obj.get("c2") is not None:
                    self.data_q.put({"ts": now, "sensor": "S2", "t_c": float(obj["c2"])})
                    self._check_alert("S2", float(obj["c2"]))
                if not last_ok:
                    self.notify(f"Connected (HTTP {resp.status_code})")
                    last_ok = True
            except Exception as e:
                if last_ok:
                    # mark a disconnect to draw plot gap
                    try:
                        self.status_q.put({"type": "disconnected", "timestamp": time.time()})
                    except queue.Full:
                        pass
                self.notify(f"HTTP error: {e}. Retrying...")
                last_ok = False
            finally:
                time.sleep(self.interval)

    def _check_alert(self, sensor_id, temp_c):
        if self.max_temp_threshold is not None and temp_c > self.max_temp_threshold:
            self.trigger_alert_callback(sensor_id, temp_c, "above max threshold")
        if self.min_temp_threshold is not None and temp_c < self.min_temp_threshold:
            self.trigger_alert_callback(sensor_id, temp_c, "below min threshold")


class TempMonitorClientApp:
    def __init__(self, root, host, port, history_seconds, interval):
        self.root = root
        self.root.title("Temperature Monitor (HTTP client)")
        self.host = host
        self.port = port
        self.interval = interval
        self.history_seconds = history_seconds

        # Temperature unit and plot limits
        self.temp_unit = 'C'
        self.plot_limits = {'C': (10, 50), 'F': (50, 122)}

        # Queues
        self.data_queue = queue.Queue()
        self.status_queue = queue.Queue()

        # Data model
        self.series = defaultdict(lambda: deque(maxlen=history_seconds * 4))
        self.latest = {}

        # Alert settings
        self.max_temp_threshold = None
        self.min_temp_threshold = None
        self.recipient = None
        self.sender_email = None
        self.sender_password = None
        self.last_alert_time = 0

        # Build UI
        self._build_widgets()

        # Start poller
        self.poller = None
        self._start_poller_thread()

        # Poll queues
        self.root.after(100, self._drain_status)
        self.root.after(100, self._drain_data)

    def _start_poller_thread(self):
        if self.poller and self.poller.is_alive():
            self.poller.stop()
        self.poller = HTTPPollerThread(self.host, self.port, "/temp",
                                       self.data_queue, self.status_queue,
                                       trigger_alert_callback=self._trigger_alert_from_reader,
                                       max_temp_threshold=self.max_temp_threshold,
                                       min_temp_threshold=self.min_temp_threshold,
                                       interval=self.interval)
        self.poller.start()

    def _build_widgets(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Label(top, text=f"Connecting to {self.host}:{self.port} (HTTP)", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.unit_button = ttk.Button(top, text=f"Switch from {self.temp_unit}", command=self._toggle_unit)
        self.unit_button.pack(side="right", padx=5)
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
        self.ax.set_xlim(-self.history_seconds, 0)

        self.canvas = FigureCanvasTkAgg(self.fig, master=bottom)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(8, 0))

    def _on_quit(self):
        try:
            self.poller.stop()
        except Exception:
            pass
        self.root.destroy()

    def _toggle_unit(self):
        self.temp_unit = 'F' if self.temp_unit == 'C' else 'C'
        self.unit_button.config(text=f"Switch to {self.temp_unit}")
        self._update_treeview_display()
        self._redraw_plot()

    def _update_treeview_display(self):
        self.tree.delete(*self.tree.get_children())
        for sensor, (t, temp_c) in sorted(self.latest.items()):
            timestr = time.strftime("%H:%M:%S", time.localtime(t))
            if temp_c is None or math.isnan(temp_c):
                temp_display = "N/A"
            else:
                temp_display = f"{(temp_c * 9/5) + 32:.2f}" if self.temp_unit == 'F' else f"{temp_c:.2f}"
                self.tree.heading("temp", text="Temp (°F)" if self.temp_unit == 'F' else "Temp (°C)")
            self.tree.insert("", "end", values=(sensor, temp_display, timestr))

    # Email alert plumbing (same signatures as original)
    def _trigger_alert_from_reader(self, sensor, temp_c, alert_type):
        # no-op here; keep interface for future reuse
        pass

    def _drain_status(self):
        try:
            while True:
                msg = self.status_queue.get_nowait()
                if isinstance(msg, dict) and msg.get("type") == "disconnected":
                    timestamp = msg.get("timestamp", time.time())
                    for sensor in list(self.series.keys()):
                        self.series[sensor].append((timestamp, float('nan')))
                    self._redraw_plot()
                    self.status_var.set(f"Disconnected at {time.strftime('%H:%M:%S', time.localtime(timestamp))}. Reconnecting...")
                else:
                    self.status_var.set(msg)
        except queue.Empty:
            pass
        finally:
            self.root.after(250, self._drain_status)

    def _drain_data(self):
        try:
            updated_sensors = set()
            while True:
                obj = self.data_queue.get_nowait()
                t = obj.get("ts", time.time())
                sensor = str(obj.get("sensor", "S1"))
                temp = float(obj["t_c"])
                self.latest[sensor] = (t, temp)
                dq = self.series[sensor]
                dq.append((t, temp))
                updated_sensors.add(sensor)
        except queue.Empty:
            pass

        self._update_treeview_display()
        self._redraw_plot()
        self.root.after(int(self.interval * 1000), self._drain_data)

    def _redraw_plot(self):
        now = time.time()
        tmin = now - self.history_seconds
        self.ax.clear()
        self.ax.set_xlabel("Time (s, recent)")
        if self.temp_unit == 'F':
            y_min, y_max = 50, 122
            self.ax.set_ylabel("Temperature (°F)")
        else:
            y_min, y_max = 10, 50
            self.ax.set_ylabel("Temperature (°C)")
        self.ax.set_ylim(y_min, y_max)
        self.ax.grid(True, which="both", linestyle="--", alpha=0.4)

        for sensor, dq in sorted(self.series.items()):
            xs, ys = [], []
            for (t, y) in dq:
                if t >= tmin:
                    xs.append(t - now)
                    ys.append(y if y is not None else math.nan)
            if xs:
                if self.temp_unit == 'F':
                    ys_conv = [(y * 9/5) + 32 if y is not None and not math.isnan(y) else math.nan for y in ys]
                else:
                    ys_conv = [y if y is not None and not math.isnan(y) else math.nan for y in ys]
                segx, segy = [], []
                for i in range(len(xs)):
                    if not math.isnan(ys_conv[i]):
                        segx.append(xs[i])
                        segy.append(ys_conv[i])
                    else:
                        if segx:
                            self.ax.plot(segx, segy, label=sensor)
                            segx, segy = [], []
                if segx:
                    self.ax.plot(segx, segy, label=sensor)
        if self.series:
            self.ax.legend(loc="upper left")
        self.canvas.draw_idle()


def main():
    p = argparse.ArgumentParser(description="Temperature Monitor UI (HTTP client)")
    p.add_argument("--host", required=True, help="ESP32 hostname or IP (supports .local if OS provides mDNS)")
    p.add_argument("--port", type=int, default=80, help="HTTP port on device (default: 80)")
    p.add_argument("--history", type=int, default=300, help="History window in seconds (default: 300)")
    p.add_argument("--interval", type=float, default=0.5, help="Polling interval seconds (default: 0.5)")
    args = p.parse_args()

    root = tk.Tk()
    app = TempMonitorClientApp(root, args.host, args.port, args.history, args.interval)
    root.mainloop()


if __name__ == "__main__":
    main()
