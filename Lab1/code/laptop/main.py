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
from email_handler import EmailHandler
import json
import queue
import socket
import threading
import time
from collections import defaultdict, deque
import math # Import math module for isnan

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
    def __init__(self, host, port, data_q, status_q, trigger_alert_callback, max_temp_threshold=None, min_temp_threshold=None, reconnect_delay=3):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.data_q = data_q
        self.status_q = status_q
        self.trigger_alert_callback = trigger_alert_callback # Callback to trigger alert in main app
        self.max_temp_threshold = max_temp_threshold
        self.min_temp_threshold = min_temp_threshold
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

                                    # Check for alerts
                                    current_temp_c = float(obj["t_c"])
                                    current_sensor = str(obj.get("sensor", "S1"))
                                    if self.max_temp_threshold is not None and current_temp_c > self.max_temp_threshold:
                                        self.trigger_alert_callback(current_sensor, current_temp_c, "above max threshold")
                                    if self.min_temp_threshold is not None and current_temp_c < self.min_temp_threshold:
                                        self.trigger_alert_callback(current_sensor, current_temp_c, "below min threshold")

                                else:
                                    self.notify(f"Skipped line (no t_c): {line[:80]!r}")
                            except json.JSONDecodeError:
                                self.notify(f"Bad JSON: {line[:80]!r}")
            except Exception as e:
                self.notify(f"Disconnected: {e}. Reconnecting in {self.reconnect_delay}s ...")
                self.status_q.put({"type": "disconnected", "timestamp": time.time()}) # Notify main app of disconnection
                time.sleep(self.reconnect_delay)


