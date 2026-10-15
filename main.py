APP_VERSION = "0.5.0"

import ctypes
from contextlib import closing
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

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

try:
    import configparser
    import tkinter as tk
    from tkinter import filedialog
    from PIL import Image, ImageDraw, ImageFont
    import pystray
    import win32print
    import wmi
    import qrcode
    from roll_importer import VoucherRollImporter
except ImportError as exc:
    sys.exit(
        IMPORT_ERROR_MESSAGES.get(
            exc.name,
            f"Unable to import dependency: {exc}",
        )
    )

# Use the executable's folder when packaged, or the script folder during development.
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
INSTANCE_MUTEX_NAME = "Local\\WifiVoucherCodes_SingleInstance"
ERROR_ALREADY_EXISTS = 183
logger = logging.getLogger(__name__)
_instance_mutex_handle = None
_instance_kernel32 = None

# This template is written beside the app when its configuration is missing.
DEFAULT_INI_TEMPLATE = '''[DEFAULT]
header = Wifi Voucher
https_address = example.com
zone_name = Example WiFi
redir_url = https://example.com
url_scheme = https
logging_enabled = true
start_minimized = false
compact_database = false

[PRINTER SETTINGS]
printer = Epson

[WINDOW]
x = 10
y = 10
'''

# ESC/POS commands configure and finish the thermal-printer receipt.
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
PRINT_WIDTH_DOTS = 384
PRINT_MARGIN_DOTS = 8
PRINT_TOP_MARGIN_DOTS = 102
PRINT_END_MARGIN_DOTS = 203
PRINT_TEXT_SIZE = 36
PRINT_LINE_GAP = 6
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
GWL_WNDPROC = -4
DEFAULT_WINDOW_POSITION = (10, 10)


# The main application coordinates voucher selection, printing, and the UI.
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

    # Read one voucher, verify the printer, and mark it printed after confirmation.
    def get_voucher_code(self):
        roll_number = self.settings.get("roll_number")
        if roll_number is None:
            self.show_message(
                "Import Roll Required",
                "Import a voucher roll before printing.",
                0,
            )
            return

        try:
            voucher = self.roll_importer.get_next_voucher(roll_number)
        except sqlite3.Error as exc:
            logger.exception("Unable to read voucher roll from the database")
            self.show_message(
                "Voucher Database Error",
                f"Unable to read voucher roll from the database:\n{exc}",
                0,
            )
            return

        if voucher is None:
            logger.info("No unprinted vouchers remain in roll %s", roll_number)
            self.show_message(
                "Voucher Roll Empty",
                f"No unprinted voucher codes remain in roll {roll_number}.",
                0,
            )
            return

        voucher_id, voucher_code = voucher
        try:
            printer_status = check_printer_online(self.settings["printer"])
        except Exception as exc:
            logger.exception("Unable to check printer status")
            self.show_message(
                "Printer Error",
                f"Unable to check printer status:\n{exc}",
                0,
            )
            return

        if printer_status == "not_found":
            logger.warning(
                "Configured printer was not found: %s", self.settings["printer"]
            )
            self.show_message(
                "Printer Not Found",
                f"Configured printer was not found:\n{self.settings['printer']}",
                0,
            )
            return
        if printer_status == "offline":
            logger.warning(
                "Configured printer is offline: %s", self.settings["printer"]
            )
            self.show_message(
                "Printer Offline",
                f"The configured printer is offline:\n{self.settings['printer']}",
                0,
            )
            return

        try:
            printed = self.ticket_print(voucher_code)
        except ValueError:
            return

        if not printed:
            return

        response = self.show_message(
            "Print Voucher Codes",
            "If voucher printed then click OK otherwise click cancel",
            1,
        )
        if response == 1:
            try:
                marked_printed = self.roll_importer.mark_printed(
                    roll_number, voucher_id
                )
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

    # Prepare the receipt and submit it to the configured printer.
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

        print_data = build_thermal_bytes(lines, voucher_url)
        printed = print_printer_bytes(self.settings["printer"], print_data)
        if printed:
            logger.info("Print job submitted to printer: %s", self.settings["printer"])
        return printed

    # UI commands and window lifecycle handlers.
    def print_vouchers(self):
        self.get_voucher_code()

    def handle_return_key(self, _event):
        self.print_vouchers()
        return "break"

    def minimize_to_tray(self):
        if self.window is not None:
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
        self.tray_icon.run_detached()

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

        result = None
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
            except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
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
            f"Version: {APP_VERSION}",
            "Licensed under the MIT License.",
            0,
        )

    def close_and_exit(self, _event):
        self.kill_app()

    def build_main_window(self):
        window_x, window_y = load_window_position()
        self.window = tk.Tk()
        self.window.title("Wifi Vouchers")
        self.window.geometry(f"170x345{window_x:+d}{window_y:+d}")
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
            ("Open config", "green", edit_ini_file, 140, 1),
            ("Minimize", "red", self.minimize_to_tray, 180, 1),
            ("Exit", "red", self.kill_app, 220, 1),
            ("About", "purple", self.show_about, 260, 1),
        )
        self.buttons = []
        for text, color, command, y_position, height in buttons:
            button = tk.Button(
                self.window,
                text=text,
                width=15,
                height=height,
                fg=color,
                command=command,
            )
            button.place(x=30, y=y_position)
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
        self.message_bar.place(x=5, y=295, width=160, height=45)
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


