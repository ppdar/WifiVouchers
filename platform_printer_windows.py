"""Windows printer backend using pywin32 and WMI."""

import logging

import wifivouchers.platform_ui_windows as _ui
import wmi  # noqa: F401  (module-level so tests can patch it)
import win32print  # noqa: F401  (module-level so tests can patch it)

logger = logging.getLogger(__name__)


def _show_message(title, text, style=0):
    # Delegate through wifivouchers.show_message so tests patching that name
    # observe the dialog calls; fall back to the native UI when wifivouchers
    # is not fully initialised.
    try:
        import wifivouchers

        return wifivouchers.show_message(title, text, style)
    except Exception:
        return _ui.show_message(title, text, style)


def print_printer_bytes(printer_name, data):
    """Send raw receipt bytes to Windows printing services."""
    try:
        h_printer = win32print.OpenPrinter(printer_name)
    except Exception as exc:
        logger.exception("Unable to open printer: %s", printer_name)
        _show_message(
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
            _show_message(
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
        _show_message(
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
            _show_message(
                "Printer Cleanup Warning",
                f"The print operation finished, but the printer handle could not be closed:\n{exc}",
                0,
            )
    return True


def check_printer_online(printer_name):
    """Match the configured printer and distinguish offline from missing."""
    target_name = (printer_name or "").strip().lower()
    printers = wmi.WMI().Win32_Printer()
    for printer in printers:
        caption = getattr(printer, "caption", "") or ""
        if caption.strip().lower() == target_name:
            return "offline" if printer.WorkOffline else "online"
    return "not_found"


def list_available_printers():
    """Return the local and connected printers registered with Windows."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    printer_info = win32print.EnumPrinters(flags)
    return sorted(
        {printer[2].strip() for printer in printer_info if printer[2].strip()},
        key=str.casefold,
    )
