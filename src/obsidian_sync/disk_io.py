import os
import asyncio
import shutil
import ctypes
import platform
import uuid
from ctypes import wintypes
from datetime import datetime

# ── Windows API ──────────────────────────────────────────────────
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8
FILE_ATTRIBUTE_NORMAL = 0x80

if platform.system() in ("Windows", "Microsoft"):
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    MoveFileExW = kernel32.MoveFileExW
    MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    MoveFileExW.restype = wintypes.BOOL
    SetFileAttributesW = kernel32.SetFileAttributesW
    SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    SetFileAttributesW.restype = wintypes.BOOL
else:
    MoveFileExW = None
    SetFileAttributesW = None

# ── Utility functions (used across modules) ──────────────────────

def ensure_dir(path: str) -> None:
    """
    Ensures that a directory and all its parent directories exist. Creates them if they are missing.

    Args:
        path (str): The directory path to ensure.
    """
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def safe_exists(path: str) -> bool:
    """
    Safely checks if a file or directory exists.

    Args:
        path (str): The path to check.
    Returns:
        bool: True if the path exists and is accessible, False otherwise.
    """
    try:
        return os.path.exists(path)
    except Exception:
        return False


def size_or_zero(path: str) -> int:
    """
    Safely retrieves the size of a file.

    Args:
        path (str): The file path.
    Returns:
        int: The size of the file in bytes, or 0 if the file cannot be read.
    """
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def safe_mtime(path: str) -> float:
    """
    Safely retrieves the last modification time of a file.

    Args:
        path (str): The file path.
    Returns:
        float: The modification timestamp, or 0 if the file cannot be read.
    """
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0


# ── Disk I/O class ───────────────────────────────────────────────

