import ctypes
from contextlib import closing
import dataclasses
import logging
import math
from logging.handlers import RotatingFileHandler
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

if __package__:
    from . import __version__
else:
    from __init__ import __version__

# Running ``python -m wifivouchers`` from this package directory resolves this
# file as a top-level module instead of resolving the package from its parent.
if __name__ == "__main__" and not __package__:
    package_root = Path(__file__).resolve().parent.parent
    if (package_root / "wifivouchers" / "__init__.py").is_file():
        sys.path.insert(0, str(package_root))
        application = __import__(
            "wifivouchers.wifivouchers",
            fromlist=["StartupError", "main"],
        )

        try:
            application.main()
        except application.StartupError as exc:
            sys.exit(exc.exit_code)
        sys.exit(0)

IMPORT_ERROR_MESSAGES = {
    "configparser": "configparser not installed.",
    "tkinter": "Tkinter not installed.",
    "tkinter.filedialog": "Tkinter file dialog support is missing.",
    "win32print": "pywin32 printing support is missing.",
    "wmi": "wmi module is missing.",
    "qrcode": "qrcode module is missing.",
    "pystray": "pystray is missing; install it with 'pip install pystray'.",
    "PIL": "Pillow is missing; install it with 'pip install Pillow'.",
    "PIL.Image": "Pillow image support is missing; install it with 'pip install Pillow'.",
    "PIL.ImageDraw": "Pillow drawing support is missing; install it with 'pip install Pillow'.",
    "PIL.ImageFont": "Pillow font support is missing; install it with 'pip install Pillow'.",
    "roll_importer": "Voucher roll importer is missing.",
}

def _import_dependency(module_name, import_callable, friendly_name=None):
    """Import one dependency, exiting with a friendly message on failure."""
    try:
        return import_callable()
    except ImportError as exc:
        message = IMPORT_ERROR_MESSAGES.get(
            friendly_name or module_name,
            IMPORT_ERROR_MESSAGES.get(
                module_name,
                f"Unable to import dependency {module_name}: {exc}",
            ),
        )
        if friendly_name == "roll_importer":
            message = f"{message} Import error: {exc}"
        print(message, file=sys.stderr)
        sys.exit(message)


configparser = _import_dependency(
    "configparser", lambda: __import__("configparser")
)
import tkinter as tk
from tkinter import messagebox
filedialog = _import_dependency(
    "tkinter.filedialog",
    lambda: __import__("tkinter.filedialog", fromlist=["askopenfilename"]),
    friendly_name="tkinter.filedialog",
)
_pil_image = _import_dependency(
    "PIL.Image", lambda: __import__("PIL.Image", fromlist=["Image"])
)
_pil_draw = _import_dependency(
    "PIL.ImageDraw", lambda: __import__("PIL.ImageDraw", fromlist=["ImageDraw"])
)
_pil_font = _import_dependency(
    "PIL.ImageFont", lambda: __import__("PIL.ImageFont", fromlist=["ImageFont"])
)
pystray = _import_dependency("pystray", lambda: __import__("pystray"))
qrcode = _import_dependency("qrcode", lambda: __import__("qrcode"))
VoucherRollImporter = _import_dependency(
    "roll_importer",
    lambda: __import__(
        "wifivouchers.roll_importer", fromlist=["VoucherRollImporter"]
    ).VoucherRollImporter,
    friendly_name="roll_importer",
)
Image = _pil_image
ImageDraw = _pil_draw
ImageFont = _pil_font

# detect OS: platform_dispatch.py chooses correct CSV reader, printer
# backend, and UI helpers to match the operating system.
from .platform_dispatch import (  # noqa: E402
    IS_LINUX,
    IS_WINDOWS,
    acquire_single_instance,
    check_printer_online,
    edit_ini_file as _platform_edit_ini_file,
    list_available_printers,
    print_printer_bytes,
    read_csv_roll,
    release_single_instance,
    show_message,
)

# use executables folder when packaged or use script folder during development.
SCRIPT_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_PATH = SCRIPT_DIR / "vouchers.ini"
DATABASE_PATH = SCRIPT_DIR / "vouchers.db"
LOG_PATH = SCRIPT_DIR / "wifivouchers.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 3
logger = logging.getLogger(__name__)


