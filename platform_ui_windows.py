"""Windows-native UI helpers: message boxes, config opening, single instance."""

import ctypes
from pathlib import Path
import sys

INSTANCE_MUTEX_NAME = "Local\\WifiVoucherCodes_SingleInstance"
ERROR_ALREADY_EXISTS = 183
_instance_mutex_handle = None
_instance_kernel32 = None


def show_message(title, text, style=0):
    """Display a Windows-native message box used for status and error feedback."""
    return ctypes.windll.user32.MessageBoxW(0, text, title, style)


def edit_ini_file(config_path):
    """Open the configuration in the user's Windows default editor."""
    import os

    os.startfile(str(Path(config_path)))


def acquire_single_instance():
    """A named Windows mutex prevents two copies from consuming the same roll."""
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


def release_single_instance():
    """Release the mutex so a later launch can become the active instance."""
    global _instance_kernel32, _instance_mutex_handle

    if _instance_mutex_handle is not None:
        try:
            _instance_kernel32.CloseHandle(_instance_mutex_handle)
        finally:
            _instance_mutex_handle = None
            _instance_kernel32 = None
