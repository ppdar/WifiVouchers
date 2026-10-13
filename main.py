APP_VERSION = "0.3.1"

import ctypes
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

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

SCRIPT_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_PATH = SCRIPT_DIR / "vouchers.ini"

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


class VoucherApp:
    def __init__(self, settings):
        self.settings = settings
        self.window = None

    def show_message(self, title, text, style=0):
        return show_message(title, text, style)

    def get_voucher_code(self):
        voucher_path = SCRIPT_DIR / self.settings["roll"]

        try:
            with voucher_path.open("r", encoding="utf-8") as voucher_file:
                voucher_lines = voucher_file.read().splitlines()
        except OSError as exc:
            raise OSError(f"Unable to read voucher file: {voucher_path}") from exc

        row_index = None
        voucher_code = None
        for index, line in enumerate(voucher_lines):
            if line.startswith('"'):
                row_index = index
                voucher_code = line.replace('"', "").strip()
                break

        if row_index is None or voucher_code is None:
            raise ValueError("No voucher code found in the roll file")

        self.ticket_print(voucher_code)
        voucher_lines.pop(row_index)

        response = self.show_message(
            "Print Voucher Codes",
            "If voucher printed then click OK otherwise click cancel",
            1,
        )

        if response == 1:
            try:
                with voucher_path.open("w", encoding="utf-8") as voucher_file:
                    voucher_file.writelines(
                        f"{line}\n" for line in voucher_lines
                    )
            except OSError as exc:
                raise OSError(
                    f"Unable to save updated voucher file: {voucher_path}"
                ) from exc

    def ticket_print(self, print_voucher_code):
        lines = self.settings["header"].split("\n")
        lines.extend(print_voucher_code.split("\n\n"))

        try:
            voucher_url = build_voucher_url(print_voucher_code, self.settings)
        except ValueError as exc:
            self.show_message("Configuration Error", str(exc), 0)
            return

        print_data = build_thermal_bytes(lines, voucher_url)
        print_printer_bytes(self.settings["printer"], print_data)

    def print_vouchers(self):
        if check_printer_online(self.settings["printer"]) == "online":
            self.get_voucher_code()
        else:
            self.show_message("Printer Error", "Please check printer is on", 0)

    def kill_app(self):
        if self.window is not None and self.window.winfo_exists():
            save_window_position(
                self.window.winfo_rootx(),
                self.window.winfo_rooty(),
            )
            self.window.destroy()

    def show_about(self):
        self.show_message(
            f"Version: {APP_VERSION}",
            "Freely distributable under AGPL.",
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
            ("Edit ini file", "green", edit_ini_file, 60),
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


def show_message(title, text, style=0):
    return ctypes.windll.user32.MessageBoxW(0, text, title, style)


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
    with CONFIG_PATH.open("w", encoding="utf-8") as config_file:
        parser.write(config_file)


def load_settings():
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError("unable to locate vouchers.ini")

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
            "printer": parser["PRINTER SETTINGS"]["printer"],
            "font_type": parser["PRINTER SETTINGS"]["fonttype"],
            "font_size": int(parser["PRINTER SETTINGS"]["fontsize"]),
            "x": int(parser["PRINTER SETTINGS"]["x"]),
            "y": int(parser["PRINTER SETTINGS"]["y"]),
            "th": int(parser["PRINTER SETTINGS"]["th"]),
        }
    except (KeyError, ValueError) as exc:
        raise ValueError("Invalid vouchers.ini configuration") from exc


def print_printer_bytes(printer_name, data):
    try:
        h_printer = win32print.OpenPrinter(printer_name)
    except Exception:
        show_message("Printer Error", f"Printer not found: {printer_name}", 0)
        return

    try:
        win32print.StartDocPrinter(h_printer, 1, ("Voucher Code", None, "RAW"))
        win32print.StartPagePrinter(h_printer)
        win32print.WritePrinter(h_printer, data)
        win32print.EndPagePrinter(h_printer)
        win32print.EndDocPrinter(h_printer)
    finally:
        win32print.ClosePrinter(h_printer)


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


def append_qr_code(data, qr_text):
    data.extend(b"\n\n")
    data.extend(ESC_POS_CENTER)
    data.extend(build_qr_raster(qr_text))
    data.extend(ESC_POS_LEFT_ALIGN)


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


def edit_ini_file():
    subprocess.Popen(["notepad.exe", str(CONFIG_PATH)], shell=True)


def check_printer_online(printer_name):
    printers = wmi.WMI().Win32_Printer()
    for printer in printers:
        if printer.caption == printer_name:
            return "offline" if printer.WorkOffline else "online"
    return "offline"


def main():
    try:
        settings = load_settings()
    except (FileNotFoundError, ValueError):
        show_message("Error", "unable to locate vouchers.ini", 0)
        sys.exit("unable to locate vouchers.ini")

    app = VoucherApp(settings)
    app.main()


if __name__ == "__main__":
    main()