# sets up vouchers.ini.
@dataclasses.dataclass
class Settings:
    roll_number: int | None = None
    header: str = ""
    server_address: str = ""
    zone_name: str = ""
    url_scheme: str = "http"
    logging_enabled: bool = True
    start_minimized: bool = False
    compact_database: bool = False
    font_size: float = 1.0
    printer: str = ""

    @classmethod
    def from_configparser(cls, parser):
        font_size = parser.getfloat("DEFAULT", "font_size", fallback=1.0)
        if not math.isfinite(font_size) or font_size <= 0:
            raise ValueError("font_size in vouchers.ini must be a positive number.")

        return cls(
            roll_number=parser.getint("DEFAULT", "roll_number", fallback=None),
            header=parser["DEFAULT"]["header"],
            server_address=parser.get("DEFAULT", "server_address", fallback=""),
            zone_name=parser.get("DEFAULT", "zone_name", fallback=""),
            url_scheme=parser.get(
                "DEFAULT", "url_scheme", fallback="http"
            ).strip().lower(),
            logging_enabled=parser.getboolean(
                "DEFAULT", "logging_enabled", fallback=True
            ),
            start_minimized=parser.getboolean(
                "DEFAULT", "start_minimized", fallback=False
            ),
            compact_database=parser.getboolean(
                "DEFAULT", "compact_database", fallback=False
            ),
            font_size=font_size,
            printer=parser["PRINTER SETTINGS"]["printer"],
        )

    # dict-style compatibility so existing settings[key] access keeps working.
    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __setitem__(self, key, value):
        if not hasattr(self, key):
            raise KeyError(key)
        setattr(self, key, value)

    def __contains__(self, key):
        return hasattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

# create INI file and use blablabla values if INI not found
DEFAULT_INI_TEMPLATE = '''[DEFAULT]
header = Wifi Voucher
server_address = pfsense.example.local
zone_name = Guest_WiFi
url_scheme = http
logging_enabled = true
start_minimized = false
compact_database = false
font_size = 1.0

[PRINTER SETTINGS]
printer = Epson

[WINDOW]
x = 10
y = 10
'''

# ESC/POS commands for generic thermal printer
ESC_POS_INITIALIZE = b"\x1b\x40"
ESC_POS_LINE_SPACING = b"\x1b\x33\x20"
ESC_POS_BOLD_ON = b"\x1b\x45\x01"
ESC_POS_BOLD_OFF = b"\x1b\x45\x00"
ESC_POS_NORMAL_SIZE = b"\x1d\x21\x00"
ESC_POS_CENTER = b"\x1b\x61\x01"
ESC_POS_LEFT_ALIGN = b"\x1b\x61\x00"
ESC_POS_FEED_AND_CUT = b"\n\n\x1d\x56\x41\x00"
MAX_QR_WIDTH_DOTS = 384
MAX_QR_DOTS_PER_MODULE = 4
QR_IMAGE_SCALE_FACTOR = 1.5
PRINT_WIDTH_DOTS = 384
PRINT_MARGIN_DOTS = 8
PRINT_TOP_MARGIN_DOTS = 102
PRINT_END_MARGIN_DOTS = 203
PRINT_TEXT_SIZE = 36
LINUX_PRINT_TEXT_SIZE = 40
FALLBACK_PRINT_TEXT_SIZE = 10
PRINT_LINE_GAP = 6
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
GWL_WNDPROC = -4
DEFAULT_WINDOW_POSITION = (10, 10)


