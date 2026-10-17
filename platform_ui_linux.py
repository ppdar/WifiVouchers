"""Linux UI helpers: dialogs, config opening, single-instance file lock."""

import fcntl
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

SINGLE_INSTANCE_LOCK_PATH = Path.home() / ".wifivouchers.lock"
_lock_file = None


def show_message(title, text, style=0):
    """Display a Tk message box (styles 0=info, 1=OK/Cancel question)."""
    parent = tk._default_root
    if style == 1:
        result = messagebox.askokcancel(title, text, parent=parent)
        return 1 if result else 0
    messagebox.showinfo(title, text, parent=parent)
    return 0


def edit_ini_file(config_path):
    """Open the configuration in the user's default Linux editor."""
    config_path = Path(config_path).resolve()
    try:
        mime_result = subprocess.run(
            ["xdg-mime", "query", "filetype", str(config_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        messagebox.showerror(
            "Configuration Error",
            "Unable to check the system's default application for .ini files. "
            "Please install xdg-utils and set a default application before "
            f"opening the configuration.\n\n{exc}",
            parent=tk._default_root,
        )
        return

    if mime_result.returncode != 0 or not mime_result.stdout.strip():
        messagebox.showerror(
            "Configuration Error",
            "Unable to determine the file type for the configuration file:\n"
            f"{config_path}",
            parent=tk._default_root,
        )
        return

    mime_type = mime_result.stdout.strip()
    try:
        default_result = subprocess.run(
            ["xdg-mime", "query", "default", mime_type],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        messagebox.showerror(
            "Configuration Error",
            "Unable to check the system's default application for .ini files. "
            "Please set a default application before opening the configuration.\n\n"
            f"{exc}",
            parent=tk._default_root,
        )
        return

    if default_result.returncode != 0:
        messagebox.showerror(
            "Configuration Error",
            "Unable to determine the system's default application for "
            f"{mime_type} files.",
            parent=tk._default_root,
        )
        return

    if not default_result.stdout.strip():
        messagebox.showinfo(
            "Default Application Required",
            "No default application is set for .ini files. Please set a "
            "default application in your system settings, then close this "
            "dialog and try again.",
            parent=tk._default_root,
        )
        return

    try:
        subprocess.Popen(["xdg-open", str(config_path)])
    except OSError as exc:
        messagebox.showerror(
            "Configuration Error",
            f"Unable to open the configuration file:\n{config_path}\n\n{exc}",
            parent=tk._default_root,
        )


def acquire_single_instance():
    """An advisory file lock prevents two copies from consuming the same roll."""
    global _lock_file

    lock_path = SINGLE_INSTANCE_LOCK_PATH
    try:
        _lock_file = lock_path.open("w")
        try:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _lock_file.close()
            _lock_file = None
            return False
    except OSError as exc:
        if _lock_file is not None:
            _lock_file.close()
            _lock_file = None
        raise OSError(
            f"Unable to create the single-instance lock file {lock_path}: {exc}"
        ) from exc
    return True


def release_single_instance():
    """Release the advisory lock so a later launch can become the active instance."""
    global _lock_file

    if _lock_file is not None:
        try:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            _lock_file.close()
            _lock_file = None
