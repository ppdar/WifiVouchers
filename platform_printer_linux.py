"""Linux printer backend using CUPS (pycups when available, lp/lpstat otherwise)."""

import logging
import shutil
import subprocess

import wifivouchers.platform_ui_linux as _ui

logger = logging.getLogger(__name__)


def _show_message(title, text, style=0):
    return _ui.show_message(title, text, style)


def _run_lp(command, **kwargs):
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        **kwargs,
    )


def list_available_printers():
    """Return the printer names registered with CUPS."""
    try:
        import cups

        connection = cups.Connection()
        return sorted(connection.getPrinters(), key=str.casefold)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("pycups printer enumeration failed, trying lpstat: %s", exc)

    lpstat = shutil.which("lpstat")
    if lpstat is None:
        raise RuntimeError(
            "CUPS printing tools were not found. Install CUPS and the "
            "'cups' Python package (pip install pycups), or ensure "
            "lpstat is on the PATH."
        )
    result = _run_lp([lpstat, "-e"])
    if result.returncode != 0:
        raise RuntimeError(
            f"lpstat failed with exit code {result.returncode}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return sorted(
        {line.split()[0] for line in result.stdout.decode().splitlines() if line.strip()},
        key=str.casefold,
    )


def check_printer_online(printer_name):
    """Match the configured CUPS printer and distinguish offline from missing."""
    target_name = (printer_name or "").strip()
    try:
        import cups

        connection = cups.Connection()
        printers = connection.getPrinters()
        if target_name not in printers:
            return "not_found"
        # CUPS reports device-state via IPP; a printer absent from
        # accepting jobs or marked rejecting is treated as offline.
        printer = printers[target_name]
        if printer.get("printer-is-accepting-jobs", True) is False:
            return "offline"
        state = printer.get("printer-state", 3)
        return "offline" if state == 5 else "online"  # 5 = stopped
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("pycups status check failed, trying lpstat: %s", exc)

    lpstat = shutil.which("lpstat")
    if lpstat is None:
        raise RuntimeError(
            "CUPS printing tools were not found. Install CUPS and the "
            "'cups' Python package (pip install pycups), or ensure "
            "lpstat is on the PATH."
        )
    names_result = _run_lp([lpstat, "-e"])
    if names_result.returncode != 0:
        raise RuntimeError(
            f"lpstat failed with exit code {names_result.returncode}: "
            f"{names_result.stderr.decode(errors='replace').strip()}"
        )
    names = {
        line.split()[0] for line in names_result.stdout.decode().splitlines() if line.strip()
    }
    if target_name not in names:
        return "not_found"
    status_result = _run_lp([lpstat, "-p", target_name])
    if status_result.returncode != 0:
        return "offline"
    status_text = status_result.stdout.decode(errors="replace")
    return "offline" if "disabled" in status_text else "online"


def print_printer_bytes(printer_name, data):
    """Send raw receipt bytes to a CUPS printer as a RAW job."""
    lp = shutil.which("lp")
    if lp is None:
        logger.error("The 'lp' command was not found; is CUPS installed?")
        _show_message(
            "Printer Error",
            "The 'lp' command was not found. Install CUPS (cups package) "
            "so vouchers can be printed.",
            0,
        )
        return False

    result = _run_lp(
        [lp, "-d", printer_name, "-o", "raw", "-"],
        input=data,
    )
    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="replace").strip()
        logger.error(
            "Print job failed on printer %s: %s", printer_name, stderr_text
        )
        _show_message(
            "Print Failed",
            f"Unable to print voucher on '{printer_name}':\n{stderr_text}\n\n"
            "The voucher was not removed.",
            0,
        )
        return False

    request_id = result.stdout.decode(errors="replace").strip()
    logger.info(
        "Print job submitted to printer: %s (request: %s)",
        printer_name,
        request_id,
    )
    return True