# most of the logic is done here which coordinates voucher selection, printing, and UI functions
class VoucherApp:
    def __init__(self, settings):
        self.settings = settings
        self.window = None
        self.buttons = []
        self.message_bar = None
        self.tray_icon = None
        self.roll_importer = VoucherRollImporter(DATABASE_PATH)
        self._shutdown_window_proc = None
        self._original_window_proc = None
        self._call_window_proc = None

    def show_message(self, title, text, style=0):
        return show_message(title, text, style)

    # try to read one voucher code from the database else warn user to import a roll first
    def fetch_voucher(self):
        roll_number = self.settings.get("roll_number")
        if roll_number is None:
            self.show_message(
                "Import Roll Required",
                "Import a voucher roll before printing.",
                0,
            )
            return None

        try:
            voucher = self.roll_importer.get_next_voucher(roll_number)
        except sqlite3.Error as exc:
            logger.exception("Unable to read voucher roll from the database")
            self.show_message(
                "Voucher Database Error",
                f"Unable to read voucher roll from the database:\n{exc}",
                0,
            )
            return None

        if voucher is None:
            logger.info("No unprinted vouchers remain in roll %s", roll_number)
            self.show_message(
                "Voucher Roll Empty",
                f"No unprinted voucher codes remain in roll {roll_number}.",
                0,
            )
            return None

        return (roll_number, *voucher)

    # confirm selected printer is available or report error.
    def verify_printer(self):
        printer_name = self.settings["printer"]
        try:
            printer_status = check_printer_online(printer_name)
        except Exception as exc:
            logger.exception("Unable to check printer status")
            self.show_message(
                "Printer Error",
                f"Unable to check printer status:\n{exc}",
                0,
            )
            return False

        if printer_status == "not_found":
            logger.warning("Configured printer was not found: %s", printer_name)
            self.show_message(
                "Printer Not Found",
                f"Configured printer was not found:\n{printer_name}",
                0,
            )
            return False
        if printer_status == "offline":
            logger.warning("Configured printer is offline: %s", printer_name)
            self.show_message(
                "Printer Offline",
                f"The configured printer is offline:\n{printer_name}",
                0,
            )
            return False
        return True

    # Mark the voucher printed only after the user confirms it printed correctly.
    def confirm_and_mark_printed(self, roll_number, voucher_id):
        response = self.show_message(
            "Print Voucher Codes",
            "If voucher printed then click OK otherwise click cancel",
            1,
        )
        if response != 1:
            return

        try:
            marked_printed = self.roll_importer.mark_printed(roll_number, voucher_id)
        except sqlite3.Error as exc:
            logger.exception(
                "Unable to mark voucher %s from roll %s as printed",
                voucher_id,
                roll_number,
            )
            self.show_message(
                "Voucher Database Error",
                "The voucher was printed, but the database could not be updated. "
                "It may print again if retried.\n\n"
                f"Error: {exc}",
                0,
            )
            return

        if not marked_printed:
            logger.warning(
                "Voucher %s in roll %s was not marked printed",
                voucher_id,
                roll_number,
            )
            self.show_message(
                "Voucher Database Warning",
                "The voucher was printed, but its database record was already "
                "updated or could not be found.",
                0,
            )
        else:
            self.refresh_available_status()

    # Read one voucher code, print it, then mark it printed after user confirmation.
    def get_voucher_code(self):
        fetched = self.fetch_voucher()
        if fetched is None:
            return
        roll_number, voucher_id, voucher_code = fetched

        if not self.verify_printer():
            return

        try:
            printed = self.ticket_print(voucher_code)
        except ValueError:
            return

        if printed:
            self.confirm_and_mark_printed(roll_number, voucher_id)

    # prepare receipt and submit it to the selected printer.
    def ticket_print(self, print_voucher_code):
        lines = self.settings["header"].split("\n")
        lines.extend(("", "", ""))
        lines.extend(
            "".join(code_line)
            for code_line in print_voucher_code.split("\n\n")
        )

        try:
            voucher_url = build_voucher_url(print_voucher_code, self.settings)
        except ValueError as exc:
            logger.warning("Voucher URL configuration is invalid: %s", exc)
            self.show_message("Configuration Error", str(exc), 0)
            raise ValueError(str(exc)) from exc

        print_data = build_thermal_bytes(
            lines,
            voucher_url,
            self.settings.get("font_size", 1.0),
        )
        printed = print_printer_bytes(self.settings["printer"], print_data)
        if printed:
            logger.info("Print job submitted to printer: %s", self.settings["printer"])
        return printed

    # UI and window lifecycle stuff.
    def print_vouchers(self):
        self.get_voucher_code()

    def handle_return_key(self, _event):
        self.print_vouchers()
        return "break"

    def minimize_to_tray(self):
        if self.window is None:
            return
        tray_unavailable = self.tray_icon is None
        if (
            IS_LINUX
            and self.tray_icon is not None
            and type(self.tray_icon).__module__ == "pystray._xorg"
        ):
            tray_unavailable = (
                getattr(self.tray_icon, "_systray_manager", None) is None
            )

        if tray_unavailable:
            self.show_message(
                "System Tray Unavailable",
                "WifiVouchers cannot minimize because no system tray area is "
                "available. On Xfce, add the Notification Area or Status Tray "
                "item to the panel, then restart WifiVouchers. The window "
                "will remain open.",
                0,
            )
            return
        self.window.withdraw()

    def restore_window(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()

    def start_tray(self):
        self.tray_icon = pystray.Icon(
            "WifiVoucherCodes",
            Image.new("RGB", (32, 32), color="#ff0000"),
            "Wifi Voucher Codes",
            menu=pystray.Menu(
                pystray.MenuItem(
                    "Restore",
                    self._tray_restore,
                    default=True,
                ),
                pystray.MenuItem(
                    "Print Voucher Code",
                    self._tray_print_voucher,
                ),
                pystray.MenuItem(
                    "Exit",
                    self._tray_exit,
                ),
            ),
        )
        try:
            self.tray_icon.run_detached()
        except Exception:
            self.tray_icon = None
            raise

    def _tray_restore(self, _icon, _item):
        if self.window is not None:
            self.window.after(0, self.restore_window)

    def _tray_print_voucher(self, _icon, _item):
        if self.window is not None:
            self.window.after(0, self.print_vouchers)

    def _tray_exit(self, _icon, _item):
        if self.window is not None:
            self.window.after(0, self.kill_app)

    def import_roll(self):
        selected_path = filedialog.askopenfilename(
            title="Select voucher roll",
            initialdir=str(SCRIPT_DIR),
            filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not selected_path:
            return

        result = (
            "Import Roll Error",
            "An unexpected error occurred while importing the voucher roll.",
        )
        available_count = None
        self.set_importing(True)
        try:
            try:
                (
                    roll_number,
                    imported_count,
                    duplicate_count,
                    available_count,
                ) = self.roll_importer.import_roll(selected_path)
            except Exception as exc:
                logger.exception("Unable to import voucher roll: %s", selected_path)
                result = (
                    "Import Roll Error",
                    f"Unable to import the selected voucher roll:\n{selected_path}\n\n{exc}",
                )
            else:
                self.settings["roll_number"] = roll_number
                try:
                    save_active_roll_number(roll_number)
                except (OSError, configparser.Error) as exc:
                    logger.exception("Unable to save active voucher roll number")
                    result = (
                        "Import Roll Warning",
                        f"{imported_count} new voucher codes were imported and "
                        f"{duplicate_count} duplicates were found for roll {roll_number}. "
                        f"The codes were saved to the database "
                        "but the active roll could not be saved "
                        "to the configuration file. This roll is active until the app closes.\n\n"
                        f"{exc}",
                    )
                else:
                    result = (
                        "Import Roll",
                        f"Imported {imported_count} new voucher codes and found "
                        f"{duplicate_count} duplicates in roll {roll_number}.",
                    )
        finally:
            self.set_importing(False)

        self.refresh_available_status()
        self.show_message(result[0], result[1], 0)

    def select_printer(self):
        try:
            printers = list_available_printers()
        except Exception as exc:
            logger.exception("Unable to list installed printers")
            self.show_message(
                "Printer Error",
                f"Unable to list the Windows printers:\n{exc}",
                0,
            )
            return

        if not printers:
            self.show_message(
                "No Printers Found",
                (
                    "Unable to find any installed printers."
                    if IS_LINUX
                    else "Windows did not report any installed printers."
                ),
                0,
            )
            return

        build_printer_dialog(
            self.window,
            printers,
            self.settings.get("printer", ""),
            self._on_printer_selected,
        )

    def _on_printer_selected(self, printer_name):
        try:
            save_printer_name(printer_name)
        except (OSError, configparser.Error) as exc:
            logger.exception("Unable to save selected printer")
            self.show_message(
                "Printer Save Error",
                f"The selected printer could not be saved:\n{exc}",
                0,
            )
            return False

        self.settings["printer"] = printer_name
        self.refresh_available_status()
        return True

    def set_importing(self, importing):
        button_state = tk.DISABLED if importing else tk.NORMAL
        for button in self.buttons:
            button.configure(state=button_state)

        if importing:
            self.set_message_bar("Import in progress...")
        if self.window is not None:
            self.window.update_idletasks()

    def set_message_bar(self, message):
        if self.message_bar is not None:
            self.message_bar.configure(text=message)
        if self.window is not None:
            self.window.update_idletasks()

    def refresh_available_status(self):
        try:
            available_count = self.roll_importer.get_available_count()
        except sqlite3.Error:
            logger.exception("Unable to count available vouchers in the database")
            self.set_message_bar("Available voucher count is unavailable.")
            return

        self.set_message_bar(f"Vouchers available: {available_count}")

    def kill_app(self, show_warnings=True):
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception as exc:
                logger.exception("Unable to stop tray icon")
                if show_warnings:
                    self.show_message(
                        "Tray Cleanup Warning",
                        "The app will close, but the tray icon could not be removed:\n"
                        f"{exc}",
                        0,
                    )
            self.tray_icon = None

        if self.window is not None and self.window.winfo_exists():
            try:
                save_window_position(*get_window_geometry_position(self.window))
            except (OSError, ValueError) as exc:
                logger.exception("Unable to save window position")
                if show_warnings:
                    self.show_message(
                        "Window Position Warning",
                        "The window position could not be saved. "
                        f"The app will still close.\n\n{exc}",
                        0,
                    )
            self.window.destroy()

    def install_windows_shutdown_handler(self):
        if os.name != "nt" or self.window is None:
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_window_proc = getattr(
            user32,
            "SetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "SetWindowLongW",
        )
        set_window_proc.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
        )
        set_window_proc.restype = ctypes.c_void_p

        self._call_window_proc = user32.CallWindowProcW
        self._call_window_proc.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self._call_window_proc.restype = ctypes.c_ssize_t

        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        self._shutdown_window_proc = callback_type(
            self.handle_windows_shutdown_message
        )
        callback_address = ctypes.cast(
            self._shutdown_window_proc, ctypes.c_void_p
        )
        ctypes.set_last_error(0)
        original_proc = set_window_proc(
            self.window.winfo_id(),
            GWL_WNDPROC,
            callback_address,
        )
        if not original_proc:
            error_code = ctypes.get_last_error()
            self._shutdown_window_proc = None
            self._call_window_proc = None
            raise ctypes.WinError(error_code)
        self._original_window_proc = original_proc

    def handle_windows_shutdown_message(self, hwnd, message, wparam, lparam):
        if message == WM_QUERYENDSESSION:
            return 1
        if message == WM_ENDSESSION and wparam:
            self.kill_app(show_warnings=False)
            return 0
        if self._original_window_proc is None or self._call_window_proc is None:
            return 0
        return self._call_window_proc(
            self._original_window_proc,
            hwnd,
            message,
            wparam,
            lparam,
        )

    def show_about(self):
        self.show_message(
            f"Version: {__version__}",
            "Licensed under the MIT License.",
            0,
        )

    def install_posix_signal_handlers(self):
        """Handle SIGTERM/SIGINT (logout, shutdown, Ctrl-C) gracefully on POSIX.

        The signal handler runs on the main thread and schedules kill_app on
        the Tk event loop, mirroring the Windows WM_ENDSESSION behaviour.
        """
        if os.name == "nt" or self.window is None:
            return

        def handle_signal(signum, _frame):
            logger.info("Received signal %s; shutting down gracefully", signum)
            # Signals arrive on the main thread between bytecode instructions,
            # but Tk calls must run on the event loop, so schedule via after().
            try:
                self.window.after(0, self.kill_app, False)
            except RuntimeError:
                # Window already destroyed; nothing left to clean up via Tk.
                pass

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handle_signal)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Unable to install handler for signal %s: %s", sig, exc
                )

    def close_and_exit(self, _event):
        self.kill_app()

    def build_main_window(self):
        window_x, window_y = load_window_position()
        self.window = tk.Tk()
        self.window.title("Wifi Vouchers")
        self.window.geometry(f"170x385{window_x:+d}{window_y:+d}")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.kill_app)

        title_label = tk.Label(
            self.window,
            text="Wifi Vouchers",
            font=("Arial", 12, "bold"),
        )
        title_label.place(x=5, y=0, width=160, height=35)

        buttons = (
            ("Print Voucher", "blue", self.print_vouchers, 40, 2),
            ("Import Roll", "green", self.import_roll, 100, 1),
            ("Select printer", "green", self.select_printer, 140, 1),
            ("Open config", "green", edit_ini_file, 180, 1),
            ("Minimize", "red", self.minimize_to_tray, 220, 1),
            ("Exit", "red", self.kill_app, 260, 1),
            ("About", "purple", self.show_about, 300, 1),
        )
        self.buttons = []
        for text, color, command, y_position, height in buttons:
            is_linux_minimize = IS_LINUX and text == "Minimize"
            button = tk.Button(
                self.window,
                text=text,
                width=15,
                height=height,
                fg=color,
                command=command,
                state=tk.DISABLED if is_linux_minimize else tk.NORMAL,
                disabledforeground="gray" if is_linux_minimize else color,
            )
            if IS_LINUX:
                button.place(relx=0.5, y=y_position, anchor=tk.N)
            else:
                button.place(x=30, y=y_position)
            if not is_linux_minimize:
                button.bind("<Return>", self.handle_return_key)
            self.buttons.append(button)

        self.message_bar = tk.Label(
            self.window,
            text="",
            anchor=tk.W,
            justify=tk.LEFT,
            relief=tk.SUNKEN,
            padx=4,
            wraplength=145,
        )
        self.message_bar.place(x=5, y=335, width=160, height=45)
        self.refresh_available_status()

        self.window.bind("<Escape>", self.close_and_exit)
        self.window.bind("x", self.close_and_exit)
        self.window.bind("p", lambda _event: self.print_vouchers())
        self.window.bind("<Return>", self.handle_return_key)
        try:
            self.install_windows_shutdown_handler()
        except (OSError, AttributeError) as exc:
            logger.exception("Unable to register Windows shutdown handler")
            self.show_message(
                "Windows Shutdown Handler Error",
                "Windows shutdown notifications could not be registered. "
                f"The app may not close gracefully during shutdown.\n\n{exc}",
                0,
            )
        self.install_posix_signal_handlers()
        return self.window

    def main(self):
        self.build_main_window()
        try:
            self.start_tray()
        except Exception as exc:
            logger.exception("Unable to start the system tray icon")
            self.show_message(
                "Tray Startup Error",
                f"The system tray icon could not be started:\n{exc}",
                0,
            )
        else:
            if self.settings["start_minimized"]:
                self.minimize_to_tray()
        self.window.mainloop()


