"""Prevent concurrent Windows runs using the same configuration file."""

import ctypes
import hashlib
import os
import sys


class AlreadyRunningError(RuntimeError):
    pass


def config_identity(config_path: str) -> str:
    path = os.path.normcase(os.path.realpath(config_path))
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]


class InstanceLock:
    """Keep a named mutex handle open; Windows releases it on process exit."""

    def __init__(self, config_path: str):
        self.name = "Local\\ObsidianSync-" + config_identity(config_path)
        self._handle = None
        self._kernel32 = None

    def __enter__(self):
        if sys.platform != "win32":
            return self
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            raise AlreadyRunningError("Sync is already open for this configuration.")
        self._kernel32 = kernel32
        self._handle = handle
        return self

    def __exit__(self, *exc):
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
