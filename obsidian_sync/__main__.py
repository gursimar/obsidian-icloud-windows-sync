import asyncio
import argparse
import os
import sys

from .config import SyncConfig
from .logger import SyncLogger
from .hasher import FileHasher
from .disk_io import DiskIO
from .duplicates import DuplicateScanner
from .sync_engine import SyncEngine
from .instance import AlreadyRunningError, InstanceLock


def create_engine(config, *, cleanup=True):
    logger = SyncLogger(config)
    hasher = FileHasher(config, logger)
    disk_io = DiskIO(config, logger)
    duplicates = DuplicateScanner(config, logger, disk_io)
    if cleanup:
        duplicates.scan_and_clean()
    return SyncEngine(config, logger, hasher, disk_io, duplicates)

def main():
    """
    Entry point for the Obsidian iCloud Windows Sync.
    """
    parser = argparse.ArgumentParser(description="Obsidian iCloud Windows Sync")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML file (default: config.yaml)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tray", action="store_true", help="Run in the Windows system tray (requires the tray extra)")
    mode.add_argument("--install-autostart", action="store_true", help="Start tray mode at logon for the current Windows user")
    mode.add_argument("--remove-autostart", action="store_true", help="Remove this configuration's tray autostart entry")
    args = parser.parse_args()

    # pythonw has no console streams; sync diagnostics still go to its log files.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    if args.remove_autostart:
        from .startup import configure_startup
        try:
            configure_startup(args.config, remove=True)
        except (OSError, RuntimeError) as error:
            parser.error(str(error))
        print("Tray autostart removed for this configuration.")
        return

    try:
        config = SyncConfig.from_yaml(args.config)
    except Exception as e:
        print(f"Unexpected error while loading config from {args.config}: {e}", file=sys.stderr)
        sys.exit(1)

    if args.tray or args.install_autostart:
        from .tray import load_tray_dependencies
        try:
            load_tray_dependencies()
            config_dir = os.path.dirname(os.path.realpath(args.config))
            for field in ("local_vault", "icloud_vault", "history_dir", "logs_dir"):
                path = getattr(config, field)
                if path and not os.path.isabs(path):
                    setattr(config, field, os.path.abspath(os.path.join(config_dir, path)))
            issues = config.validate()
            critical = [message for _, level, message in issues if level == "critical"]
            if critical:
                raise ValueError("; ".join(critical))
            if not config.run_continuously:
                raise ValueError("Tray mode requires run_continuously: true.")
        except (OSError, RuntimeError, ValueError) as error:
            parser.error(str(error))

    if args.install_autostart:
        from .startup import configure_startup
        try:
            configure_startup(args.config)
        except (OSError, RuntimeError) as error:
            parser.error(str(error))
        print("Tray autostart enabled for the current Windows user.")
        return

    try:
        with InstanceLock(args.config):
            if args.tray:
                from .tray import run_tray
                # A hidden process cannot answer the destructive cleanup prompt.
                run_tray(lambda: create_engine(config, cleanup=False), config.logs_dir)
            else:
                asyncio.run(create_engine(config).run())
    except AlreadyRunningError as error:
        print(str(error))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