# show_message and other platform backends are imported from platform.py.


def _read_config_parser():
    parser = configparser.ConfigParser()
    try:
        config_text = CONFIG_PATH.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(
            f"Unable to read vouchers.ini as a UTF-8 text file: {exc}"
        ) from exc

    config_text = "\n".join(
        line for line in config_text.splitlines() if line.strip("\x00")
    )
    try:
        parser.read_string(config_text, source=str(CONFIG_PATH))
    except configparser.Error as exc:
        raise ValueError(
            f"Invalid vouchers.ini configuration. Please check your settings.\n\n{exc}"
        ) from exc
    return parser


# Read and persist window placement in the INI file.
def load_window_position():
    parser = _read_config_parser()

    try:
        return (
            parser.getint("WINDOW", "x", fallback=DEFAULT_WINDOW_POSITION[0]),
            parser.getint("WINDOW", "y", fallback=DEFAULT_WINDOW_POSITION[1]),
        )
    except ValueError as exc:
        raise ValueError("Invalid window position in vouchers.ini") from exc


def get_window_geometry_position(window):
    geometry = window.geometry()
    match = re.search(r"([+-]\d+)([+-]\d+)$", geometry)
    if match is None:
        raise ValueError(f"Unable to read window position from geometry: {geometry}")
    return int(match.group(1)), int(match.group(2))