class TempMonitorClientApp:
    def __init__(self, root, host, port, history_seconds):
        self.root = root
        self.root.title("Temperature Monitor (TCP client)")
        self.host = host
        self.port = port
        self.history_seconds = history_seconds

        # Temperature unit and plot limits
        self.temp_unit = 'C'
        self.plot_limits = {'C': (10, 50), 'F': (50, 122)} # Requirement 5.c.i

        # Queues
        self.data_queue = queue.Queue()
        self.status_queue = queue.Queue()

        # Data model
        self.series = defaultdict(lambda: deque(maxlen=history_seconds * 4))
        self.latest = {}
        self.lines = {}

        # Sensor states (initially off, as per requirement 4.c)
        self.sensor_states = {'S1': 'off', 'S2': 'off'}

        # Alert settings
        self.max_temp_threshold = None
        self.min_temp_threshold = None
        self.recipient = None
        self.sender_email = None
        self.sender_password = None # Note: Storing passwords directly is insecure. Consider env vars or secure storage.
        self.last_alert_time = 0 # Initialize last alert time for cooldown

        # Build UI
        self._build_widgets()
        
        # Load settings from file after UI is built
        self._load_settings()

        # Start reader (will be re-initialized after settings are saved)
        self.reader = None 
        self._start_reader_thread() # Start the reader thread initially

        # Poll queues
        self.root.after(100, self._drain_status)
        self.root.after(100, self._drain_data)

    def _start_reader_thread(self):
        if self.reader and self.reader.is_alive():
            self.reader.stop()
            # Removed self.reader.join() to prevent UI freeze.
            # The old thread is a daemon and will terminate when the main app exits.
            # Or it will eventually exit its run loop when sock.recv times out or connection breaks.

        self.reader = ReaderThread(self.host, self.port, self.data_queue, self.status_queue,
                                   trigger_alert_callback=self._trigger_alert_from_reader,
                                   max_temp_threshold=self.max_temp_threshold,
                                   min_temp_threshold=self.min_temp_threshold)
        self.reader.start()

    def _build_widgets(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Label(top, text=f"Connecting to {self.host}:{self.port}", font=("Segoe UI", 10, "bold")).pack(side="left")
        # Add unit toggle button
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

        # Alert Settings
        alert_frame = ttk.LabelFrame(main, text="Alert Settings")
        alert_frame.pack(fill="x", pady=(10, 10))

        # Max Temp Threshold
        max_temp_frame = ttk.Frame(alert_frame)
        max_temp_frame.pack(fill="x", pady=2)
        ttk.Label(max_temp_frame, text="Max Temp (°C):", width=15).pack(side="left", padx=5)
        self.max_temp_entry = ttk.Entry(max_temp_frame, width=10)
        self.max_temp_entry.pack(side="left", padx=5)

        # Min Temp Threshold
        min_temp_frame = ttk.Frame(alert_frame)
        min_temp_frame.pack(fill="x", pady=2)
        ttk.Label(min_temp_frame, text="Min Temp (°C):", width=15).pack(side="left", padx=5)
        self.min_temp_entry = ttk.Entry(min_temp_frame, width=10)
        self.min_temp_entry.pack(side="left", padx=5)

        # Recipient
        recipient_frame = ttk.Frame(alert_frame)
        recipient_frame.pack(fill="x", pady=2)
        ttk.Label(recipient_frame, text="Recipient (Email/Phone):", width=15).pack(side="left", padx=5)
        self.recipient_entry = ttk.Entry(recipient_frame, width=30)
        self.recipient_entry.pack(side="left", padx=5)

        # Sender Email
        sender_email_frame = ttk.Frame(alert_frame)
        sender_email_frame.pack(fill="x", pady=2)
        ttk.Label(sender_email_frame, text="Sender Email:", width=15).pack(side="left", padx=5)
        self.sender_email_entry = ttk.Entry(sender_email_frame, width=30)
        self.sender_email_entry.pack(side="left", padx=5)

        # Sender Password
        sender_password_frame = ttk.Frame(alert_frame)
        sender_password_frame.pack(fill="x", pady=2)
        ttk.Label(sender_password_frame, text="Sender Password:", width=15).pack(side="left", padx=5)
        self.sender_password_entry = ttk.Entry(sender_password_frame, width=30)
        self.sender_password_entry.pack(side="left", padx=5)

        # Save Settings Button
        self.save_settings_button = ttk.Button(alert_frame, text="Save Settings", command=self._save_all_settings)
        self.save_settings_button.pack(pady=5)

        self.fig = Figure(figsize=(7, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (s, recent)")
        self.ax.set_ylabel("Temperature (°C)")
        self.ax.grid(True, which="both", linestyle="--", alpha=0.4)
        self.ax.set_xlim(-self.history_seconds, 0) # Force x-axis bounds to -300 to 0

        self.canvas = FigureCanvasTkAgg(self.fig, master=bottom)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(8, 0))

    def _load_settings(self):
        try:
            with open("config.json", "r") as f:
                settings = json.load(f)
                self.max_temp_threshold = settings.get("max_temp_threshold")
                self.min_temp_threshold = settings.get("min_temp_threshold")
                self.recipient = settings.get("recipient_email")
                self.sender_email = settings.get("sender_email")
                self.sender_password = settings.get("sender_password")

                # Update UI entries with loaded values
                self.max_temp_entry.delete(0, tk.END)
                if self.max_temp_threshold is not None:
                    self.max_temp_entry.insert(0, str(self.max_temp_threshold))
                
                self.min_temp_entry.delete(0, tk.END)
                if self.min_temp_threshold is not None:
                    self.min_temp_entry.insert(0, str(self.min_temp_threshold))

                self.recipient_entry.delete(0, tk.END)
                if self.recipient is not None:
                    self.recipient_entry.insert(0, self.recipient)

                self.sender_email_entry.delete(0, tk.END)
                if self.sender_email is not None:
                    self.sender_email_entry.insert(0, self.sender_email)

                self.sender_password_entry.delete(0, tk.END)
                if self.sender_password is not None:
                    self.sender_password_entry.insert(0, self.sender_password)

            self.notify("Settings loaded from config.json.")
        except FileNotFoundError:
            self.notify("config.json not found. Using default settings.")
        except json.JSONDecodeError:
            self.notify("Error decoding config.json. Using default settings.")
        except Exception as e:
            self.notify(f"Error loading settings: {e}")

    def _save_settings(self):
        settings = {
            "max_temp_threshold": self.max_temp_threshold,
            "min_temp_threshold": self.min_temp_threshold,
            "recipient_email": self.recipient,
            "sender_email": self.sender_email,
            "sender_password": self.sender_password
        }
        try:
            with open("config.json", "w") as f:
                json.dump(settings, f, indent=4)
            self.notify("Settings saved to config.json.")
        except Exception as e:
            self.notify(f"Error saving settings: {e}")

    def _on_quit(self):
        try:
            self.reader.stop()
        except Exception:
            pass
        self.root.destroy()

    def _toggle_unit(self):
        if self.temp_unit == 'C':
            self.temp_unit = 'F'
        else:
            self.temp_unit = 'C'
        self.unit_button.config(text=f"Switch to {self.temp_unit}")
        self._update_treeview_display() # Update treeview immediately
        self._redraw_plot() # Redraw plot with new unit and limits

    def _update_treeview_display(self):
        # Clear and re-populate treeview with current data in the new unit
        self.tree.delete(*self.tree.get_children())
        for sensor, (t, temp_c) in sorted(self.latest.items()):
            timestr = time.strftime("%H:%M:%S", time.localtime(t))
            temp_display = ""
            if temp_c is not None and not float('nan') == temp_c:
                if self.temp_unit == 'F':
                    temp_display = f"{(temp_c * 9/5) + 32:.2f}"
                    self.tree.heading("temp", text="Temp (°F)")
                else: # 'C'
                    temp_display = f"{temp_c:.2f}"
                    self.tree.heading("temp", text="Temp (°C)")
            else:
                temp_display = "N/A" # Display N/A for disconnected data

            self.tree.insert("", "end", values=(sensor, temp_display, timestr))

    def _toggle_sensor(self, sensor_id):
        current_state = self.sensor_states.get(sensor_id, 'off')
        new_state = 'on' if current_state == 'off' else 'off'
        self.sensor_states[sensor_id] = new_state

        # Update button text
        if sensor_id == 'S1':
            self.s1_button.config(text=f"Sensor 1: {new_state.capitalize()}")
        elif sensor_id == 'S2':
            self.s2_button.config(text=f"Sensor 2: {new_state.capitalize()}")

        # Send command to device
        command = {"command": "set_sensor", "sensor": sensor_id, "state": new_state}
        self._send_command(command)

    def _send_command(self, command_data):
        # Placeholder for sending commands. Actual implementation requires socket communication.
        # This will need to be integrated with the ReaderThread or a separate sender.
        command_str = json.dumps(command_data) + '\n'
        self.notify(f"Command to send: {command_str.strip()}")
        # In a real implementation, this would send command_str over the socket.
        # For now, we'll just update the status.
        # The actual sending mechanism needs to be implemented.
        # This is a complex part and might require refactoring ReaderThread.
        # For now, we'll assume the device is listening for commands.
        # If the device is expecting commands on a different port or via a different mechanism,
        # this part would need significant changes.
        # For now, we'll just print the command and update the status bar.

    def _save_all_settings(self):
        try:
            max_temp_str = self.max_temp_entry.get()
            min_temp_str = self.min_temp_entry.get()
            recipient = self.recipient_entry.get()

            # Capture sender email and password
            self.sender_email = self.sender_email_entry.get() if hasattr(self, 'sender_email_entry') else None
            self.sender_password = self.sender_password_entry.get() if hasattr(self, 'sender_password_entry') else None

            if max_temp_str:
                self.max_temp_threshold = float(max_temp_str)
            else:
                self.max_temp_threshold = None

            if min_temp_str:
                self.min_temp_threshold = float(min_temp_str)
            else:
                self.min_temp_threshold = None

            self.recipient = recipient if recipient else None

            if self.max_temp_threshold is not None or self.min_temp_threshold is not None or self.recipient:
                self.notify("Alert settings saved.")
                if not (self.sender_email and self.sender_password and self.recipient):
                    self.notify("Warning: Email alerts are enabled but sender email, password, or recipient is missing. Please check alert settings.")
            else:
                self.notify("Alert settings cleared.")
            
            # Restart reader thread with updated thresholds
            self._start_reader_thread()

        except ValueError:
            self.notify("Invalid input for temperature thresholds. Please enter numbers.")
        except Exception as e:
            self.notify(f"Error saving alert settings: {e}")
        
        self._save_settings() # Save current settings to config.json

    def notify(self, msg):
        """Updates the status bar with a message."""
        self.status_var.set(msg)

    def _trigger_alert_from_reader(self, sensor, temp_c, alert_type):
        """Callback from ReaderThread to trigger an alert in the main app."""
        current_time = time.time()
        if current_time - self.last_alert_time >= 60: # 60-second cooldown
            self.last_alert_time = current_time
            self._send_alert_email(sensor, temp_c, alert_type)
            self.notify(f"UI Alert: {sensor} {alert_type} at {temp_c:.2f}°C. Email sent.")
        else:
            self.notify(f"UI Alert: {sensor} {alert_type} at {temp_c:.2f}°C. Email suppressed (cooldown).")

    def _send_alert_email(self, sensor, temp_c, alert_type):
        """Sends an email alert."""
        message_body = f"ALERT: Sensor {sensor} is {alert_type} at {temp_c:.2f}°C."
        subject = f"Temperature Alert: {sensor} {alert_type.split(' ')[-1]}"

        if self.recipient:
            if self.sender_email and self.sender_password:
                try:
                    email_handler = EmailHandler()
                    if email_handler.send_email(self.sender_email, self.sender_password, self.recipient, subject, message_body):
                        self.notify(f"Alert email sent to {self.recipient}.")
                    else:
                        self.notify(f"Failed to send alert email to {self.recipient}.")
                except Exception as e:
                    self.notify(f"Error during alert email sending: {e}")
            else:
                self.notify("Sender email or password not provided. Cannot send alert email.")
        else:
            self.notify(f"{message_body} (No recipient set).")

    def _drain_status(self):
        try:
            while True:
                msg = self.status_queue.get_nowait()
                if isinstance(msg, dict) and msg.get("type") == "disconnected":
                    # Insert None for all active sensors to create a gap
                    timestamp = msg.get("timestamp", time.time())
                    for sensor in self.series.keys():
                        self.series[sensor].append((timestamp, float('nan')))
                    self._redraw_plot() # Redraw immediately to show the gap
                    self.status_var.set(f"Disconnected at {time.strftime('%H:%M:%S', time.localtime(timestamp))}. Reconnecting...")
                else:
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

        # Always redraw plot to ensure scrolling and gaps are visible
        self._update_treeview_display() # Update treeview with current data in selected unit
        self._redraw_plot()
        self.root.after(200, self._drain_data)

    def _redraw_plot(self):
        now = time.time()
        tmin = now - self.history_seconds
        now = time.time()
        tmin = now - self.history_seconds
        self.ax.clear() # Clear the axes before redrawing
        self.ax.set_xlabel("Time (s, recent)")

        # Set y-axis label and limits based on selected unit
        if self.temp_unit == 'F':
            y_min, y_max = self.plot_limits['F']
            self.ax.set_ylabel("Temperature (°F)")
        else: # 'C'
            y_min, y_max = self.plot_limits['C']
            self.ax.set_ylabel("Temperature (°C)")
        self.ax.set_ylim(y_min, y_max)

        self.ax.grid(True, which="both", linestyle="--", alpha=0.4)
        for sensor, dq in sorted(self.series.items()):
            xs, ys_converted = [], []
            for (t, y) in dq:
                if t >= tmin:
                    xs.append(t - now)
                    if self.temp_unit == 'F':
                        ys_converted.append((y * 9/5) + 32 if y is not None and not math.isnan(y) else math.nan)
                    else: # 'C'
                        ys_converted.append(y if y is not None and not math.isnan(y) else math.nan)
            
            
            # Handle gaps for disconnected data
            if xs:
                # Convert y-values to the selected unit for plotting
                if self.temp_unit == 'F':
                    ys_converted = [(y * 9/5) + 32 if y is not None and not math.isnan(y) else math.nan for y in ys]
                else: # 'C'
                    ys_converted = [y if y is not None and not math.isnan(y) else math.nan for y in ys]
                
                # Plot segments separated by NaN values
                # This creates the desired gaps in the plot
                segment_xs, segment_ys = [], []
                for i in range(len(xs)):
                    if not math.isnan(ys_converted[i]):
                        segment_xs.append(xs[i])
                        segment_ys.append(ys_converted[i])
                    else:
                        if segment_xs: # Plot previous segment if it exists
                            self.ax.plot(segment_xs, segment_ys, label=sensor)
                            segment_xs, segment_ys = [], [] # Reset for next segment
                if segment_xs: # Plot the last segment
                    self.ax.plot(segment_xs, segment_ys, label=sensor)
            
        # Matplotlib can automatically handle unique labels if we just call legend
        if self.series: # Only attempt to draw legend if there's any series data
            self.ax.legend(loc="upper left")
        self.canvas.draw_idle()


def main():
    parser = argparse.ArgumentParser(description="Temperature Monitor UI (TCP client)")
    parser.add_argument("--host", required=True, help="Device hostname or IP (supports .local if OS provides mDNS)")
    parser.add_argument("--port", type=int, default=5000, help="TCP port on device (default: 5000)")
    parser.add_argument("--history", type=int, default=300, help="History window in seconds (default: 300)")
    # Add argument for testing email functionality
    parser.add_argument('--test-email', nargs=3, metavar=('SENDER_EMAIL', 'SENDER_PASSWORD', 'RECIPIENT_EMAIL'),
                        help='Test email functionality: provide sender email, sender password, and recipient email.')
    args = parser.parse_args()

    # If testing email, send the email and exit
    if args.test_email:
        sender_email, sender_password, recipient_email = args.test_email
        try:
            email_handler = EmailHandler()
            subject = "Test Email from Temperature Monitor"
            body = "This is a test email to verify email sending functionality."
            if email_handler.send_email(sender_email, sender_password, recipient_email, subject, body):
                print(f"Test email successfully sent to {recipient_email}")
            else:
                print(f"Failed to send test email to {recipient_email}")
        except Exception as e:
            print(f"An error occurred while sending test email: {e}")
        import sys
        sys.exit(0)

    # Otherwise, start the main application
    root = tk.Tk()
    app = TempMonitorClientApp(root, args.host, args.port, args.history)
    root.mainloop()


if __name__ == "__main__":
    main()