# Display Windows-native message boxes used for status and error feedback.
def show_message(title, text, style=0):
    return ctypes.windll.user32.MessageBoxW(0, text, title, style)


# Read and persist window placement in the INI file.
def load_window_position():
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError(
            f"Unable to locate configuration file: {CONFIG_PATH}"
        )

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


def save_window_position(x_position, y_position):
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError(
            f"Unable to locate configuration file: {CONFIG_PATH}"
        )
    if not parser.has_section("WINDOW"):
        parser.add_section("WINDOW")

    parser["WINDOW"]["x"] = str(x_position)
    parser["WINDOW"]["y"] = str(y_position)
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


def save_active_roll_number(roll_number):
    ensure_config_file()
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError(f"Unable to locate configuration file: {CONFIG_PATH}")

    parser["DEFAULT"]["roll_number"] = str(roll_number)
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


def save_compact_database_setting(enabled):
    ensure_config_file()
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError(f"Unable to locate configuration file: {CONFIG_PATH}")

    parser["DEFAULT"]["compact_database"] = str(enabled).lower()
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


# Load application settings from vouchers.ini and convert numeric fields.
def load_settings():
    ensure_config_file()
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError(f"Unable to locate configuration file: {CONFIG_PATH}")

    try:
        return {
            "roll_number": parser.getint(
                "DEFAULT", "roll_number", fallback=None
            ),
            "header": parser["DEFAULT"]["header"],
            "https_address": parser.get("DEFAULT", "https_address", fallback=""),
            "zone_name": parser.get("DEFAULT", "zone_name", fallback=""),
            "redir_url": parser.get("DEFAULT", "redir_url", fallback=""),
            "url_scheme": parser.get(
                "DEFAULT", "url_scheme", fallback="http"
            ).strip().lower(),
            "logging_enabled": parser.getboolean(
                "DEFAULT", "logging_enabled", fallback=True
            ),
            "start_minimized": parser.getboolean(
                "DEFAULT", "start_minimized", fallback=False
            ),
            "compact_database": parser.getboolean(
                "DEFAULT", "compact_database", fallback=False
            ),
            "printer": parser["PRINTER SETTINGS"]["printer"],
        }
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "Invalid vouchers.ini configuration. Please check your settings."
        ) from exc