def build_printer_dialog(parent_window, printers, current_printer, on_select):
    """Show a modal Tk dialog for choosing a printer from `printers`.

    `on_select` receives the chosen printer name and should return True when
    the selection was accepted (the dialog closes); returning False keeps the
    dialog open, e.g. when saving the choice failed.
    """
    dialog = tk.Toplevel(parent_window)
    dialog.title("Select printer")
    dialog_x = parent_window.winfo_rootx() + 30
    dialog_y = parent_window.winfo_rooty() + 30
    dialog.geometry(f"302x210{dialog_x:+d}{dialog_y:+d}")
    dialog.resizable(False, False)
    dialog.transient(parent_window)

    tk.Label(dialog, text="Choose a Windows printer:").pack(
        padx=12, pady=(12, 4), anchor=tk.W
    )
    printer_list_frame = tk.Frame(dialog)
    printer_list_frame.pack(fill=tk.BOTH, expand=True, padx=12)
    printer_scrollbar = tk.Scrollbar(printer_list_frame)
    printer_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    printer_list = tk.Listbox(
        printer_list_frame,
        height=6,
        yscrollcommand=printer_scrollbar.set,
    )
    printer_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    printer_scrollbar.configure(command=printer_list.yview)
    for printer_name in printers:
        printer_list.insert(tk.END, printer_name)

    selected_index = next(
        (
            index
            for index, printer_name in enumerate(printers)
            if printer_name.casefold() == (current_printer or "").casefold()
        ),
        0,
    )
    printer_list.selection_set(selected_index)
    printer_list.activate(selected_index)
    printer_list.see(selected_index)

    def save_selection():
        selection = printer_list.curselection()
        if not selection:
            tk.messagebox.showinfo(
                "No Printer Selected",
                "Choose a printer from the list, or click Cancel.",
                parent=dialog,
            )
            return

        if on_select(printers[selection[0]]):
            dialog.destroy()

    actions = tk.Frame(dialog)
    actions.pack(pady=12)
    tk.Button(actions, text="Select", command=save_selection).pack(
        side=tk.LEFT, padx=4
    )
    tk.Button(actions, text="Cancel", command=dialog.destroy).pack(
        side=tk.LEFT, padx=4
    )
    printer_list.bind("<Double-Button-1>", lambda _event: save_selection())
    dialog.bind("<Return>", lambda _event: save_selection())
    dialog.bind("<Escape>", lambda _event: dialog.destroy())
    dialog.grab_set()


