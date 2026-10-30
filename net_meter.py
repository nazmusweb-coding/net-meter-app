import time
import json
import os
import sys
import subprocess
import ssl
import urllib.request
import urllib.error
import certifi
from datetime import datetime, date
import tkinter as tk
from tkinter import messagebox
import psutil
import ctypes


# -------------------------------------------------------------
# SINGLE-INSTANCE CHECK (Prevents running multiple windows)
# -------------------------------------------------------------
def check_single_instance():
    """Ensure only one instance of the application runs at a time on Windows."""
    if os.name == 'nt':
        kernel32 = ctypes.windll.kernel32
        # Create a named system-wide mutex lock
        mutex_name = "NetMeterApp_SingleInstance_Mutex_NazmusSakib"
        # Try to create the mutex
        global_mutex = kernel32.CreateMutexW(None, False, mutex_name)
        # ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == 183:
            sys.exit(0)
        return global_mutex
    return None


# Application version for auto-update checks
CURRENT_VERSION = "1.0.0"

# Automatically store the history file securely inside Windows AppData
if os.name == 'nt':  # Windows
    app_data = os.environ.get('APPDATA', os.path.expanduser("~"))
    HISTORY_DIR = os.path.join(app_data, "NetMeterApp")
else:  # Mac / Linux fallback
    HISTORY_DIR = os.path.expanduser("~")

os.makedirs(HISTORY_DIR, exist_ok=True)
HISTORY_FILE = os.path.join(HISTORY_DIR, "net_usage_history.json")


