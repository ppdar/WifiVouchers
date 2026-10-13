APP_VERSION = "0.3"

import ctypes
import subprocess
import sys
from pathlib import Path

try:
    import configparser
except ImportError:
    sys.exit("configparser not installed.")

try:
    import tkinter as tk
except ImportError:
    sys.exit("Tkinter not installed.")

try:
    import win32print
except ImportError:
    sys.exit("pywin32 printing support is missing.")

try:
    import wmi
except ImportError:
    sys.exit("wmi module is missing.")

SCRIPT_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
    if "__file__" in globals()
    else Path.cwd()
)
CONFIG_PATH = SCRIPT_DIR / "vouchers.ini"
WORKING_DIR = SCRIPT_DIR


class VoucherApp:
    def __init__(self, settings):
        self.settings = settings
        self.window = None

    def show_message(self, title, text, style=0):
        return ctypes.windll.user32.MessageBoxW(0, text, title, style)

    def print_printer_bytes(self, printer_name, data):
        try:
            h_printer = win32print.OpenPrinter(printer_name)
        except Exception:
            self.show_message("Printer Error", f"Printer not found: {printer_name}", 0)
            return

        try:
            win32print.StartDocPrinter(h_printer, 1, ("Voucher Code", None, "RAW"))
            win32print.StartPagePrinter(h_printer)
            win32print.WritePrinter(h_printer, data)
            win32print.EndPagePrinter(h_printer)
            win32print.EndDocPrinter(h_printer)
        finally:
            win32print.ClosePrinter(h_printer)

    def get_voucher_code(self):
        voucher_path = WORKING_DIR / self.settings["roll"]

        try:
            with voucher_path.open("r", encoding="utf-8") as voucher_file:
                voucher_lines = voucher_file.read().splitlines()
        except OSError as exc:
            raise OSError(f"Unable to read voucher file: {voucher_path}") from exc

        row_index = -1
        voucher_code = None
        for index, line in enumerate(voucher_lines):
            if line.startswith('"'):
                row_index = index
                voucher_code = line.replace('"', "").strip()
                break

        if voucher_code is None:
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
                    voucher_file.writelines(f"{line}\n" for line in voucher_lines)
            except OSError as exc:
                raise OSError(f"Unable to save updated voucher file: {voucher_path}") from exc

    def ticket_print(self, print_voucher_code):
        lines = self.settings["header"].split("\n")
        lines.extend(print_voucher_code.split("\n\n"))
        print_data = build_thermal_bytes(lines)
        self.print_printer_bytes(self.settings["printer"], print_data)

    def print_vouchers(self):
        if check_printer_online(self.settings["printer"]) == "online":
            self.get_voucher_code()
        else:
            self.show_message("Printer Error", "Please check printer is on", 0)

    def kill_app(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.destroy()

    def show_about(self):
        self.show_message("Version: " + APP_VERSION, "Freely distributable under AGPL.", 0)

    def close_and_exit(self, event):
        self.kill_app()

    def build_main_window(self):
        self.window = tk.Tk()
        self.window.title("Wifi Voucher Codes")
        self.window.geometry("170x200+10+10")
        self.window.protocol("WM_DELETE_WINDOW", self.kill_app)

        tk.Button(self.window, text="Print Voucher", width=15, fg="blue", command=self.print_vouchers).place(x=30, y=20)
        tk.Button(self.window, text="Edit ini file", width=15, fg="green", command=edit_ini_file).place(x=30, y=60)
        tk.Button(self.window, text="Exit", fg="red", width=15, command=self.kill_app).place(x=30, y=100)
        tk.Button(self.window, text="About", width=15, fg="purple", command=self.show_about).place(x=30, y=140)

        self.window.bind("<Escape>", self.close_and_exit)
        self.window.bind("p", lambda _event: self.print_vouchers())
        return self.window

    def main(self):
        self.build_main_window()
        self.window.mainloop()


def show_message(title, text, style=0):
    return ctypes.windll.user32.MessageBoxW(0, text, title, style)


def load_settings():
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise FileNotFoundError("unable to locate vouchers.ini")

    try:
        return {
            "roll": parser["DEFAULT"]["roll"],
            "header": parser["DEFAULT"]["header"],
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


def build_thermal_bytes(lines):
    data = bytearray()
    data += b"\x1b\x40"
    data += b"\x1b\x33\x20"
    data += b"\x1b\x21\x00"

    for line in lines:
        if not line:
            data += b"\n"
            continue
        data += line.encode("cp1252", errors="ignore")
        data += b"\n"

    data += b"\n\n\x1d\x56\x41\x00"
    return bytes(data)


def edit_ini_file():
    subprocess.Popen(["notepad.exe", str(WORKING_DIR / CONFIG_PATH.name)], shell=True)


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
