import time
import json
import os
import sys
import subprocess
import ssl
import urllib.request
import urllib.error
import certifi
import hashlib
import math
import hmac
import logging
from logging.handlers import RotatingFileHandler
import tempfile
from datetime import datetime, date
import tkinter as tf_tk
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
        mutex_name = "NetMeterApp_SingleInstance_Mutex_NazmusSakib"
        global_mutex = kernel32.CreateMutexW(None, False, mutex_name)
        if kernel32.GetLastError() == 183:
            sys.exit(0)
        return global_mutex
    return None

# Application version
CURRENT_VERSION = "2.0.1"

# Automatically store history files securely inside Windows AppData
if os.name == 'nt':
    app_data = os.environ.get('APPDATA', os.path.expanduser("~"))
    HISTORY_DIR = os.path.join(app_data, "NetMeterApp")
else:
    HISTORY_DIR = os.path.expanduser("~")

os.makedirs(HISTORY_DIR, exist_ok=True)

# Four Storage Layers
MH_FILE = os.path.join(HISTORY_DIR, "net_usage_history.json")
BU1_FILE = os.path.join(HISTORY_DIR, "net_usage_history.json.bu1")
BU2_FILE = os.path.join(HISTORY_DIR, "net_usage_history.json.bu2")
BU3_FILE = os.path.join(HISTORY_DIR, "net_usage_history.json.bu3")



# -------------------------------------------------------------
# PERSISTENT LOGGING
# -------------------------------------------------------------
LOG_FILE = os.path.join(HISTORY_DIR, "netmeter.log")