class NetMeterApp:
    def __init__(self, root):
        self.root = root
        self.root.overrideredirect(True)  # Remove window borders/title bar
        self.root.attributes("-topmost", True)  # Keep window always on top
        self.root.attributes("-alpha", 0.88)  # Slightly transparent
        self.root.configure(bg="#222222")

        # Track the history settings window instance to prevent multiple popups
        self.history_window = None

        # Label to display the speed and session usage
        self.label = tk.Label(
            root,
            text="Initializing...",
            font=("Consolas", 9, "bold"),
            fg="#00FFCC",
            bg="#222222",
            padx=10,
            pady=6,
            justify="left"
        )
        self.label.pack()

        # Enable dragging the window around with single click
        self.label.bind("<Button-1>", self.start_move)
        self.label.bind("<B1-Motion>", self.do_move)

        # Enable opening history dashboard with a double-click
        self.label.bind("<Double-Button-1>", self.open_history_window)

        # Initialize network counters and time for speed calculation
        self.last_net = psutil.net_io_counters()
        self.last_time = time.time()

        # Track session data safely using accumulated counters
        self.session_dl = 0
        self.session_ul = 0

        # Load historical usage data
        self.history = self.load_history()

        # Start update loop
        self.update_speed()

        # Silently check for updates in the background on startup
        self.root.after(3000, lambda: self.check_for_updates(manual=False))

    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        x = self.root.winfo_x() + (event.x - self.x)
        y = self.root.winfo_y() + (event.y - self.y)
        self.root.geometry(f"+{x}+{y}")

    def format_speed(self, speed_bytes):
        """Convert bytes/sec to KB/s or MB/s"""
        speed_kb = speed_bytes / 1024
        if speed_kb > 1024:
            return f"{speed_kb / 1024:.2f} MB/s"
        return f"{speed_kb:.1f} KB/s"

    def format_size(self, size_bytes):
        """Convert total bytes to KB, MB, or GB"""
        size_kb = size_bytes / 1024
        if size_kb > 1024 * 1024:
            return f"{size_kb / (1024 * 1024):.2f} GB"
        elif size_kb > 1024:
            return f"{size_kb / 1024:.2f} MB"
        return f"{size_kb:.1f} KB"

    def load_history(self):
        """Load history from JSON file or create empty dict"""
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_history(self):
        """Save history and keep only the last 15 days"""
        sorted_dates = sorted(self.history.keys(), reverse=True)
        if len(sorted_dates) > 15:
            for old_date in sorted_dates[15:]:
                del self.history[old_date]

        with open(HISTORY_FILE, "w") as f:
            json.dump(self.history, f, indent=4)

    def update_speed(self):
        current_time = time.time()
        current_net = psutil.net_io_counters()

        time_delta = current_time - self.last_time
        if time_delta > 0:
            # Calculate instantaneous speeds
            download_speed = max(0, (current_net.bytes_recv - self.last_net.bytes_recv) / time_delta)
            upload_speed = max(0, (current_net.bytes_sent - self.last_net.bytes_sent) / time_delta)

            # Calculate bytes transferred in this specific 1-second tick (deltas)
            dl_delta = current_net.bytes_recv - self.last_net.bytes_recv
            ul_delta = current_net.bytes_sent - self.last_net.bytes_sent

            # Accumulate download and upload usage safely
            if dl_delta > 0:
                self.session_dl += dl_delta
                today_str = str(date.today())
                if today_str not in self.history:
                    self.history[today_str] = 0
                self.history[today_str] += dl_delta

            if ul_delta > 0:
                self.session_ul += ul_delta
                today_str = str(date.today())
                if today_str not in self.history:
                    self.history[today_str] = 0
                self.history[today_str] += ul_delta

            if dl_delta > 0 or ul_delta > 0:
                self.save_history()

            # Update tracking variables for next loop
            self.last_net = current_net
            self.last_time = time.time()

            # Format strings
            dl_str = self.format_speed(download_speed)
            ul_str = self.format_speed(upload_speed)
            session_dl_str = self.format_size(self.session_dl)
            session_ul_str = self.format_size(self.session_ul)

            display_text = (
                f"📥 {dl_str}  |  📤 {ul_str}\n"
                f"Session: 📥 {session_dl_str} | 📤 {session_ul_str}\n"
                f"(Developed By Nazmus Sakib)"
            )
            self.label.config(text=display_text)

        # Refresh every 1 second
        self.root.after(1000, self.update_speed)

    def get_startup_shortcut_path(self):
        """Get the path to the shortcut inside the Windows Startup folder"""
        if os.name == 'nt':
            startup_dir = os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup')
            return os.path.join(startup_dir, "NetMeterApp.lnk")
        return None

    def toggle_startup(self, btn_widget):
        """Enable or disable starting with Windows programmatically"""
        shortcut_path = self.get_startup_shortcut_path()
        if not shortcut_path:
            return

        if os.path.exists(shortcut_path):
            # Disable: Remove shortcut
            try:
                os.remove(shortcut_path)
            except Exception:
                pass
            btn_widget.config(text="Start with Windows: OFF", bg="#333333", fg="#ffffff")
        else:
            # Enable: Create shortcut using PowerShell
            target_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
            ps_script = f"""
            $WScript = New-Object -ComObject WScript.Shell
            $Shortcut = $WScript.CreateShortcut('{shortcut_path}')
            $Shortcut.TargetPath = '{target_path}'
            $Shortcut.Save()
            """
            try:
                subprocess.run(["powershell", "-Command", ps_script], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                btn_widget.config(text="Start with Windows: ON", bg="#005533", fg="#00FFCC")
            except Exception:
                pass

    def check_for_updates(self, manual=False):
        """Query GitHub Releases API to check for newer version using certifi SSL context"""
        try:
            url = "https://api.github.com/repos/nazmusweb-coding/net-meter-app/releases/latest"

            # Create secure SSL context using certifi root certificates to fix SSL verification errors on client machines
            context = ssl.create_default_context(cafile=certifi.where())

            req = urllib.request.Request(url, headers={'User-Agent': 'NetMeterApp'})
            with urllib.request.urlopen(req, context=context, timeout=5) as response:
                data = json.loads(response.read().decode())
                latest_tag = data.get("tag_name", "").lstrip('v')

                if latest_tag and latest_tag > CURRENT_VERSION:
                    assets = data.get("assets", [])
                    download_url = None
                    for asset in assets:
                        if asset.get("name") == "net_meter.exe":
                            download_url = asset.get("browser_download_url")
                            break

                    if download_url:
                        if manual or messagebox.askyesno("Update Available",
                                                         f"A new version ({latest_tag}) is available!\nDo you want to update now?"):
                            self.perform_update(download_url, latest_tag)
                    else:
                        if manual:
                            messagebox.showinfo("Update",
                                                "New release found on GitHub, but 'net_meter.exe' asset is missing.")
                else:
                    if manual:
                        messagebox.showinfo("Up to Date", f"You are using the latest version ({CURRENT_VERSION}).")
        except Exception as e:
            if manual:
                messagebox.showerror("Update Error", f"Could not check for updates.\n{e}")

    def perform_update(self, download_url, latest_tag):
        """Download new executable and use helper batch script to bypass file-locking"""
        try:
            temp_dir = os.environ.get('TEMP', os.path.expanduser("~"))
            new_exe_path = os.path.join(temp_dir, "net_meter_new.exe")

            # Create secure SSL context for downloading as well
            context = ssl.create_default_context(cafile=certifi.where())

            # Download file securely using context
            req = urllib.request.Request(download_url, headers={'User-Agent': 'NetMeterApp'})
            with urllib.request.urlopen(req, context=context) as response, open(new_exe_path, 'wb') as out_file:
                out_file.write(response.read())

            current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
            bat_path = os.path.join(temp_dir, "update_netmeter.bat")

            # Temporary helper batch script to overwrite running executable and restart
            bat_content = f"""
            @echo off
            timeout /t 2 /nobreak > nul
            move /y "{new_exe_path}" "{current_exe}"
            start "" "{current_exe}"
            del "%~f0"
            """
            with open(bat_path, "w") as f:
                f.write(bat_content)

            # Launch batch script silently and terminate app
            subprocess.Popen(bat_path, shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.root.destroy()
            sys.exit(0)
        except Exception as e:
            messagebox.showerror("Update Failed", f"An error occurred during update:\n{e}")

    def open_history_window(self, event):
        """Open settings & history dashboard ensuring only one window exists at a time"""
        # If the window already exists and is open, bring it to focus instead of opening a new one
        if self.history_window is not None and tk.Toplevel.winfo_exists(self.history_window):
            self.history_window.lift()
            self.history_window.focus_force()
            return

        self.history_window = tk.Toplevel(self.root)
        self.history_window.title("15-Day Data Usage History")
        self.history_window.geometry("310x540")
        self.history_window.configure(bg="#1e1e1e")
        self.history_window.attributes("-topmost", True)

        # Clear reference when closed
        def on_close():
            if self.history_window:
                self.history_window.destroy()
                self.history_window = None

        self.history_window.protocol("WM_DELETE_WINDOW", on_close)

        # Title at the top
        title_label = tk.Label(
            self.history_window,
            text="Recent Total Consumption",
            font=("Consolas", 11, "bold"),
            fg="#00FFCC",
            bg="#1e1e1e",
            pady=8
        )
        title_label.pack(side="top")

        # Buttons at the bottom
        btn_frame = tk.Frame(self.history_window, bg="#1e1e1e")
        btn_frame.pack(side="bottom", pady=8)

        close_btn = tk.Button(
            btn_frame,
            text="Close Menu",
            command=on_close,
            bg="#333333",
            fg="#ffffff",
            font=("Consolas", 9),
            bd=0,
            padx=10,
            pady=5
        )
        close_btn.pack(side="left", padx=5)

        quit_btn = tk.Button(
            btn_frame,
            text="Quit App",
            command=self.root.destroy,
            bg="#8b0000",
            fg="#ffffff",
            font=("Consolas", 9, "bold"),
            bd=0,
            padx=10,
            pady=5
        )
        quit_btn.pack(side="left", padx=5)

        # Manual Check for Updates Button
        update_btn = tk.Button(
            self.history_window,
            text=f"Check for Updates (v{CURRENT_VERSION})",
            font=("Consolas", 9, "bold"),
            bg="#003366",
            fg="#00FFCC",
            bd=0,
            padx=10,
            pady=6,
            command=lambda: self.check_for_updates(manual=True)
        )
        update_btn.pack(side="bottom", fill="x", padx=15, pady=2)

        # Startup Toggle Button
        shortcut_path = self.get_startup_shortcut_path()
        is_startup_enabled = shortcut_path and os.path.exists(shortcut_path)

        startup_btn_text = "Start with Windows: ON" if is_startup_enabled else "Start with Windows: OFF"
        startup_btn_bg = "#005533" if is_startup_enabled else "#333333"
        startup_btn_fg = "#00FFCC" if is_startup_enabled else "#ffffff"

        startup_btn = tk.Button(
            self.history_window,
            text=startup_btn_text,
            font=("Consolas", 9, "bold"),
            bg=startup_btn_bg,
            fg=startup_btn_fg,
            bd=0,
            padx=10,
            pady=6,
            command=lambda: self.toggle_startup(startup_btn)
        )
        startup_btn.pack(side="bottom", fill="x", padx=15, pady=2)

        # Calculate Grand Total and create summary box
        sorted_dates = sorted(self.history.keys(), reverse=True)
        grand_total_bytes = sum(self.history[d] for d in sorted_dates)
        grand_total_str = self.format_size(grand_total_bytes)

        total_frame = tk.Frame(self.history_window, bg="#252525", bd=1, relief="solid")
        total_frame.pack(side="bottom", fill="x", padx=10, pady=4)

        total_label = tk.Label(
            total_frame,
            text=f"Total (15 Days): {grand_total_str}",
            font=("Consolas", 10, "bold"),
            fg="#00FFCC",
            bg="#252525",
            anchor="w",
            padx=10,
            pady=5
        )
        total_label.pack(fill="x")

        # Scrollable container for dates
        container = tk.Frame(self.history_window, bg="#1e1e1e")
        container.pack(side="top", fill="both", expand=True, padx=10, pady=2)

        canvas = tk.Canvas(container, bg="#1e1e1e", highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg="#1e1e1e")

        def configure_interior(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        scrollable_frame.bind("<Configure>", configure_interior)

        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")

        def configure_canvas(event):
            canvas.itemconfig(canvas_window, width=event.width)

        canvas.bind("<Configure>", configure_canvas)

        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        if not sorted_dates:
            no_data_label = tk.Label(
                scrollable_frame,
                text="No history recorded yet.",
                font=("Consolas", 9),
                fg="#aaaaaa",
                bg="#1e1e1e"
            )
            no_data_label.pack(pady=20)
        else:
            for d in sorted_dates:
                total_bytes = self.history[d]
                size_str = self.format_size(total_bytes)

                row_text = f"{d}  ➔  {size_str}"
                row_label = tk.Label(
                    scrollable_frame,
                    text=row_text,
                    font=("Consolas", 10),
                    fg="#ffffff",
                    bg="#2d2d2d",
                    anchor="w",
                    padx=10,
                    pady=4
                )
                row_label.pack(fill="x", pady=2)


if __name__ == "__main__":
    # Enforce single instance execution
    mutex_lock = check_single_instance()

    root = tk.Tk()
    root.geometry("+100+100")
    app = NetMeterApp(root)
    root.mainloop()