def save_config_value(section, updates):
    """Atomically persist one or more key/value pairs in vouchers.ini.

    Reads the current configuration, applies `updates` (a dict of key -> value)
    to `section`, and writes the file back via a temporary file plus os.replace.
    Any OSError is raised after the temporary file is cleaned up.
    """
    parser = _read_config_parser()
    if not parser.has_section(section) and section != "DEFAULT":
        parser.add_section(section)

    for key, value in updates.items():
        parser[section][key] = str(value)

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_PATH.parent,
            prefix=f".{CONFIG_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as config_file:
            temporary_path = Path(config_file.name)
            parser.write(config_file)
        os.replace(temporary_path, CONFIG_PATH)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    "Unable to remove temporary config file: %s",
                    temporary_path,
                )
        raise


def save_window_position(x_position, y_position):
    save_config_value("WINDOW", {"x": x_position, "y": y_position})


def save_active_roll_number(roll_number):
    ensure_config_file()
    save_config_value("DEFAULT", {"roll_number": roll_number})


def save_compact_database_setting(enabled):
    ensure_config_file()
    save_config_value("DEFAULT", {"compact_database": str(enabled).lower()})


def save_printer_name(printer_name):
    printer_name = printer_name.strip()
    if not printer_name:
        raise ValueError("A printer name is required.")

    ensure_config_file()
    save_config_value("PRINTER SETTINGS", {"printer": printer_name})


# Load application settings from vouchers.ini and convert numeric fields.
def load_settings():
    ensure_config_file()
    parser = _read_config_parser()
    try:
        return Settings.from_configparser(parser)
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "Invalid vouchers.ini configuration. Please check your settings."
        ) from exc


