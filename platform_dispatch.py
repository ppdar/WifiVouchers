"""OS detection and platform-specific backends for wifivouchers.

At import time this module detects the operating system once and loads the
matching implementations:

- read_csv_roll: platform CSV reader (windows_csv / linux_csv)
- check_printer_online, list_available_printers, print_printer_bytes:
  platform printer backend (win32print/wmi or CUPS/lp)
- show_message, edit_ini_file: native dialogs and file opening
- acquire_single_instance / release_single_instance: single-instance lock
  (Windows mutex or POSIX file lock)

Everything else in the application is platform-neutral and calls these
helpers without knowing which OS it is running on.
"""

import platform
import sys

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

if not (IS_WINDOWS or IS_LINUX):
    print(
        "WifiVouchers supports Windows and Linux only; "
        f"unsupported platform: {sys.platform}",
        file=sys.stderr,
    )
    sys.exit(1)


def _is_supported_linux_runtime(machine, maxsize):
    return machine.casefold() in {"x86_64", "amd64"} and maxsize > 2**32


if IS_LINUX:
    machine = platform.machine()
    if not _is_supported_linux_runtime(machine, sys.maxsize):
        python_bits = 64 if sys.maxsize > 2**32 else 32
        print(
            "WifiVouchers requires 64-bit x86_64 Linux and a 64-bit Python "
            f"interpreter; detected machine {machine!r} with a "
            f"{python_bits}-bit interpreter.",
            file=sys.stderr,
        )
        sys.exit(1)

    # On Linux, verify every required Python module is installed before the
    # GUI starts; report all missing modules at once with pip install hints.
    import importlib.util

    REQUIRED_MODULES = {
        "tkinter": "tkinter",
        "PIL": "Pillow",
        "qrcode": "qrcode",
        "pystray": "pystray",
        "cups": "pycups",  # optional; lp/lpstat fallback is used without it
    }

    missing = []
    for module_name, pip_name in REQUIRED_MODULES.items():
        try:
            found = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append((module_name, pip_name))

    if missing:
        lines = [
            "WifiVouchers is missing required Python modules:",
            "",
        ]
        for module_name, pip_name in missing:
            if module_name == "cups":
                lines.append(
                    f"  - {module_name} (optional; enables CUPS status checks)"
                )
            else:
                lines.append(f"  - {module_name} (required)")
        lines.append("")
        lines.append("Install them with:")
        lines.append(
            "    pip install " + " ".join(pip_name for _, pip_name in missing)
        )
        lines.append("")
        lines.append(
            "Note: tkinter is provided by your distribution's python3-tk "
            "package (e.g. 'sudo apt install python3-tk') rather than pip "
            "if the pip command above does not install it."
        )
        lines.append("")
        lines.append("The application will now exit.")
        message = "\n".join(lines)
        print(message, file=sys.stderr)
        # Try to also show a dialog when a display is available so GUI users
        # see the message; otherwise the terminal output is the only notice.
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("WifiVouchers", message, parent=root)
            root.destroy()
        except Exception:
            pass  # No display available; terminal message above is sufficient.
        sys.exit(1)


if IS_WINDOWS:
    from wifivouchers.platform_csv_windows import read_csv_roll  # noqa: F401
    from wifivouchers.platform_printer_windows import (  # noqa: F401
        check_printer_online,
        list_available_printers,
        print_printer_bytes,
    )
    from wifivouchers.platform_ui_windows import (  # noqa: F401
        acquire_single_instance,
        edit_ini_file,
        release_single_instance,
        show_message,
    )
else:
    from wifivouchers.platform_csv_linux import read_csv_roll  # noqa: F401
    from wifivouchers.platform_printer_linux import (  # noqa: F401
        check_printer_online,
        list_available_printers,
        print_printer_bytes,
    )
    from wifivouchers.platform_ui_linux import (  # noqa: F401
        acquire_single_instance,
        edit_ini_file,
        release_single_instance,
        show_message,
    )
