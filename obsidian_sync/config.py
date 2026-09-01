import os
import fnmatch
from dataclasses import dataclass, field

import yaml

DEFAULT_IGNORED_DIRS = {'.trash', '.fseventsd', '.spotlight-v100', '.apdisk'}
DEFAULT_IGNORED_FILES = {'.ds_store', '.trash', 'workspace.json', 'workspace-mobile.json'}

@dataclass
class SyncConfig:
    """
    Holds all paths, sync timings, logging preferences, and ignore patterns required to govern the three-way synchronization process between local, iCloud, and history directories.
    """
    # Paths
    local_vault: str = ""
    icloud_vault: str = ""
    history_dir: str = ""
    logs_dir: str = ""
    # Sync
    run_continuously: bool = True
    check_icloud_status: bool = True
    poll_interval: int = 2
    stability_window: int = 3
    stabilize_wait: int = 8
    # Quiet-period debounce: a file must go this many seconds without changing
    # (in either vault) before it is synced. Editors autosave every ~2s while
    # typing; pushing each save to iCloud makes iCloud fork conflict copies.
    quiet_period: int = 20
    # Upper bound on how long to keep waiting for quiet before syncing anyway.
    quiet_max_wait: int = 600
    tiny_threshold: int = 8
    max_concurrent_io: int = 50
    # Logging
    console_level: str = "normal"
    shorter_paths: bool = True
    max_display_length: int = 50
    log_retention: int = 10
    # Ignore
    ignore_patterns: list[str] = field(default_factory=list)
    ignored_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORED_DIRS))
    ignored_files: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORED_FILES))

    @classmethod
    def from_yaml(cls, path: str) -> "SyncConfig":
        """
        Creates a SyncConfig instance from a YAML configuration file.

        Args:
            path (str): The file path to the YAML configuration file.
        Returns:
            SyncConfig: An instance populated with values from the YAML file. Missing sections or values will fall back to their defaults.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Config file not found: {path}") from e
        except PermissionError as e:
            raise PermissionError(f"Config file not accessible: {path}") from e
        except yaml.YAMLError as e:
            raise ValueError(f"Config file has invalid YAML: {path}") from e
        except Exception as e:
            raise OSError(f"Error reading config file: {path}") from e

        paths = data.get("paths", {})
        sync = data.get("sync", {})
        logging_cfg = data.get("logging", {})
        ignore = data.get("ignore", {})

        return cls(
            local_vault=paths.get("local_vault", ""),
            icloud_vault=paths.get("icloud_vault", ""),
            history_dir=paths.get("history_dir", ""),
            logs_dir=paths.get("logs_dir", ""),
            run_continuously=sync.get("run_continuously", True),
            check_icloud_status=sync.get("check_icloud_status", True),
            poll_interval=sync.get("poll_interval", 2),
            stability_window=sync.get("stability_window", 3),
            stabilize_wait=sync.get("stabilize_wait", 8),
            quiet_period=sync.get("quiet_period", 20),
            quiet_max_wait=sync.get("quiet_max_wait", 600),
            tiny_threshold=sync.get("tiny_threshold", 8),
            max_concurrent_io=sync.get("max_concurrent_io", 50),
            console_level=logging_cfg.get("console_level", "normal"),
            shorter_paths=logging_cfg.get("shorter_paths", True),
            max_display_length=logging_cfg.get("max_display_length", 50),
            log_retention=logging_cfg.get("log_retention", 10),
            ignore_patterns=ignore.get("patterns", []),
            ignored_dirs=set(ignore.get("dirs", DEFAULT_IGNORED_DIRS)),
            ignored_files=set(ignore.get("files", DEFAULT_IGNORED_FILES)),
        )

    @property
    def state_file_path(self) -> str:
        """
        Gets the absolute file path to the JSON state cache file within the logs directory.

        Returns:
            str: The absolute file path to the JSON state cache file within the logs directory.
        """
        return os.path.join(self.logs_dir, "sync_state.json")

    def validate(self) -> list[tuple[str, str, str]]:
        """
        Validates the configuration paths and settings for logical correctness. Checks for unset paths, missing directories, overlapping vault paths, and improperly nested history directories.

        Returns:
            list[tuple[str, str, str]]: A list of identified configuration issues. Each tuple contains (error_code, severity_level, error_message), where severity_level is either 'critical' or 'warn'.
        """
        errors: list[tuple[str, str, str]] = []

        for name, path in [("local_vault", self.local_vault), ("icloud_vault", self.icloud_vault)]:
            if not path:
                errors.append(("path_not_set", "critical", f"{name} is not set"))
            elif not os.path.exists(path):
                errors.append(("path_does_not_exist", "critical", f"{name} does not exist: {path}"))

        for name, path in [("history_dir", self.history_dir), ("logs_dir", self.logs_dir)]:
            if not path:
                errors.append(("path_not_set", "critical", f"{name} is not set"))
            elif not os.path.exists(path):
                errors.append(("dir_missing", "warn", f"{name} did not exist, trying to create: {path}"))

        if self.local_vault and self.icloud_vault:
            if os.path.normcase(self.local_vault) == os.path.normcase(self.icloud_vault):
                errors.append(("same_paths", "critical", "local_vault and icloud_vault cannot be the same path"))

        if self.history_dir:
            norm_history = os.path.normcase(self.history_dir)
            for name, guarded in [("local_vault", self.local_vault), ("icloud_vault", self.icloud_vault)]:
                if guarded:
                    norm_guarded = os.path.normcase(guarded)
                    if norm_history == norm_guarded or norm_history.startswith(norm_guarded + os.sep):
                        errors.append(("history_dir_inside_vault", "critical", f"history_dir cannot be inside or equal to {name}"))

        for p in self.ignore_patterns:
            if '//' in p or p.startswith('/'):
                errors.append(("suspicious_ignore_pattern", "warn", f"Suspicious ignore pattern: '{p}'"))

        if self.poll_interval <= 0:
            errors.append(("invalid_value", "critical", "poll_interval must be positive"))

        if self.stability_window < 0:
            errors.append(("invalid_value", "critical", "stability_window cannot be negative"))

        if self.quiet_period < 0:
            errors.append(("invalid_value", "critical", "quiet_period cannot be negative"))

        if self.quiet_max_wait < self.quiet_period:
            errors.append(("invalid_value", "critical", "quiet_max_wait must be >= quiet_period"))

        return errors

    def is_ignored(self, rel_path: str) -> bool:
        """
        Determines if a given relative file path matches any active ignore patterns.

        Args:
            rel_path (str): The relative file or directory path to check.
        Returns:
            bool: True if the path should be ignored, False otherwise.
        """
        rel = rel_path.replace('\\', '/').replace(os.sep, '/').lower()
        for pattern in self.ignore_patterns:
            pat = pattern.replace('\\', '/').replace(os.sep, '/').lower()
            if rel == pat:
                return True
            if fnmatch.fnmatch(rel, pat):
                return True
            if rel.startswith(pat.rstrip('/') + '/'):
                return True
        return False

    def disp(self, path: str) -> str:
        """
        Formats a file path into a concise representation for logging outputs. Truncates paths exceeding `max_display_length` by replacing middle segments with an ellipsis (...).

        Args:
            path (str): The absolute or relative file path to format.
        Returns:
            str: The shortened path string ready for display.
        """
        if not self.shorter_paths:
            return path
        if os.path.isabs(path):
            matched = False
            for root in [self.local_vault, self.icloud_vault, self.history_dir]:
                if root and (path == root or path.startswith(root + os.sep)):
                    path = os.path.relpath(path, root)
                    matched = True
                    break
            if not matched:
                return os.path.basename(path)
        if len(path) <= self.max_display_length:
            return path
        parts = path.split(os.sep)
        if len(parts) > 2:
            return f"{parts[0]}{os.sep}...{os.sep}{parts[-1]}"
        return f"...{os.sep}{os.path.basename(path)}"

    def min_seed_size(self, rel_path: str) -> int:
        """
        Calculates the minimum file size required to seed a file into history. Obsidian internal files (inside '.obsidian') are allowed to be smaller (1 byte) to preserve essential settings, while regular files default to the configured `tiny_threshold`.

        Args:
            rel_path (str): The relative path of the file being evaluated.
        Returns:
            int: The minimum required file size in bytes.
        """
        normalized_rel_path = os.path.normpath(rel_path)
        rel_parts = [part.lower() for part in normalized_rel_path.split(os.sep)]
        return 1 if '.obsidian' in rel_parts else self.tiny_threshold
