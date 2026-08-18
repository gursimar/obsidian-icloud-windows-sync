import os
import asyncio
import traceback
import time
from dataclasses import dataclass

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .sync_worker import FileSynchronizer


@dataclass(frozen=True)
class FileSyncEvent:
    event_type: str
    rel_path: str


class SyncEventHandler(FileSystemEventHandler):
    def __init__(self, engine: "SyncEngine", root: str):
        self.engine = engine
        self.root = root

    def _queue(self, event_type: str, path: str):
        if path:
            self.engine.enqueue_path_event(event_type, path, self.root)

    def on_created(self, event):
        if not event.is_directory:
            self._queue("created", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._queue("modified", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._queue("deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._queue("moved_from", event.src_path)
            self._queue("moved_to", event.dest_path)


class SyncEngine:
    def __init__(self, config, logger, hasher, disk_io, duplicates):
        self.config = config
        self.log = logger
        self.hasher = hasher
        self.io = disk_io
        self.duplicates = duplicates
        self.synchronizer = FileSynchronizer(config, logger, hasher, disk_io, duplicates)
        self.file_queues: dict[str, asyncio.Queue[FileSyncEvent]] = {}
        self.file_workers: dict[str, asyncio.Task] = {}
        self.active_tasks: set[str] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def root_label(self, root: str) -> str:
        norm_root = os.path.normcase(os.path.abspath(root))
        mapping = {
            os.path.normcase(os.path.abspath(self.config.local_vault)): "LOCAL",
            os.path.normcase(os.path.abspath(self.config.icloud_vault)): "ICLOUD",
            os.path.normcase(os.path.abspath(self.config.history_dir)): "HISTORY",
        }
        return mapping.get(norm_root, "UNKNOWN")

    def gather_rel_paths(self) -> set[str]:
        rels = set()
        cfg = self.config

        def collect(current_path: str, base_root: str):
            try:
                with os.scandir(current_path) as it:
                    for entry in it:
                        name_lower = entry.name.lower()
                        if entry.is_dir():
                            if name_lower in cfg.ignored_dirs:
                                continue
                            collect(entry.path, base_root)
                        elif entry.is_file():
                            rel = os.path.normpath(os.path.relpath(entry.path, base_root))
                            if self.should_sync_rel(rel):
                                rels.add(rel)
            except FileNotFoundError:
                pass

        collect(cfg.local_vault, cfg.local_vault)
        collect(cfg.icloud_vault, cfg.icloud_vault)
        collect(cfg.history_dir, cfg.history_dir)
        return rels

    def rel_from_root(self, path: str, root: str) -> str | None:
        try:
            rel = os.path.normpath(os.path.relpath(path, root))
        except ValueError:
            return None
        if rel == ".." or rel.startswith(".." + os.sep):
            return None
        return rel

    def should_sync_rel(self, rel_path: str) -> bool:
        name_lower = os.path.basename(rel_path).lower()
        if (
            name_lower.endswith(".tmp")
            or name_lower.endswith(".")  # Trailing dot (temp files)
            or name_lower.endswith("~")  # Backup files
            or name_lower.startswith("._")
            or name_lower in self.config.ignored_files
            or "page-preview" in name_lower
        ):
            return False
        
        # Filter files with patterns like ".mds." (extension + s + dot) which are temp files
        if "." in name_lower:
            parts = name_lower.rsplit(".", 2)
            if len(parts) == 3 and parts[1] and len(parts[1]) == 1 and parts[2] == "":
                # Matches patterns like "file.md.s." or "file.txt.x."
                return False

        parts = [part.lower() for part in os.path.normpath(rel_path).split(os.sep)]
        if any(part in self.config.ignored_dirs for part in parts):
            return False
        return not self.config.is_ignored(rel_path)

    def enqueue_path_event(self, event_type: str, path: str, root: str):
        rel = self.rel_from_root(path, root)
        if rel is None or not self.should_sync_rel(rel):
            return
        if self.loop is None or self.loop.is_closed():
            return
        root_name = self.root_label(root)
        self.log.info(
            "FS_EVENT",
            f"{root_name} {event_type} {self.config.disp(rel)}",
            level="verbose",
        )
        self.loop.call_soon_threadsafe(
            self.enqueue_file_event,
            FileSyncEvent(event_type, rel),
        )

    def enqueue_file_event(self, event: FileSyncEvent):
        queue = self.file_queues.get(event.rel_path)
        if queue is None:
            queue = asyncio.Queue()
            self.file_queues[event.rel_path] = queue

        worker = self.file_workers.get(event.rel_path)
        if worker is None or worker.done():
            self.file_workers[event.rel_path] = asyncio.create_task(
                self.file_worker(event.rel_path)
            )

        queue.put_nowait(event)
        state = "active" if event.rel_path in self.active_tasks else "idle"
        self.log.info(
            "QUEUE",
            f"Queued {event.event_type} for {self.config.disp(event.rel_path)}; worker={state}; pending={queue.qsize()}",
            level="verbose",
        )

    async def file_worker(self, rel_path: str):
        queue = self.file_queues[rel_path]
        while True:
            event = await queue.get()
            pending_count = queue.qsize()
            
            # Drain all pending events since we'll sync current state anyway
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
            
            self.active_tasks.add(rel_path)
            try:
                if pending_count > 0:
                    self.log.info(
                        "QUEUE",
                        f"Starting {event.event_type} sync for {self.config.disp(rel_path)}; drained {pending_count} pending events",
                        level="verbose",
                    )
                else:
                    self.log.info(
                        "QUEUE",
                        f"Starting {event.event_type} sync for {self.config.disp(rel_path)}",
                        level="verbose",
                    )
                await self.synchronizer.sync_wrapper(event.rel_path)
            finally:
                self.active_tasks.discard(rel_path)
                queue.task_done()
                self.log.info(
                    "QUEUE",
                    f"Finished {event.event_type} sync for {self.config.disp(rel_path)}",
                    level="verbose",
                )

    def create_observer(self):
        observer = Observer()
        watched: set[str] = set()
        for root in [self.config.local_vault, self.config.icloud_vault]:
            if not root:
                continue
            root = os.path.abspath(root)
            norm = os.path.normcase(root)
            if norm in watched or not os.path.isdir(root):
                continue
            watched.add(norm)
            observer.schedule(SyncEventHandler(self, root), root, recursive=True)
        return observer

    def validate_config(self):
        errors = self.config.validate()
        missing_dirs = [e for e in errors if e[0] == "dir_missing"]
        if missing_dirs:
            for path in [self.config.history_dir, self.config.logs_dir]:
                if path:
                    os.makedirs(path, exist_ok=True)

        for _, level, msg in errors:
            if level == "critical":
                self.log.error("CONFIG", msg, level="important")
                raise ValueError(f"Critical configuration error: {msg}")
            self.log.warn("CONFIG", msg, level="important")

    async def seed_existing_files(self):
        rel_paths = await asyncio.to_thread(self.gather_rel_paths)
        rel_paths.update(k for k, v in self.hasher.state.items() if v)
        for rel in sorted(rel_paths):
            if self.should_sync_rel(rel):
                self.enqueue_file_event(FileSyncEvent("initial", rel))

    async def run(self):
        await self.log.cleanup_old_logs()
        self.log.init_log_file()
        self.validate_config()
        self.log.startup("DAEMON MODE" if self.config.run_continuously else "ONE-SHOT MODE")
        self.hasher.load_state()

        self.loop = asyncio.get_running_loop()
        observer = self.create_observer()
        loop = asyncio.get_running_loop()
        last_save = loop.time()

        try:
            observer.start()
            await self.seed_existing_files()

            if not self.config.run_continuously:
                # Wait for every file worker spawned during seeding to finish processing
                # its queue, then fall through to the shared cleanup in `finally`.
                if self.file_queues:
                    await asyncio.gather(*(q.join() for q in self.file_queues.values()))
                return

            while True:
                try:
                    if not self.active_tasks:
                        self.log.idle()

                    now = loop.time()
                    #  periodic checkpoint timer: every 5s it saves the hasher state if dirty and flushes logs
                    if now - last_save > 5:
                        if self.hasher.dirty:
                            self.hasher.save_state()
                        self.log.flush()
                        last_save = now
                except Exception as outer:
                    self.log.error("ERROR", f"Unexpected error in main loop: {outer}")
                    self.log.error("TRACEBACK", traceback.format_exc())

                await asyncio.sleep(self.config.poll_interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            self.log.warn("INFO", "Shutdown requested, saving state...", level="important")
        finally:
            observer.stop()
            observer.join()
            for worker in self.file_workers.values():
                worker.cancel()
            self.hasher.save_state()
            self.log.flush()
            self.log.success("DONE", "Graceful shutdown complete.", level="important")
