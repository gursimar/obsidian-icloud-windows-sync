"""Opt-in tray autostart for the current Windows user."""

import os
from pathlib import Path
import subprocess
import sys

from .instance import config_identity

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_name(config_path: str) -> str:
    return "ObsidianSync-" + config_identity(config_path)


def startup_command(config_path: str) -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("pythonw.exe was not found next to Python; use a Windows Python installation.")
    return subprocess.list2cmdline([
        str(pythonw), "-X", "utf8", "-m", "obsidian_sync", "--tray",
        "--config", os.path.realpath(config_path),
    ])


def configure_startup(config_path: str, *, remove: bool = False) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Tray autostart is only available on Windows.")
    import winreg

    name = startup_name(config_path)
    if remove:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
    else:
        command = startup_command(config_path)
        # Run entries are limited to 260 characters by Windows.
        if len(command) > 260:
            raise RuntimeError("Autostart command exceeds 260 characters; use shorter installation/config paths.")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
