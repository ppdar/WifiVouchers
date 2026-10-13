APP_VERSION = "0.4.0"

# Standard-library tools for Windows integration, file handling, and voucher URLs.
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

# Give users a direct message when a required runtime package is unavailable.
IMPORT_ERROR_MESSAGES = {
    "configparser": "configparser not installed.",
    "tkinter": "Tkinter not installed.",
    "win32print": "pywin32 printing support is missing.",
    "wmi": "wmi module is missing.",
    "qrcode": "qrcode module is missing.",
}

try:
    import configparser
    import tkinter as tk
    import win32print
    import wmi
    import qrcode
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
roll = roll.txt
header = Wifi Voucher
https_address = example.com
zone_name = Example WiFi
redir_url = https://example.com
url_scheme = https
logging_enabled = true

[PRINTER SETTINGS]
printer = Epson
fonttype =
fontsize = 24
x = 0
y = 0
th = 0

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
DEFAULT_WINDOW_POSITION = (10, 10)


# The main application coordinates voucher selection, printing, and the UI.
class VoucherApp:
    def __init__(self, settings):
        self.settings = settings
        self.window = None

    def show_message(self, title, text, style=0):
        return show_message(title, text, style)

    # Read one voucher, verify the printer, and remove the code only after confirmation.
    def get_voucher_code(self):
        voucher_path = SCRIPT_DIR / self.settings["roll"]

        try:
            with voucher_path.open("r", encoding="utf-8") as voucher_file:
                voucher_lines = voucher_file.read().splitlines()
        except FileNotFoundError:
            logger.error("Voucher roll file not found: %s", voucher_path)
            self.show_message(
                "Voucher Roll Error",
                f"Voucher roll file not found:\n{voucher_path}",
                0,
            )
            return
        except OSError as exc:
            logger.exception("Unable to read voucher roll file: %s", voucher_path)
            self.show_message(
                "Voucher Roll Error",
                f"Unable to read voucher roll file:\n{voucher_path}\n\n{exc}",
                0,
            )
            return

        row_index = None
        voucher_code = None
        for index, line in enumerate(voucher_lines):
            if line.startswith('"'):
                row_index = index
                voucher_code = line.replace('"', "").strip()
                break

        if row_index is None or not voucher_code:
            logger.warning("No voucher codes found in roll file: %s", voucher_path)
            self.show_message(
                "Voucher Roll Empty",
                f"No voucher codes found in the roll file:\n{voucher_path}",
                0,
            )
            return

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

        voucher_lines.pop(row_index)

        response = self.show_message(
            "Print Voucher Codes",
            "If voucher printed then click OK otherwise click cancel",
            1,
        )

        if response == 1:
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=voucher_path.parent,
                    prefix=f".{voucher_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as voucher_file:
                    temporary_path = Path(voucher_file.name)
                    voucher_file.writelines(f"{line}\n" for line in voucher_lines)
                os.replace(temporary_path, voucher_path)
            except OSError as exc:
                logger.exception("Unable to save updated voucher roll: %s", voucher_path)
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.exception(
                            "Unable to remove temporary voucher roll file: %s",
                            temporary_path,
                        )
                self.show_message(
                    "Voucher Roll Save Error",
                    "The voucher was printed, but the voucher roll could not be updated. "
                    "The voucher remains in the roll and could print again if retried.\n\n"
                    f"File: {voucher_path}\nError: {exc}",
                    0,
                )

    # Prepare the receipt and submit it to the configured printer.
    def ticket_print(self, print_voucher_code):
        lines = self.settings["header"].split("\n")
        lines.extend(print_voucher_code.split("\n\n"))

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

    def kill_app(self):
        if self.window is not None and self.window.winfo_exists():
            try:
                save_window_position(*get_window_geometry_position(self.window))
            except (OSError, ValueError) as exc:
                logger.exception("Unable to save window position")
                self.show_message(
                    "Window Position Warning",
                    f"The window position could not be saved. The app will still close.\n\n{exc}",
                    0,
                )
            self.window.destroy()

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
        self.window.title("Wifi Voucher Codes")
        self.window.geometry(f"170x200{window_x:+d}{window_y:+d}")
        self.window.protocol("WM_DELETE_WINDOW", self.kill_app)

        buttons = (
            ("Print Voucher", "blue", self.print_vouchers, 20),
            ("Open config", "green", edit_ini_file, 60),
            ("Exit", "red", self.kill_app, 100),
            ("About", "purple", self.show_about, 140),
        )
        for text, color, command, y_position in buttons:
            tk.Button(
                self.window,
                text=text,
                width=15,
                fg=color,
                command=command,
            ).place(x=30, y=y_position)

        self.window.bind("<Escape>", self.close_and_exit)
        self.window.bind("p", lambda _event: self.print_vouchers())
        return self.window

    def main(self):
        self.build_main_window()
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


# Load application settings from vouchers.ini and convert numeric fields.
def load_settings():
    ensure_config_file()
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError(f"Unable to locate configuration file: {CONFIG_PATH}")

    try:
        return {
            "roll": parser["DEFAULT"]["roll"],
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
            "printer": parser["PRINTER SETTINGS"]["printer"],
            "font_type": parser["PRINTER SETTINGS"]["fonttype"],
            "font_size": int(parser["PRINTER SETTINGS"]["fontsize"]),
            "x": int(parser["PRINTER SETTINGS"]["x"]),
            "y": int(parser["PRINTER SETTINGS"]["y"]),
            "th": int(parser["PRINTER SETTINGS"]["th"]),
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


# Convert QR modules into the raster format expected by the thermal printer.
def build_qr_raster(qr_text):
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=4,
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
    height = width
    row_bytes = (width + 7) // 8
    raster = bytearray()

    for module_row in qr_modules:
        for _ in range(dots_per_module):
            row = bytearray(row_bytes)
            for module_x, is_black in enumerate(module_row):
                if is_black:
                    for horizontal_scale in range(dots_per_module):
                        x_position = module_x * dots_per_module + horizontal_scale
                        row[x_position // 8] |= 0x80 >> (x_position % 8)
            raster.extend(row)

    raster_header = (
        b"\x1d\x76\x30\x00"
        + row_bytes.to_bytes(2, "little")
        + height.to_bytes(2, "little")
    )
    return raster_header + raster


# Add a centered QR image to the receipt stream.
def append_qr_code(data, qr_text):
    data.extend(b"\n\n")
    data.extend(ESC_POS_CENTER)
    data.extend(build_qr_raster(qr_text))
    data.extend(ESC_POS_LEFT_ALIGN)


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


# Assemble text, QR code, and paper-cut commands into one receipt payload.
def build_thermal_bytes(lines, qr_data):
    data = bytearray()
    data.extend(ESC_POS_INITIALIZE)
    data.extend(ESC_POS_LINE_SPACING)
    data.extend(ESC_POS_CENTER)
    data.extend(ESC_POS_BOLD_ON)

    for line in lines:
        if not line:
            data.extend(b"\n")
            continue
        data.extend(line.encode("cp1252", errors="ignore"))
        data.extend(b"\n")

    data.extend(ESC_POS_NORMAL_SIZE)
    data.extend(ESC_POS_BOLD_OFF)
    append_qr_code(data, qr_data)
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


# Run the app only when this file is launched directly.
if __name__ == "__main__":
    main()