# print_printer_bytes is provided by the platform-specific printer module.


# build QR code image that fits thermal printer width.
def build_qr_image(qr_text):
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=4,
    )
    qr.add_data(qr_text)
    qr.make(fit=True)
    qr_modules = qr.get_matrix()
    module_count = len(qr_modules)
    dots_per_module = max(
        1,
        min(MAX_QR_DOTS_PER_MODULE, MAX_QR_WIDTH_DOTS // module_count),
    )
    width = module_count * dots_per_module
    image = Image.new("1", (width, width), 1)
    draw = ImageDraw.Draw(image)
    for module_y, module_row in enumerate(qr_modules):
        for module_x, is_black in enumerate(module_row):
            if is_black:
                left = module_x * dots_per_module
                top = module_y * dots_per_module
                draw.rectangle(
                    (
                        left,
                        top,
                        left + dots_per_module - 1,
                        top + dots_per_module - 1,
                    ),
                    fill=0,
                )

    scaled_width = max(1, int(round(width * QR_IMAGE_SCALE_FACTOR)))
    scaled_height = max(1, int(round(width * QR_IMAGE_SCALE_FACTOR)))
    resample = getattr(Image, "Resampling", Image).NEAREST
    return image.resize((scaled_width, scaled_height), resample)


# Convert a monochrome image into the ESC/POS GS v 0 raster command.
def image_to_raster(image):
    image = image.convert("1")
    width, height = image.size
    row_bytes = (width + 7) // 8
    raster = bytearray()
    pixels = image.load()

    for y_position in range(height):
        row = bytearray(row_bytes)
        for x_position in range(width):
            if pixels[x_position, y_position] == 0:
                row[x_position // 8] |= 0x80 >> (x_position % 8)
        raster.extend(row)

    return (
        b"\x1d\x76\x30\x00"
        + row_bytes.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + raster
    )


# Convert QR modules into the raster format expected by the thermal printer.
def build_qr_raster(qr_text):
    return image_to_raster(build_qr_image(qr_text))


def load_print_font(font_scale=1.0):
    linux_font_size = max(1, round(LINUX_PRINT_TEXT_SIZE * font_scale))
    windows_font_size = max(1, round(PRINT_TEXT_SIZE * font_scale))
    fallback_font_size = max(1, round(FALLBACK_PRINT_TEXT_SIZE * font_scale))

    if IS_LINUX:
        try:
            return ImageFont.load_default(size=linux_font_size)
        except TypeError:
            linux_font_paths = (
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
                Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
                Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
            )
            for font_path in linux_font_paths:
                try:
                    return ImageFont.truetype(
                        str(font_path),
                        linux_font_size,
                    )
                except OSError:
                    continue

    for font_path in (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "calibri.ttf",
    ):
        try:
            return ImageFont.truetype(str(font_path), windows_font_size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=fallback_font_size)
    except TypeError:
        return ImageFont.load_default()


# Render the complete receipt as one bitmap before sending it to the printer.
def build_receipt_raster(lines, qr_data, font_scale=1.0):
    font = load_print_font(font_scale)
    text_lines = [line or "" for line in lines]
    line_heights = []
    for line in text_lines:
        bounds = ImageDraw.Draw(Image.new("1", (1, 1))).textbbox(
            (0, 0), line, font=font
        )
        line_heights.append(max(1, bounds[3] - bounds[1]))

    qr_image = build_qr_image(qr_data)
    text_height = sum(line_heights) + PRINT_LINE_GAP * max(0, len(text_lines) - 1)
    image_height = (
        PRINT_TOP_MARGIN_DOTS
        + text_height
        + PRINT_LINE_GAP * 2
        + qr_image.height
        + PRINT_MARGIN_DOTS
        + PRINT_END_MARGIN_DOTS
    )
    image = Image.new("1", (PRINT_WIDTH_DOTS, image_height), 1)
    draw = ImageDraw.Draw(image)
    y_position = PRINT_TOP_MARGIN_DOTS
    for line, line_height in zip(text_lines, line_heights):
        bounds = draw.textbbox((0, 0), line, font=font)
        text_width = bounds[2] - bounds[0]
        x_position = (PRINT_WIDTH_DOTS - text_width) // 2 - bounds[0]
        draw.text((x_position, y_position - bounds[1]), line, fill=0, font=font)
        y_position += line_height + PRINT_LINE_GAP

    y_position += PRINT_LINE_GAP
    image.paste(qr_image, ((PRINT_WIDTH_DOTS - qr_image.width) // 2, y_position))
    return image_to_raster(image)


# Validate URL-related settings and encode voucher details into the pfSense login URL.
def build_voucher_url(voucher_code, settings):
    required_settings = ("server_address", "zone_name")
    missing_settings = [
        name for name in required_settings if not settings.get(name, "").strip()
    ]
    if missing_settings:
        raise ValueError(
            "Set the following values in vouchers.ini before printing: "
            + ", ".join(missing_settings)
        )

    url_scheme = settings.get("url_scheme", "http").strip().lower()
    if url_scheme not in {"http", "https"}:
        raise ValueError("url_scheme in vouchers.ini must be 'http' or 'https'")

    query = urlencode(
        (
            ("zone", settings["zone_name"]),
            ("auth_voucher", voucher_code),
            ("accept", "Connect"),
        )
    )
    return f"{url_scheme}://{settings['server_address']}:8002/index.php?{query}"


# Assemble an all-raster receipt and paper-cut commands.
def build_thermal_bytes(lines, qr_data, font_scale=1.0):
    data = bytearray()
    data.extend(ESC_POS_INITIALIZE)
    data.extend(build_receipt_raster(lines, qr_data, font_scale))
    data.extend(b"\n\n")
    data.extend(ESC_POS_FEED_AND_CUT)
    return bytes(data)


# Create the starter INI only when no configuration file exists.
def ensure_config_file():
    if CONFIG_PATH.exists():
        return False

    try:
        CONFIG_PATH.write_text(DEFAULT_INI_TEMPLATE, encoding="utf-8")
    except OSError:
        logger.exception("Unable to create configuration file: %s", CONFIG_PATH)
        raise
    return True


# Reclaim unused SQLite pages before the application opens its database.
def compact_voucher_database(database_path):
    if not database_path.exists():
        logger.info(
            "Skipping database compaction because the database does not exist: %s",
            database_path,
        )
        return False

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("VACUUM")
    logger.info("Compacted voucher database: %s", database_path)
    return True


# Open the configuration with the platform-specific editor helper.
def edit_ini_file():
    created_default = ensure_config_file()
    if created_default:
        show_message(
            "Configuration File Created",
            f"A default vouchers.ini file was created at:\n{CONFIG_PATH}\n\nPlease update it before printing.",
            0,
        )
    _platform_edit_ini_file(CONFIG_PATH)


# check_printer_online and list_available_printers are provided by the
# platform-specific printer module.


# Logging remains enabled for older INI files unless explicitly disabled.
def load_logging_enabled():
    try:
        parser = _read_config_parser()
        return parser.getboolean("DEFAULT", "logging_enabled", fallback=True)
    except (OSError, ValueError):
        return True


# Set up optional rotating diagnostics without logging voucher contents.
def configure_logging(enabled):
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.disabled = not enabled
    if not enabled:
        return False

    try:
        handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
    except OSError as exc:
        logger.disabled = True
        show_message(
            "Logging Error",
            f"Unable to create the diagnostic log at:\n{LOG_PATH}\n\n{exc}",
            0,
        )
        return False
    return True


# The single-instance lock is provided by the platform UI module
# (Windows mutex or Linux advisory file lock); see platform.py.


# Raised when startup cannot continue; carries the exit code for the process.
class StartupError(Exception):
    def __init__(self, message, exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


# Startup owns the single-instance lock until the UI exits.
def main():
    try:
        is_first_instance = acquire_single_instance()
    except OSError as exc:
        show_message(
            "Startup Error",
            f"Unable to check whether another app instance is running:\n{exc}",
            0,
        )
        raise StartupError(str(exc), exit_code=1) from exc

    if not is_first_instance:
        show_message(
            "Already Running",
            "Wifi Voucher Codes is already running.",
            0,
        )
        return

    try:
        try:
            configure_logging(load_logging_enabled())
            created_default = ensure_config_file()
            settings = load_settings()
        except (FileNotFoundError, ValueError) as exc:
            logger.exception("Unable to load application configuration")
            show_message("Configuration Error", str(exc), 0)
            raise StartupError(str(exc), exit_code=1) from exc

        if settings["compact_database"]:
            try:
                compacted = compact_voucher_database(DATABASE_PATH)
                if compacted:
                    save_compact_database_setting(False)
                    settings["compact_database"] = False
            except (OSError, sqlite3.Error) as exc:
                logger.exception("Unable to compact voucher database")
                show_message(
                    "Database Compaction Warning",
                    "The voucher database could not be compacted. "
                    f"The app will continue starting.\n\n{exc}",
                    0,
                )

        if created_default:
            logger.info("Created default configuration file: %s", CONFIG_PATH)
            show_message(
                "Configuration File Created",
                f"A default vouchers.ini file was created at:\n{CONFIG_PATH}\n\nPlease update it before printing.",
                0,
            )

        try:
            build_voucher_url("", settings)
        except ValueError as exc:
            logger.warning("Voucher URL configuration is invalid: %s", exc)
            show_message("Configuration Error", str(exc), 0)

        app = VoucherApp(settings)
        app.main()
    finally:
        release_single_instance()


if __name__ == "__main__":
    try:
        main()
    except StartupError as exc:
        sys.exit(exc.exit_code)