logger = logging.getLogger("NetMeterApp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        ))
        logger.addHandler(file_handler)
    except Exception:
        logger.addHandler(logging.StreamHandler())
logger.propagate = False


# -------------------------------------------------------------
# STORAGE VALIDATOR & CHECKSUM UTILITIES
# -------------------------------------------------------------
SCHEMA_VERSION = 1
MAX_HISTORY_DAYS = 30


def calculate_checksum(schema_version, generation, captured_at, history_dict):
    """Calculate a deterministic SHA-256 checksum over the stored payload."""
    payload = {
        "schema_version": schema_version,
        "generation": generation,
        "captured_at": captured_at,
        "history": history_dict,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _valid_date_key(value):
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except ValueError:
        return False


def _clean_history(history, enforce_limit=True):
    """Validate and return a newest-first, bounded copy of a history mapping."""
    if not isinstance(history, dict):
        raise ValueError("History must be a JSON object.")

    cleaned = {}
    for day, value in history.items():
        if not _valid_date_key(day):
            raise ValueError(f"Invalid history date: {day!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Invalid usage value for {day}.")
        if (isinstance(value, float) and not math.isfinite(value)) or value < 0:
            raise ValueError(f"Usage value for {day} must be finite and non-negative.")
        cleaned[day] = value

    ordered = dict(sorted(cleaned.items(), key=lambda item: item[0], reverse=True))
    if enforce_limit and len(ordered) > MAX_HISTORY_DAYS:
        raise ValueError(f"History exceeds the {MAX_HISTORY_DAYS}-day retention limit.")
    return ordered


def validate_storage_file(filepath):
    """
    Validate a storage layer.

    Returns (status, data), where status is VALID, MISSING, EMPTY, READ_ERROR,
    INVALID_JSON, INVALID_SCHEMA, INVALID_CHECKSUM, INVALID_HISTORY,
    INVALID_DATES, or INVALID_VALUES. Legacy date dictionaries are accepted
    and returned as VALID with legacy_format=True so they can be migrated.
    """
    if not os.path.exists(filepath):
        return "MISSING", None

    try:
        if os.path.getsize(filepath) == 0:
            return "EMPTY", None
        with open(filepath, "r", encoding="utf-8") as file:
            raw = file.read()
        if not raw.strip():
            return "EMPTY", None
        data = json.loads(raw)
    except (OSError, UnicodeError):
        return "READ_ERROR", None
    except (json.JSONDecodeError, ValueError):
        return "INVALID_JSON", None

    if not isinstance(data, dict):
        return "INVALID_SCHEMA", None

    # Legacy format: {"YYYY-MM-DD": number, ...}
    if "history" not in data:
        try:
            legacy_history = _clean_history(data, enforce_limit=False)
            legacy_history = dict(list(legacy_history.items())[:MAX_HISTORY_DAYS])
            captured_at = datetime.now().isoformat(timespec="microseconds")
            checksum = calculate_checksum(
                SCHEMA_VERSION, 1, captured_at, legacy_history
            )
            return "VALID", {
                "schema_version": SCHEMA_VERSION,
                "generation": 1,
                "captured_at": captured_at,
                "history": legacy_history,
                "checksum": checksum,
                "legacy_format": True,
            }
        except ValueError as exc:
            message = str(exc)
            if "date" in message.lower():
                return "INVALID_DATES", None
            return "INVALID_VALUES", None

    required = {"schema_version", "generation", "captured_at", "history", "checksum"}
    if not required.issubset(data):
        return "INVALID_SCHEMA", None

    schema_version = data["schema_version"]
    generation = data["generation"]
    captured_at = data["captured_at"]
    checksum = data["checksum"]

    # bool is a subclass of int, so explicitly reject it.
    if (isinstance(schema_version, bool) or not isinstance(schema_version, int)
            or schema_version != SCHEMA_VERSION
            or isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0):
        return "INVALID_SCHEMA", None

    if not isinstance(captured_at, str):
        return "INVALID_SCHEMA", None
    try:
        if "T" not in captured_at:
            return "INVALID_SCHEMA", None
        datetime.fromisoformat(captured_at)
    except ValueError:
        return "INVALID_SCHEMA", None

    if not isinstance(checksum, str) or len(checksum) != 64:
        return "INVALID_SCHEMA", None
    try:
        int(checksum, 16)
    except ValueError:
        return "INVALID_SCHEMA", None

    try:
        history = _clean_history(data["history"], enforce_limit=True)
    except ValueError as exc:
        message = str(exc)
        if "date" in message.lower():
            return "INVALID_DATES", None
        if "History must" in message or "exceeds" in message:
            return "INVALID_HISTORY", None
        return "INVALID_VALUES", None

    expected = calculate_checksum(
        schema_version, generation, captured_at, history
    )
    if not hmac.compare_digest(checksum.lower(), expected):
        return "INVALID_CHECKSUM", None

    normalized = dict(data)
    normalized["history"] = history
    return "VALID", normalized


# -------------------------------------------------------------
# ATOMIC WRITER WITH OPTIONAL FSYNC
# -------------------------------------------------------------
def atomic_save_data(filepath, history_dict, generation=1, use_fsync=False):
    """
    Write a complete snapshot through a same-directory temporary file and
    atomic replace. Returns True only after the replacement succeeds.
    """
    temp_file = None
    try:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("Generation must be a non-negative integer.")

        retained_history = _clean_history(history_dict, enforce_limit=False)
        retained_history = dict(list(retained_history.items())[:MAX_HISTORY_DAYS])

        directory = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(directory, exist_ok=True)

        captured_at = datetime.now().isoformat(timespec="microseconds")
        checksum = calculate_checksum(
            SCHEMA_VERSION, generation, captured_at, retained_history
        )
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "captured_at": captured_at,
            "history": retained_history,
            "checksum": checksum,
        }

        fd, temp_file = tempfile.mkstemp(
            prefix=f".{os.path.basename(filepath)}.",
            suffix=".tmp",
            dir=directory,
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(snapshot, file, indent=4, allow_nan=False)
            file.flush()
            if use_fsync:
                os.fsync(file.fileno())

        temp_status, _ = validate_storage_file(temp_file)
        if temp_status != "VALID":
            raise IOError(f"Temporary snapshot validation failed: {temp_status}")

        os.replace(temp_file, filepath)
        temp_file = None

        if use_fsync and hasattr(os, "O_DIRECTORY"):
            try:
                dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                logger.warning("Directory fsync unavailable for %s: %s", directory, exc)

        logger.info(
            "Storage write succeeded: %s | generation=%s | fsync=%s",
            filepath, generation, use_fsync
        )
        return True

    except Exception:
        logger.exception("Storage write failed: %s", filepath)
        return False
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                logger.exception("Could not remove temporary file: %s", temp_file)


# -------------------------------------------------------------
# APPLICATION MAIN CLASS
# -------------------------------------------------------------
class NetMeterApp:
    def __init__(self, root):
        self.root = root
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.88)
        self.root.configure(bg="#222222")

        # Force high priority topmost window via Win32 API
        self.enforce_top_priority()

        self.history_window = None
        self.current_generation = 1

        self.label = tf_tk.Label(
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

        self.label.bind("<Button-1>", self.start_move)
        self.label.bind("<B1-Motion>", self.do_move)
        self.label.bind("<Double-Button-1>", self.open_history_window)

        self.last_net = psutil.net_io_counters()
        self.last_time = time.time()

        self.session_dl = 0
        self.session_ul = 0

        # Startup recovery sequence & initial load
        self.history, self.current_generation = self.startup_recovery_sequence()

        # Start main update loop (MH event-driven/1s check)
        self.update_speed()

        # Start independent staggered backup timers
        # BU1: 15s initial delay -> 300s (5 min) interval
        self.root.after(15000, self.schedule_backup_1)
        # BU2: 30s initial delay -> 720s (12 min) interval
        self.root.after(30000, self.schedule_backup_2)
        # BU3: 45s initial delay -> 1020s (17 min) interval
        self.root.after(45000, self.schedule_backup_3)

        # Check for updates in background
        self.root.after(3000, lambda: self.check_for_updates(manual=False))

    def enforce_top_priority(self):
        """Win32 API force-pin solution to ensure absolute topmost Z-order priority."""
        if os.name == 'nt':
            try:
                hwnd = self.root.winfo_id()
                # HWND_TOPMOST = -1
                # SWP_NOSIZE = 0x0001, SWP_NOMOVE = 0x0002, SWP_SHOWWINDOW = 0x0040
                ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            except Exception:
                pass
        # Re-enforce periodically (every 2.5 seconds) to beat competing topmost applications
        self.root.after(2500, self.enforce_top_priority)

    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        x = self.root.winfo_x() + (event.x - self.x)
        y = self.root.winfo_y() + (event.y - self.y)
        self.root.geometry(f"+{x}+{y}")

    def format_speed(self, speed_bytes):
        speed_kb = speed_bytes / 1024
        if speed_kb > 1024:
            return f"{speed_kb / 1024:.2f} MB/s"
        return f"{speed_kb:.1f} KB/s"

    def format_size(self, size_bytes):
        size_kb = size_bytes / 1024
        if size_kb > 1024 * 1024:
            return f"{size_kb / (1024 * 1024):.2f} GB"
        elif size_kb > 1024:
            return f"{size_kb / 1024:.2f} MB"
        return f"{size_kb:.1f} KB"

    def startup_recovery_sequence(self):
        statuses = {}
        records = {}
        for name, path in (
            ("MH", MH_FILE), ("BU1", BU1_FILE),
            ("BU2", BU2_FILE), ("BU3", BU3_FILE)
        ):
            status, data = validate_storage_file(path)
            statuses[name] = status
            if status == "VALID":
                records[name] = data

        logger.info(
            "Recovery audit | MH=%s BU1=%s BU2=%s BU3=%s",
            statuses["MH"], statuses["BU1"], statuses["BU2"], statuses["BU3"]
        )

        mh_valid = "MH" in records
        selected_name = None
        selected_data = None
        needs_fsync = False

        if mh_valid:
            mh_generation = records["MH"]["generation"]
            newer_backups = [
                name for name in ("BU1", "BU2", "BU3")
                if name in records and records[name]["generation"] > mh_generation
            ]
            if newer_backups:
                selected_name = max(
                    newer_backups,
                    key=lambda name: (
                        records[name]["generation"],
                        -("BU1", "BU2", "BU3").index(name)
                    )
                )
                selected_data = records[selected_name]
                logger.warning(
                    "Option B: %s generation %s is newer than MH generation %s; "
                    "the backup will supersede MH.",
                    selected_name, selected_data["generation"], mh_generation
                )
            else:
                selected_name = "MH"
                selected_data = records["MH"]
                logger.info("Valid MH selected; no backup has a higher generation.")

        else:
            bu1_valid = "BU1" in records
            bu2_valid = "BU2" in records
            bu3_valid = "BU3" in records

            if not bu1_valid and not bu3_valid and bu2_valid:
                selected_name, needs_fsync = "BU2", True
                logger.warning("Recovery matrix: MH + BU3 + BU1 unavailable -> BU2 (fsync).")
            elif not bu1_valid and not bu2_valid and bu3_valid:
                selected_name, needs_fsync = "BU3", True
                logger.warning("Recovery matrix: MH + BU1 + BU2 unavailable -> BU3 (fsync).")
            elif not bu1_valid and bu2_valid:
                selected_name, needs_fsync = "BU2", False
                logger.warning("Recovery matrix: MH + BU1 unavailable -> BU2.")
            elif bu1_valid and not bu2_valid:
                selected_name, needs_fsync = "BU1", False
                logger.warning("Recovery matrix: MH + BU2 unavailable -> BU1.")
            elif bu1_valid and not bu3_valid:
                selected_name, needs_fsync = "BU1", False
                logger.warning("Recovery matrix: MH + BU3 unavailable -> BU1.")
            else:
                logger.warning(
                    "Unlisted recovery combination; applying fallback BU1 > BU2 > BU3."
                )
                if bu1_valid:
                    selected_name, needs_fsync = "BU1", False
                elif bu2_valid:
                    selected_name, needs_fsync = "BU2", True
                elif bu3_valid:
                    selected_name, needs_fsync = "BU3", True

            if selected_name:
                selected_data = records[selected_name]

        if selected_data is None:
            logger.critical(
                "All storage layers are unavailable or invalid. Starting with empty history."
            )
            empty_saved = atomic_save_data(MH_FILE, {}, generation=1, use_fsync=True)
            if not empty_saved:
                logger.critical("Could not initialize an empty MH storage file.")
            try:
                messagebox.showwarning(
                    "NetMeterApp Data Recovery",
                    "No valid usage-history file or backup was found. "
                    "The application will start with empty history. "
                    "Check the NetMeterApp log for details."
                )
            except Exception:
                logger.exception("Could not display the recovery warning.")
            return {}, 1

        recovered_history = dict(selected_data["history"])
        source_generation = selected_data["generation"]

        if selected_name == "MH":
            if selected_data.get("legacy_format"):
                migrated = atomic_save_data(
                    MH_FILE, recovered_history, generation=source_generation,
                    use_fsync=True
                )
                if not migrated:
                    logger.error("Legacy MH migration failed; using validated data in memory.")
            logger.info("Recovery complete from MH at generation %s.", source_generation)
            return recovered_history, source_generation

        restored_generation = source_generation + 1
        restored = atomic_save_data(
            MH_FILE, recovered_history,
            generation=restored_generation,
            use_fsync=needs_fsync
        )
        if restored:
            logger.info(
                "Recovery succeeded: MH restored from %s at generation %s.",
                selected_name, restored_generation
            )
        else:
            logger.error(
                "Recovery source %s is valid, but restoring MH failed. "
                "Continuing with recovered data in memory.",
                selected_name
            )
            try:
                messagebox.showwarning(
                    "NetMeterApp Recovery Warning",
                    f"History was loaded from {selected_name}, but the primary "
                    "history file could not be restored. The app will continue "
                    "using recovered data in memory. Check disk permissions and "
                    "the NetMeterApp log."
                )
            except Exception:
                logger.exception("Could not display the MH restore warning.")

        return recovered_history, restored_generation

    def save_history(self):
        self.history = dict(sorted(
            self.history.items(), key=lambda item: item[0], reverse=True
        )[:MAX_HISTORY_DAYS])
        self.current_generation += 1
        saved = atomic_save_data(
            MH_FILE, self.history,
            generation=self.current_generation,
            use_fsync=False
        )
        if not saved:
            logger.error(
                "MH save failed at generation %s; a later backup may still "
                "persist this in-memory generation.",
                self.current_generation
            )
        return saved

    def _save_backup(self, backup_name, backup_path, fsync=False):
        try:
            saved = atomic_save_data(
                backup_path, self.history,
                generation=self.current_generation,
                use_fsync=fsync
            )
            if not saved:
                logger.error("%s scheduled backup failed.", backup_name)
            return saved
        except Exception:
            logger.exception("%s scheduled backup raised an exception.", backup_name)
            return False

    def schedule_backup_1(self):
        self._save_backup("BU1", BU1_FILE, fsync=False)
        self.root.after(300000, self.schedule_backup_1)

    def schedule_backup_2(self):
        self._save_backup("BU2", BU2_FILE, fsync=False)
        self.root.after(720000, self.schedule_backup_2)

    def schedule_backup_3(self):
        self._save_backup("BU3", BU3_FILE, fsync=False)
        self.root.after(1020000, self.schedule_backup_3)

    def update_speed(self):
        current_time = time.time()
        current_net = psutil.net_io_counters()

        time_delta = current_time - self.last_time
        if time_delta > 0:
            download_speed = max(0, (current_net.bytes_recv - self.last_net.bytes_recv) / time_delta)
            upload_speed = max(0, (current_net.bytes_sent - self.last_net.bytes_sent) / time_delta)

            dl_delta = current_net.bytes_recv - self.last_net.bytes_recv
            ul_delta = current_net.bytes_sent - self.last_net.bytes_sent

            today_str = str(date.today())
            activity_detected = False

            if dl_delta > 0:
                self.session_dl += dl_delta
                if today_str not in self.history:
                    self.history[today_str] = 0
                self.history[today_str] += dl_delta
                activity_detected = True

            if ul_delta > 0:
                self.session_ul += ul_delta
                if today_str not in self.history:
                    self.history[today_str] = 0
                self.history[today_str] += ul_delta
                activity_detected = True

            if activity_detected:
                self.save_history()

            self.last_net = current_net
            self.last_time = time.time()

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

        self.root.after(1000, self.update_speed)

    def get_startup_shortcut_path(self):
        if os.name == 'nt':
            startup_dir = os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup')
            return os.path.join(startup_dir, "NetMeterApp.lnk")
        return None

    def toggle_startup(self, btn_widget):
        shortcut_path = self.get_startup_shortcut_path()
        if not shortcut_path:
            return

        if os.path.exists(shortcut_path):
            try:
                os.remove(shortcut_path)
            except Exception:
                pass
            btn_widget.config(text="Start with Windows: OFF", bg="#333333", fg="#ffffff")
        else:
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
        try:
            url = "https://api.github.com/repos/nazmusweb-coding/net-meter-app/releases/latest"
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
                            messagebox.showinfo("Update", "New release found on GitHub, but 'net_meter.exe' asset is missing.")
                else:
                    if manual:
                        messagebox.showinfo("Up to Date", f"You are using the latest version ({CURRENT_VERSION}).")
        except Exception as e:
            if manual:
                messagebox.showerror("Update Error", f"Could not check for updates.\n{e}")

    def perform_update(self, download_url, latest_tag):
        try:
            temp_dir = os.environ.get('TEMP', os.path.expanduser("~"))
            new_exe_path = os.path.join(temp_dir, "net_meter_new.exe")
            context = ssl.create_default_context(cafile=certifi.where())
            req = urllib.request.Request(download_url, headers={'User-Agent': 'NetMeterApp'})
            with urllib.request.urlopen(req, context=context) as response, open(new_exe_path, 'wb') as out_file:
                out_file.write(response.read())

            current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
            bat_path = os.path.join(temp_dir, "update_netmeter.bat")

            bat_content = f"""
            @echo off
            echo Updating Net Meter...
            :wait_app
            taskkill /f /im "{os.path.basename(current_exe)}" >nul 2>&1
            timeout /t 3 /nobreak > nul
            :try_move
            move /y "{new_exe_path}" "{current_exe}"
            if errorlevel 1 (
                timeout /t 2 /nobreak > nul
                goto :try_move
            )
            echo Starting updated app...
            start "" "{current_exe}"
            del "%~f0"
            exit
            """
            with open(bat_path, "w") as f:
                f.write(bat_content)

            subprocess.Popen(["cmd.exe", "/c", bat_path], creationflags=0)
            self.root.destroy()
            sys.exit(0)
        except Exception as e:
            messagebox.showerror("Update Failed", f"An error occurred during update:\n{e}")

    def open_history_window(self, event):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            self.history_window.focus_force()
            return

        self.history_window = tf_tk.Toplevel(self.root)
        self.history_window.title("30-Day Data Usage History")
        self.history_window.geometry("310x540")
        self.history_window.configure(bg="#1e1e1e")
        self.history_window.attributes("-topmost", True)

        def on_close():
            if self.history_window:
                self.history_window.destroy()
                self.history_window = None

        self.history_window.protocol("WM_DELETE_WINDOW", on_close)

        title_label = tf_tk.Label(
            self.history_window,
            text="Recent Total Consumption",
            font=("Consolas", 11, "bold"),
            fg="#00FFCC",
            bg="#1e1e1e",
            pady=8
        )
        title_label.pack(side="top")

        btn_frame = tf_tk.Frame(self.history_window, bg="#1e1e1e")
        btn_frame.pack(side="bottom", pady=8)

        close_btn = tf_tk.Button(
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

        quit_btn = tf_tk.Button(
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

        update_btn = tf_tk.Button(
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

        shortcut_path = self.get_startup_shortcut_path()
        is_startup_enabled = shortcut_path and os.path.exists(shortcut_path)

        startup_btn_text = "Start with Windows: ON" if is_startup_enabled else "Start with Windows: OFF"
        startup_btn_bg = "#005533" if is_startup_enabled else "#333333"
        startup_btn_fg = "#00FFCC" if is_startup_enabled else "#ffffff"

        startup_btn = tf_tk.Button(
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

        sorted_dates = sorted(self.history.keys(), reverse=True)
        grand_total_bytes = sum(self.history[d] for d in sorted_dates)
        grand_total_str = self.format_size(grand_total_bytes)

        total_frame = tf_tk.Frame(self.history_window, bg="#252525", bd=1, relief="solid")
        total_frame.pack(side="bottom", fill="x", padx=10, pady=4)

        total_label = tf_tk.Label(
            total_frame,
            text=f"Total (30 Days): {grand_total_str}",
            font=("Consolas", 10, "bold"),
            fg="#00FFCC",
            bg="#252525",
            anchor="w",
            padx=10,
            pady=5
        )
        total_label.pack(fill="x")

        container = tf_tk.Frame(self.history_window, bg="#1e1e1e")
        container.pack(side="top", fill="both", expand=True, padx=10, pady=2)

        canvas = tf_tk.Canvas(container, bg="#1e1e1e", highlightthickness=0)
        scrollbar = tf_tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tf_tk.Frame(canvas, bg="#1e1e1e")

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
            no_data_label = tf_tk.Label(
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
                row_label = tf_tk.Label(
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
    mutex_lock = check_single_instance()
    root = tf_tk.Tk()
    root.geometry("+100+100")
    app = NetMeterApp(root)
    root.mainloop()