# Send raw receipt bytes to Windows printing services and report failures.
def print_printer_bytes(printer_name, data):
    try:
        h_printer = win32print.OpenPrinter(printer_name)
    except Exception as exc:
        logger.exception("Unable to open printer: %s", printer_name)
        show_message(
            "Printer Error",
            f"Unable to open printer '{printer_name}':\n{exc}",
            0,
        )
        return False

    document_started = False
    try:
        win32print.StartDocPrinter(h_printer, 1, ("Voucher Code", None, "RAW"))
        document_started = True
        win32print.StartPagePrinter(h_printer)
        bytes_written = win32print.WritePrinter(h_printer, data)
        if bytes_written != len(data):
            logger.error(
                "Printer accepted only %s of %s bytes: %s",
                bytes_written,
                len(data),
                printer_name,
            )
            win32print.AbortPrinter(h_printer)
            show_message(
                "Print Failed",
                "The printer did not accept all voucher data. The voucher was not removed.",
                0,
            )
            return False
        win32print.EndPagePrinter(h_printer)
        win32print.EndDocPrinter(h_printer)
    except Exception as exc:
        logger.exception("Print job failed on printer: %s", printer_name)
        abort_error = ""
        if document_started:
            try:
                win32print.AbortPrinter(h_printer)
            except Exception as abort_exc:
                logger.exception("Unable to cancel failed print job")
                abort_error = f"\nAdditionally, cancelling the print job failed:\n{abort_exc}"
        show_message(
            "Print Failed",
            f"Unable to print voucher:\n{exc}{abort_error}\n\nThe voucher was not removed.",
            0,
        )
        return False
    finally:
        try:
            win32print.ClosePrinter(h_printer)
        except Exception as exc:
            logger.exception("Unable to close printer handle")
            show_message(
                "Printer Cleanup Warning",
                f"The print operation finished, but the printer handle could not be closed:\n{exc}",
                0,
            )
    return True


# Build a QR image at the largest size that fits the thermal printer width.
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
    return image


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


def load_print_font():
    for font_path in (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "calibri.ttf",
    ):
        try:
            return ImageFont.truetype(str(font_path), PRINT_TEXT_SIZE)
        except OSError:
            continue
    return ImageFont.load_default()


# Render the complete receipt as one bitmap before sending it to the printer.
def build_receipt_raster(lines, qr_data):
    font = load_print_font()
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


# Validate URL-related settings and encode voucher details into the login URL.
def build_voucher_url(voucher_code, settings):
    required_settings = ("https_address", "zone_name", "redir_url")
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
            ("redirurl", settings["redir_url"]),
            ("voucher", voucher_code),
        )
    )
    return f"{url_scheme}://{settings['https_address']}:8002/index.php?{query}"


# Assemble an all-raster receipt and paper-cut commands.
def build_thermal_bytes(lines, qr_data):
    data = bytearray()
    data.extend(ESC_POS_INITIALIZE)
    data.extend(build_receipt_raster(lines, qr_data))
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


# Open the configuration in the user's Windows default editor.
def edit_ini_file():
    created_default = ensure_config_file()
    if created_default:
        show_message(
            "Configuration File Created",
            f"A default vouchers.ini file was created at:\n{CONFIG_PATH}\n\nPlease update it before printing.",
            0,
        )
    os.startfile(str(CONFIG_PATH))


# Match the configured printer and distinguish offline from missing.
def check_printer_online(printer_name):
    target_name = (printer_name or "").strip().lower()
    printers = wmi.WMI().Win32_Printer()
    for printer in printers:
        caption = getattr(printer, "caption", "") or ""
        if caption.strip().lower() == target_name:
            return "offline" if printer.WorkOffline else "online"
    return "not_found"


# Logging remains enabled for older INI files unless explicitly disabled.
def load_logging_enabled():
    parser = configparser.ConfigParser()
    try:
        if not parser.read(CONFIG_PATH):
            return True
        return parser.getboolean("DEFAULT", "logging_enabled", fallback=True)
    except (configparser.Error, OSError, ValueError):
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


# A named Windows mutex prevents two copies from consuming the same roll.
def acquire_single_instance():
    global _instance_kernel32, _instance_mutex_handle

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _instance_kernel32 = kernel32
    _instance_mutex_handle = handle
    return True


# Release the mutex so a later launch can become the active instance.
def release_single_instance():
    global _instance_kernel32, _instance_mutex_handle

    if _instance_mutex_handle is not None:
        try:
            _instance_kernel32.CloseHandle(_instance_mutex_handle)
        finally:
            _instance_mutex_handle = None
            _instance_kernel32 = None


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
        sys.exit(1)

    if not is_first_instance:
        show_message(
            "Already Running",
            "Wifi Voucher Codes is already running.",
            0,
        )
        return

    try:
        configure_logging(load_logging_enabled())
        created_default = ensure_config_file()
        try:
            settings = load_settings()
        except (FileNotFoundError, ValueError) as exc:
            logger.exception("Unable to load application configuration")
            show_message("Configuration Error", str(exc), 0)
            sys.exit(str(exc))

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
    main()