class DiskIO:
    """
    Handles robust and safe file system operations, atomic copying, deletion, and directory cleanup. It includes retry mechanisms and Windows-specific API fallbacks to bypass strict file locks often caused by background iCloud syncing.
    """
    def __init__(self, config, logger):
        """
        Initializes the DiskIO handler.

        Args:
            config (SyncConfig): The application configuration instance.
            logger (SyncLogger): The logger instance for outputting I/O events.
        """
        if platform.system() not in ("Windows", "Microsoft"):
            raise RuntimeError("Disk operations are only supported on Windows.")
        self.config = config
        self.log = logger
        self._dst_locks: dict[str, asyncio.Lock] = {}

    def _lock_key(self, path):
        return os.path.normcase(os.path.abspath(path))

    def _get_dst_lock(self, dst):
        key = self._lock_key(dst)
        lock = self._dst_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._dst_locks[key] = lock
        return lock

    def set_normal_attributes(self, path: str) -> bool:
        """
        Deletes read-only or hidden attributes from a file on Windows before attempting to forcefully replace or delete files that might have been locked or protected by external processes.

        Args:
            path (str): The file path to modify.
        Returns:
            bool: True if the operation succeeded or file doesn't exist, False on failure.
        """
        try:
            if not os.path.exists(path):
                return True
            return bool(SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL))
        except Exception:
            return False

    async def async_copy(self, src: str, dst: str, max_retries: int=12, initial_backoff: float=0.25):
        """
        The method copies the source to a `.tmp` file first, then replaces the target destination. If a PermissionError occurs (e.g., file lock), it retries with exponential backoff. If standard retries fail, it falls back to MoveFileExW and brute-force deletion.

        Args:
            src (str): Source file path.
            dst (str): Target destination file path.
            max_retries (int, optional): Maximum number of retry attempts. Defaults to 12.
            initial_backoff (float, optional): Initial wait time in seconds before retrying. Defaults to 0.25.
        """
        self.log.info("COPYING", f"{self.config.disp(src)} → {self.config.disp(dst)}", level="verbose")
        async with self._get_dst_lock(dst):
            ensure_dir(os.path.dirname(dst))
            tmp = f"{dst}.tmp.{uuid.uuid4().hex}"
            try:
                await asyncio.to_thread(shutil.copy2, src, tmp)
            except Exception as e:
                self.log.error("FAILED", f"Write tmp file {self.config.disp(tmp)}: {e}")
                raise

            backoff = initial_backoff
            attempt = 0
            while True:
                try:
                    if os.path.exists(dst):
                        self.set_normal_attributes(dst)
                    os.replace(tmp, dst)
                    self.log.success("SUCCESS", f"Updated: {self.config.disp(dst)}", level="verbose")
                    return
                except FileNotFoundError:
                    # Tmp can disappear if external software (e.g. iCloud) races with us.
                    attempt += 1
                    if attempt >= max_retries:
                        self.log.error("FAILED", f"Temp file vanished repeatedly for {self.config.disp(dst)}")
                        raise
                    await asyncio.to_thread(shutil.copy2, src, tmp)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.8, 5.0)
                except PermissionError:
                    attempt += 1
                    if attempt >= max_retries:
                        # Win32 MoveFileEx fallback
                        try:
                            ok = MoveFileExW(tmp, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)
                            if ok:
                                self.log.success("SUCCESS", f"MoveFileEx: {self.config.disp(dst)}", level="verbose")
                                return
                            self.log.error("FAILED", f"MoveFileEx (err {ctypes.get_last_error()})")
                        except Exception as exc:
                            self.log.error("DANGER", f"MoveFileEx exception: {exc}")
                        # Final brute-force attempt
                        try:
                            if os.path.exists(dst):
                                os.remove(dst)
                            os.replace(tmp, dst)
                            self.log.success("SUCCESS", f"Forced replace: {self.config.disp(dst)}", level="verbose")
                            return
                        except Exception as exc:
                            self.log.error("FAILED", f"Final forced replace failed: {exc}")
                            try:
                                if os.path.exists(tmp):
                                    os.remove(tmp)
                            except Exception:
                                pass
                            raise PermissionError(f"Unable to replace {dst}") from exc

                    self.set_normal_attributes(dst)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.8, 5.0)
                except Exception as unexpected:
                    self.log.error("DANGER", f"Unexpected error during replace: {unexpected}")
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception:
                        pass
                    raise

    async def remove_file(self, path, description):
        """Async remove a file and clean up empty parent dirs."""
        try:
            if not os.path.exists(path):
                return
            try:
                await asyncio.to_thread(os.remove, path)
            except FileNotFoundError:
                return
            self.log.success("SUCCESS", f"Removed {description}: {self.config.disp(path)}", level="verbose")
            # Walk up and remove empty directories
            roots = {self.config.local_vault, self.config.icloud_vault, self.config.history_dir}
            dir_path = os.path.dirname(path)
            while dir_path and dir_path not in roots:
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    try:
                        await asyncio.to_thread(os.rmdir, dir_path)
                        self.log.info("INFO", f"Removed empty dir: {self.config.disp(dir_path)}", level="verbose")
                        dir_path = os.path.dirname(dir_path)
                    except OSError:
                        break
                else:
                    break
        except Exception as e:
            self.log.error("FAILED", f"Remove {description} {path}: {e}")

    def remove_file_sync(self, path: str, description: str):
        """
        Synchronously removes a file (e.g., duplicate cleanup) before the main asyncio event loop begins.

        Args:
            path (str): The file path to delete.
            description (str): A descriptive label for the log output.
        """
        try:
            if os.path.exists(path):
                os.remove(path)
                self.log.success("SUCCESS", f"Removed {description}: {self.config.disp(path)}", level="verbose")
        except Exception as e:
            self.log.error("FAILED", f"Remove {description} {path}: {e}")

    async def create_conflict_duplicate(self, path: str):
        """
        Creates a backup duplicate of a file to prevent data loss during conflicts. Appends a timestamp suffix to the original filename (e.g., `file_CONFLICT_20260405_123045_123456.md`).

        Args:
            path (str): The original file path that is in conflict.
        """
        base, ext = os.path.splitext(path)
        conflict = f"{base}_CONFLICT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
        self.log.warn("WARNING", f"Creating conflict duplicate: {self.config.disp(conflict)}", level="verbose")
        try:
            await asyncio.to_thread(shutil.copy2, path, conflict)
        except Exception as e:
            self.log.error("DANGER", f"Failed to create conflict duplicate: {e}")
