import os
import time
import asyncio
import traceback

from colorama import Fore

from .logger import colored
from .disk_io import safe_exists, size_or_zero, safe_mtime, ensure_dir
from .action_log import SyncActionLog

class SyncEngine:
    """
    Manages the main asynchronous event loop, evaluates file states between the local vault, iCloud vault, and history directory, and applies the appropriate actions (push, pull, delete, resolve conflict) while respecting cooldowns and concurrency limits.
    """
    def __init__(self, config, logger, hasher, disk_io, duplicates):
        """
        Initializes the SyncEngine.

        Args:
            config (SyncConfig): The application configuration.
            logger (SyncLogger): The logger instance.
            hasher (FileHasher): The file hashing and state caching service.
            disk_io (DiskIO): The disk I/O handler for safe file operations.
            duplicates (DuplicateScanner): The scanner used for pre-flight cleanup.
        """
        self.config = config
        self.log = logger
        self.hasher = hasher
        self.io = disk_io
        self.duplicates = duplicates
        self.action_log = SyncActionLog(
            os.path.join(config.logs_dir, 'sync_actions.json'))
        self.cooldowns: dict[str, float] = {}
        self.active_tasks: set[str] = set()
        self.io_semaphore = asyncio.Semaphore(self.config.max_concurrent_io)

    # ── Core file operations ─────────────────────────────────────

    async def push_to_icloud(self, rel: str):
        """
        Asynchronously pushes a file from the local vault to iCloud and history.

        Args:
            rel (str): The relative file path to push.
        """
        local = os.path.join(self.config.local_vault, rel)
        icloud = os.path.join(self.config.icloud_vault, rel)
        history = os.path.join(self.config.history_dir, rel)
        await self.io.async_copy(local, icloud)
        await self.io.async_copy(local, history)
        size = size_or_zero(local)
        cd = self.config.big_file_cooldown if size > self.config.big_file_threshold else self.config.cooldown_seconds
        self.cooldowns[rel] = time.time() + cd

    async def restore_from_icloud(self, rel: str):
        """
        Asynchronously pulls a file from the iCloud vault to local and history.

        Args:
            rel (str): The relative file path to pull.
        """
        local = os.path.join(self.config.local_vault, rel)
        icloud = os.path.join(self.config.icloud_vault, rel)
        history = os.path.join(self.config.history_dir, rel)
        await self.io.async_copy(icloud, local)
        await self.io.async_copy(icloud, history)
        size = size_or_zero(icloud)
        cd = self.config.big_file_cooldown if size > self.config.big_file_threshold else self.config.cooldown_seconds
        self.cooldowns[rel] = time.time() + cd

    # ── Path gathering ───────────────────────────────────────────

    def gather_rel_paths(self):
        """
        Scans all vault roots to collect a unified set of relevant relative paths. Filters out ignored. This method involves heavy blocking I/O and should be run in a background thread.

        Returns:
            set[str]: A set of all relative file paths discovered across Local, iCloud, and History directories.
        """
        rels: dict[str, str] = {}
        cfg = self.config

        def collect(current_path: str, base_root: str):
            """
            Recursively scans a directory tree to collect valid file paths. Valid files are converted to their normalized relative paths (relative to the `base_root`) and added to the outer `rels` set.

            Args:
                current_path (str): The current absolute path being scanned in the recursion.
                base_root (str): The absolute path of the vault's root directory, used to calculate the relative path of discovered files.
            """
            try:
                with os.scandir(current_path) as it:
                    for entry in it:
                        name_lower = entry.name.lower()
                        if entry.is_dir():
                            if name_lower in cfg.ignored_dirs:
                                continue
                            collect(entry.path, base_root)
                        elif entry.is_file():
                            if (name_lower.endswith('.tmp')
                                    or name_lower.startswith('._')
                                    or name_lower in cfg.ignored_files
                                    or 'page-preview' in name_lower):
                                continue
                            rel = os.path.normpath(os.path.relpath(entry.path, base_root))
                            if cfg.is_ignored(rel):
                                continue
                            # Windows/iCloud can surface case-variant duplicates.
                            # Normalize to one key so we don't sync the same logical path twice.
                            key = os.path.normcase(rel)
                            if key in rels and rels[key] != rel:
                                self.log.warn("CASE DUP",
                                              f"Merging case-variant paths: {cfg.disp(rels[key])} / {cfg.disp(rel)}",
                                              level="verbose")
                            rels[key] = rel
            except FileNotFoundError:
                pass

        collect(cfg.local_vault, cfg.local_vault)
        collect(cfg.icloud_vault, cfg.icloud_vault)
        collect(cfg.history_dir, cfg.history_dir)
        return set(rels.values())

    # ── Per-file sync logic ──────────────────────────────────────

    async def recheck(self, local: str, icloud: str, history: str, rel_path: str) -> tuple[str | None, str | None, str | None]:
        """
        Waits for the stability window and retrieves fresh hashes for all locations.

        Args:
            local (str): Absolute path to the local file.
            icloud (str): Absolute path to the iCloud file.
            history (str): Absolute path to the history file.
            rel_path (str): Relative path of the file (used for caching).
        Returns:
            tuple[str | None, str | None, str | None]: The forced SHA-256 hashes for Local, iCloud, and History respectively.
        """
        await asyncio.sleep(self.config.stability_window)
        Lh = await self.hasher.get_cached_hash(local, 'L', rel_path, force=True) if safe_exists(local) else None
        Ch = await self.hasher.get_cached_hash(icloud, 'C', rel_path, force=True) if safe_exists(icloud) else None
        Hh = await self.hasher.get_cached_hash(history, 'H', rel_path, force=True) if safe_exists(history) else None
        return Lh, Ch, Hh

    async def sync_file(self, rel_path: str):
        """
        Executes the core three-way sync logic for a single file path. Evaluates the existence and hashes across Local, iCloud, and History to determine the correct action (Push, Pull, Delete, Seed, or Conflict Resolution). Bypasses files currently in their cooldown period.

        Args:
            rel_path (str): The relative file path to synchronize.
        """
        cfg = self.config
        now = time.time()
        if rel_path in self.cooldowns and self.cooldowns[rel_path] > now:
            return

        local = os.path.join(cfg.local_vault, rel_path)
        icloud = os.path.join(cfg.icloud_vault, rel_path)
        history = os.path.join(cfg.history_dir, rel_path)
        d = cfg.disp(rel_path)

        L_exists = safe_exists(local)
        C_exists = safe_exists(icloud)
        H_exists = safe_exists(history)

        # ── Nothing exists anywhere ──
        if not L_exists and not C_exists:
            if H_exists:
                self.log.warn("REMOVING HISTORY", f"{colored('No local', Fore.RED)} & {colored('No iCloud', Fore.RED)} for {d}", level="important")
                await self.io.remove_file(history, "history")
                self.action_log.record(rel_path, 'ORPHANED', 'L and C missing',
                                       'Removed orphaned history', None)
            # Clean stale state
            if rel_path in self.hasher.state:
                del self.hasher.state[rel_path]
                self.hasher.dirty = True
            return

        # ── Local missing, C+H exist ──
        if not L_exists and C_exists and H_exists:
            self.log.warn("DELETE", f"{colored('Local missing', Fore.RED)}, stabilizing for {d}", level="verbose")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Ch is not None and Hh is not None and Ch == Hh:
                self.log.custom([" ←", "⚫"], [Fore.RED, Fore.RED], "DELETE", f"{colored('Removing from iCloud', Fore.CYAN)} & history for {d}", rel_path, level="verbose")
                await self.io.remove_file(icloud, "iCloud")
                await self.io.remove_file(history, "history")
                self.action_log.record(rel_path, 'LOCAL_DELETE', 'L missing, C = H',
                                       'Local deleted → remove C and H', 'L')
            else:
                self.log.custom([" ↓", "⚪"], [Fore.CYAN, Fore.CYAN], "PULL", f"{colored('Restoring to local', Fore.GREEN)} from iCloud for {d}", rel_path, level="verbose")
                await self.restore_from_icloud(rel_path)
                self.action_log.record(rel_path, 'REMOTE_CHANGED_MISSING', 'L missing, C ≠ H',
                                       'Remote changed while local missing → restore from C', 'C')
            return

        # ── iCloud missing, L+H exist ──
        if not C_exists and L_exists and H_exists:
            self.log.warn("DELETE", f"{colored('iCloud missing', Fore.RED)}, stabilizing for {d}", level="verbose")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Lh is not None and Hh is not None and Lh == Hh:
                self.log.custom([" ←", "⚫"], [Fore.RED, Fore.RED], "DELETE", f"{colored('Removing local', Fore.RED)} & history for {d}", rel_path, level="verbose")
                await self.io.remove_file(local, "local")
                await self.io.remove_file(history, "history")
                self.action_log.record(rel_path, 'REMOTE_DELETE', 'C missing, L = H',
                                       'iCloud deleted → remove L and H', 'C')
            else:
                self.log.custom([" ↑", "⚪"], [Fore.GREEN, Fore.GREEN], "PUSH", f"Local changed for {d} -> pushing to iCloud", rel_path, level="verbose")
                await self.push_to_icloud(rel_path)
                self.action_log.record(rel_path, 'LOCAL_CHANGED_MISSING', 'C missing, L ≠ H',
                                       'Local changed while iCloud missing → push L', 'L')
            return

        # ── New local file (L only) ──
        if L_exists and not C_exists and not H_exists:
            self.log.custom([" →", "⚪"], [Fore.LIGHTBLACK_EX, Fore.GREEN], "NEW", f"{colored('Local-only', Fore.GREEN)}, stabilizing {d}", rel_path, level="verbose")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Lh is None:
                self.log.info("SKIP", f"After stabilize local missing for {d}", level="verbose")
                return
            if size_or_zero(local) < cfg.tiny_threshold:
                self.log.info("SKIP", f"Local too small, deferring {d}", level="verbose")
                return
            self.log.custom([" ↑", "⚪"], [Fore.GREEN, Fore.GREEN], "PUSH", f"{colored('Pushing to iCloud', Fore.CYAN)} for {d}", rel_path, level="verbose")
            await self.push_to_icloud(rel_path)
            self.action_log.record(rel_path, 'NEW_LOCAL', 'L only',
                                   'New local file → push to C, seed H', 'L')
            return

        # ── New iCloud file (C only) ──
        if C_exists and not L_exists and not H_exists:
            self.log.custom([" →", "⚪"], [Fore.LIGHTBLACK_EX, Fore.BLUE], "NEW", f"{colored('iCloud-only', Fore.CYAN)}, stabilizing {d}", rel_path, level="verbose")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Ch is None:
                self.log.info("SKIP", f"After stabilize iCloud missing for {d}", level="verbose")
                return
            if size_or_zero(icloud) < cfg.tiny_threshold:
                self.log.info("SKIP", f"iCloud too small, deferring {d}", level="verbose")
                return
            self.log.custom([" ↓", "⚪"], [Fore.CYAN, Fore.CYAN], "PULL", f"{colored('Restoring to local', Fore.GREEN)} for {d}", rel_path, level="verbose")
            await self.restore_from_icloud(rel_path)
            self.action_log.record(rel_path, 'NEW_REMOTE', 'C only',
                                   'New iCloud file → restore to L, seed H', 'C')
            return

        # ── Both sides exist or mixed states ──
        ensure_dir(os.path.dirname(history))

        L = await self.hasher.get_cached_hash(local, 'L', rel_path) if safe_exists(local) else None
        C = await self.hasher.get_cached_hash(icloud, 'C', rel_path) if safe_exists(icloud) else None
        H = await self.hasher.get_cached_hash(history, 'H', rel_path) if safe_exists(history) else None

        # History missing — seed it
        if H is None and (L is not None or C is not None):
            self.log.info("HISTORY MISSING", f"Seeding history for {d}")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)

            if Lh is not None and Ch is not None:
                if Lh == Ch:
                    await self.io.async_copy(local, history)
                    H = Lh
                    self.log.info("HISTORY", f"Initialized history (identical) for {d}", level="verbose")
                else:
                    # Conflict at start with no history — newer wins
                    self.log.warn("HISTORY", f"Local and iCloud differ for {d}!", level="verbose")
                    local_m, icloud_m = safe_mtime(local), safe_mtime(icloud)
                    if local_m >= icloud_m:
                        self.log.warn("CONFLICT", f"{colored('Local is newer', Fore.YELLOW)}: {d}", level="important")
                        await self.io.create_conflict_duplicate(icloud)
                        await self.push_to_icloud(rel_path)
                        self.action_log.record(rel_path, 'CONFLICT_INITIAL', 'L ≠ C, H missing',
                                               'Initial conflict, local newer → push L', 'L')
                    else:
                        self.log.warn("CONFLICT", f"{colored('iCloud is newer', Fore.YELLOW)}: {d}", level="important")
                        await self.io.create_conflict_duplicate(local)
                        await self.restore_from_icloud(rel_path)
                        self.action_log.record(rel_path, 'CONFLICT_INITIAL', 'L ≠ C, H missing',
                                               'Initial conflict, iCloud newer → pull C', 'C')
                    return
            elif Lh is not None and size_or_zero(local) >= cfg.min_seed_size(rel_path):
                await self.io.async_copy(local, history)
                H = Lh
                self.log.info("HISTORY", f"Initialized {colored('from local', Fore.GREEN)} for {d}", level="verbose")
            elif Ch is not None and size_or_zero(icloud) >= cfg.min_seed_size(rel_path):
                await self.io.async_copy(icloud, history)
                H = Ch
                self.log.info("HISTORY", f"Initialized {colored('from iCloud', Fore.CYAN)} for {d}", level="verbose")
            else:
                if size_or_zero(local) > 0 or size_or_zero(icloud) > 0:
                    self.log.error("FAILED", f"Files unreadable for {d}; retrying next pass")
                else:
                    self.log.info("SKIP", f"History seeding skipped for {d}", level="verbose")
                return

        # CASE A: Identical
        if L == C == H:
            return

        # CASE B: Local changed
        if L is not None and H is not None and L != H and C == H:
            self.log.custom([" ↑", "⚪"], [Fore.GREEN, Fore.GREEN], "PUSH", f"{colored('Local changed', Fore.GREEN)}, pushing for {d}", rel_path, level="verbose")
            await self.push_to_icloud(rel_path)
            self.action_log.record(rel_path, 'LOCAL_CHANGED', 'L ≠ H, C = H',
                                   'Local changed → push L → C, update H', 'L')
            return

        # CASE C: iCloud changed
        if C is not None and H is not None and C != H and L == H:
            self.log.custom([" ↓", "⚪"], [Fore.CYAN, Fore.CYAN], "PULL", f"{colored('iCloud changed', Fore.CYAN)}, restoring for {d}", rel_path, level="verbose")
            await self.restore_from_icloud(rel_path)
            self.action_log.record(rel_path, 'REMOTE_CHANGED', 'C ≠ H, L = H',
                                   'iCloud changed → pull C → L, update H', 'C')
            return

        # CASE D: Both changed (rare)
        self.log.warn("CONFLICT", f"Both changed, stabilizing {d} {cfg.stabilize_wait}s", level="important")
        await asyncio.sleep(cfg.stabilize_wait)

        L2 = await self.hasher.get_cached_hash(local, 'L', rel_path, force=True) if safe_exists(local) else None
        C2 = await self.hasher.get_cached_hash(icloud, 'C', rel_path, force=True) if safe_exists(icloud) else None

        if L2 is not None and L2 != L:
            self.log.warn("CONFLICT", f"{colored('Local still changing', Fore.YELLOW)}, choose local: {d}", level="important")
            await self.io.create_conflict_duplicate(icloud)
            await self.push_to_icloud(rel_path)
            self.action_log.record(rel_path, 'CONFLICT_BOTH', 'L ≠ H, C ≠ H',
                                   'Both changed, local still changing → push L', 'L')
            return

        if C2 is not None and C2 != C:
            self.log.warn("CONFLICT", f"{colored('iCloud still changing', Fore.YELLOW)}, choose iCloud: {d}", level="important")
            await self.io.create_conflict_duplicate(local)
            await self.restore_from_icloud(rel_path)
            self.action_log.record(rel_path, 'CONFLICT_BOTH', 'L ≠ H, C ≠ H',
                                   'Both changed, iCloud still changing → pull C', 'C')
            return

        if (L2 is not None and C2 is not None and L2 == L and C2 == C and L2 != C2):
            self.log.warn("CONFLICT", f"{colored('Both stabilized but still differ', Fore.YELLOW)}, resolving by fallback rules: {d}", level="important")

        if not safe_exists(local):
            self.log.custom([" ↓", "🟡"], [Fore.CYAN, Fore.YELLOW], "PULL", f"{colored('Local vanished', Fore.YELLOW)}, restoring from iCloud: {d}", rel_path, level="verbose")
            await self.restore_from_icloud(rel_path)
            self.action_log.record(rel_path, 'CONFLICT_BOTH', 'L ≠ H, C ≠ H',
                                   'Both changed, local vanished → pull C', 'C')
            return

        if not safe_exists(icloud):
            self.log.custom([" ↑", "🟡"], [Fore.GREEN, Fore.YELLOW], "PUSH", f"{colored('iCloud vanished', Fore.YELLOW)}, pushing local: {d}", rel_path, level="verbose")
            await self.push_to_icloud(rel_path)
            self.action_log.record(rel_path, 'CONFLICT_BOTH', 'L ≠ H, C ≠ H',
                                   'Both changed, iCloud vanished → push L', 'L')
            return

        # Fallback: mtime comparison
        local_m, icloud_m = safe_mtime(local), safe_mtime(icloud)
        if local_m >= icloud_m:
            self.log.info("CONFLICT", f"{colored('Local is newer', Fore.YELLOW)}, push local: {d}", level="important")
            await self.io.create_conflict_duplicate(icloud)
            await self.push_to_icloud(rel_path)
            self.action_log.record(rel_path, 'CONFLICT_BOTH', 'L ≠ H, C ≠ H',
                                   'Both changed, local newer by mtime → push L', 'L')
        else:
            self.log.info("CONFLICT", f"{colored('iCloud is newer', Fore.YELLOW)}, pull iCloud: {d}", level="important")
            await self.io.create_conflict_duplicate(local)
            await self.restore_from_icloud(rel_path)
            self.action_log.record(rel_path, 'CONFLICT_BOTH', 'L ≠ H, C ≠ H',
                                   'Both changed, iCloud newer by mtime → pull C', 'C')

    # ── Concurrency wrapper ──────────────────────────────────────

    async def sync_wrapper(self, rel: str):
        """
        Catches and logs any unexpected exceptions that occur during the sync process of a specific file, ensuring the main engine loop does not crash.

        Args:
            rel (str): The relative file path to synchronize.
        """
        try:
            async with self.io_semaphore:
                await self.sync_file(rel)
        except Exception as e:
            self.log.error("ERROR", f"Error syncing {self.config.disp(rel)}: {e}")
            self.log.error("TRACEBACK", traceback.format_exc())
        finally:
            self.active_tasks.discard(rel)

    # ── Main loop ────────────────────────────────────────────────

    async def run(self):
        """
        Starts the synchronization engine.

        In `run_continuously` (daemon) mode, it polls indefinitely.
        In `one-shot` mode, it syncs all files exactly once and then exits.
        """
        cfg = self.config
        await self.log.cleanup_old_logs()
        self.log.init_log_file()

        # Validate config
        errors = cfg.validate()
        for error, level, msg in errors:
            if error == "dir_missing":
                if not cfg.history_dir or not os.path.exists(cfg.history_dir):
                    os.makedirs(cfg.history_dir, exist_ok=True)
                if not cfg.logs_dir or not os.path.exists(cfg.logs_dir):
                    os.makedirs(cfg.logs_dir, exist_ok=True)
            if level == "critical":
                self.log.error("CONFIG", msg, level="important")
                raise ValueError(f"Critical configuration error: {msg}")
            else:
                self.log.warn("CONFIG", msg, level="important")

        mode_str = "DAEMON MODE" if cfg.run_continuously else "ONE-SHOT MODE"
        self.log.startup(mode_str)
        self.hasher.load_state()
        last_save = time.time()

        try:
            while True:
                try:
                    # Cleanup expired cooldowns
                    now = time.time()
                    expired = [k for k, v in self.cooldowns.items() if v <= now]
                    for k in expired:
                        del self.cooldowns[k]

                    # Gather files
                    rel_paths = await asyncio.to_thread(self.gather_rel_paths)
                    # Include files tracked in state (for deletion detection)
                    rel_paths.update(k for k, v in self.hasher.state.items() if v)

                    # Launch tasks for files not already being processed
                    active_rels = [r for r in sorted(rel_paths) if r not in self.active_tasks]
                    tasks = []

                    if active_rels and not cfg.run_continuously:
                        self.log.header(len(active_rels))

                    for rel in active_rels:
                        self.active_tasks.add(rel)
                        tasks.append(asyncio.create_task(self.sync_wrapper(rel)))

                    # One-shot mode: wait and exit
                    if not cfg.run_continuously:
                        if tasks:
                            self.log.info("INFO",
                                          f"One-shot: waiting for {len(tasks)} tasks...",
                                          level="normal")
                            await asyncio.gather(*tasks)
                        else:
                            self.log.info("INFO", "Nothing to sync.", level="normal")
                        self.hasher.save_state()
                        self.action_log.save()
                        self.log.flush()
                        self.log.success("DONE", "One-shot sync complete.", level="normal")
                        break

                    # Daemon mode: idle indicator
                    if not tasks:
                        self.log.idle()

                    # Periodic state save (every 5s)
                    now_t = time.time()
                    if self.hasher.dirty and now_t - last_save > 5:
                        self.hasher.save_state()
                        self.action_log.save()
                        self.log.flush()
                        last_save = now_t
                    elif now_t - last_save > 5:
                        self.log.flush()
                        last_save = now_t

                except Exception as outer:
                    self.log.error("ERROR", f"Unexpected error in main loop: {outer}")
                    self.log.error("TRACEBACK", traceback.format_exc())

                await asyncio.sleep(cfg.poll_interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            self.log.warn("INFO", "Shutdown requested, saving state...", level="important")
            self.hasher.save_state()
            self.action_log.save()
            self.log.flush()
            self.log.success("DONE", "Graceful shutdown complete.", level="important")
            return
