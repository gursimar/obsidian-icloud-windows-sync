"""Optional Windows tray UI with one in-process sync worker."""

import asyncio
from enum import StrEnum
import os
import sys
import threading
import traceback
from typing import Callable


class SyncState(StrEnum):
    STOPPED = "Stopped"
    STARTING = "Starting"
    RUNNING = "Running"
    STOPPING = "Stopping"
    ERROR = "Error"


class SyncController:
    """Run sync on its own asyncio loop without blocking tray callbacks."""

    def __init__(self, engine_factory: Callable):
        self._engine_factory = engine_factory
        self._lock = threading.RLock()
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._stop_requested = threading.Event()
        self._closed = False
        self._state = SyncState.STOPPED
        self.last_error = ""

    @property
    def state(self):
        with self._lock:
            return self._state

    def start(self):
        with self._lock:
            if self._closed or (self._thread and self._thread.is_alive()):
                return False
            self._stop_requested.clear()
            self.last_error = ""
            self._state = SyncState.STARTING
            self._thread = threading.Thread(target=self._worker, name="obsidian-sync")
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return False
            self._state = SyncState.STOPPING
            self._stop_requested.set()
            if self._loop and self._stop_event and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._stop_event.set)
            return True

    def close(self):
        with self._lock:
            self._closed = True
            self.stop()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join()

    def _worker(self):
        engine = None
        try:
            engine = self._engine_factory()

            async def run():
                with self._lock:
                    self._loop = asyncio.get_running_loop()
                    self._stop_event = asyncio.Event()
                    if self._stop_requested.is_set():
                        self._stop_event.set()
                    else:
                        self._state = SyncState.RUNNING
                await engine.run(stop_event=self._stop_event)

            asyncio.run(run())
            with self._lock:
                self._state = SyncState.STOPPED
        except Exception as error:
            if engine is not None:
                engine.log.error("TRAY", traceback.format_exc(), level="important")
                engine.log.flush()
            with self._lock:
                self.last_error = str(error)
                self._state = SyncState.ERROR
        finally:
            with self._lock:
                self._loop = None
                self._stop_event = None


def load_tray_dependencies():
    if sys.platform != "win32":
        raise RuntimeError("The system tray is only supported on Windows.")
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError('Install the tray extra first: pip install ".[tray]"') from error
    return pystray, Image, ImageDraw


def run_tray(engine_factory: Callable, logs_dir: str):
    pystray, Image, ImageDraw = load_tray_dependencies()
    controller = SyncController(engine_factory)
    finished = threading.Event()

    def image(color):
        bitmap = Image.new("RGBA", (32, 32))
        draw = ImageDraw.Draw(bitmap)
        draw.ellipse((3, 3, 29, 29), fill=color)
        draw.arc((9, 9, 23, 23), 35, 315, fill="white", width=2)
        return bitmap

    images = {
        SyncState.STOPPED: image("gray"),
        SyncState.STARTING: image("orange"),
        SyncState.RUNNING: image("forestgreen"),
        SyncState.STOPPING: image("orange"),
        SyncState.ERROR: image("firebrick"),
    }

    def open_logs(icon, item):
        try:
            os.startfile(logs_dir)
        except OSError as error:
            icon.notify(str(error), "Could not open logs")

    def quit_sync(icon, item):
        def shutdown():
            controller.close()
            finished.set()
            icon.stop()
        threading.Thread(target=shutdown, name="tray-shutdown").start()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: "Sync: " + controller.state.value, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start sync", lambda icon, item: controller.start(),
                         enabled=lambda item: controller.state in (SyncState.STOPPED, SyncState.ERROR)),
        pystray.MenuItem("Stop sync", lambda icon, item: controller.stop(),
                         enabled=lambda item: controller.state in (SyncState.STARTING, SyncState.RUNNING)),
        pystray.MenuItem("Open logs", open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit (stop sync)", quit_sync),
    )
    icon = pystray.Icon("obsidian-sync", images[SyncState.STOPPED], "Obsidian Sync: Stopped", menu)

    def setup(icon):
        icon.visible = True
        controller.start()
        previous = None
        while not finished.is_set():
            state = controller.state
            if state != previous:
                icon.icon = images[state]
                icon.title = "Obsidian Sync: " + state.value
                icon.update_menu()
                if state == SyncState.ERROR:
                    icon.notify(controller.last_error, "Sync stopped with an error")
                previous = state
            finished.wait(0.5)

    try:
        icon.run(setup=setup)
    finally:
        finished.set()
        controller.close()
        icon.stop()
        for bitmap in images.values():
            bitmap.close()
