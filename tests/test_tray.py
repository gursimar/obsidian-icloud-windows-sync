import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from obsidian_sync.tray import SyncController, SyncState


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for sync state")


class FakeEngine:
    def __init__(self):
        self.started = threading.Event()
        self.cleaned_up = threading.Event()
        self.log = MagicMock()
        self.log.log_file = "sync.log"

    async def run(self, stop_event):
        self.started.set()
        try:
            await stop_event.wait()
        finally:
            await asyncio.sleep(0)
            self.cleaned_up.set()


def test_start_stop_restart_and_close():
    engines = []

    def factory():
        engine = FakeEngine()
        engines.append(engine)
        return engine

    controller = SyncController(factory)
    try:
        assert controller.start()
        wait_for(lambda: engines and engines[0].started.is_set())
        assert controller.state == SyncState.RUNNING
        assert not controller.start()
        assert controller.stop()
        wait_for(lambda: controller.state == SyncState.STOPPED)
        assert engines[0].cleaned_up.is_set()
        wait_for(lambda: not controller._thread.is_alive())
        assert controller.start()
        wait_for(lambda: len(engines) == 2 and engines[1].started.is_set())
    finally:
        controller.close()
    assert engines[1].cleaned_up.is_set()
    assert not controller.start()


def test_stop_while_engine_is_starting():
    gate = threading.Event()
    entered = threading.Event()
    engine = FakeEngine()

    def factory():
        entered.set()
        assert gate.wait(3)
        return engine

    controller = SyncController(factory)
    try:
        controller.start()
        assert entered.wait(3)
        controller.stop()
        assert controller.state == SyncState.STOPPING
        gate.set()
        wait_for(engine.cleaned_up.is_set)
    finally:
        gate.set()
        controller.close()
    assert controller.state == SyncState.STOPPED


def test_engine_failure_is_visible_and_can_be_retried():
    broken = FakeEngine()

    async def fail(stop_event):
        raise OSError("disk unavailable")

    broken.run = fail
    engines = iter([broken, FakeEngine()])
    controller = SyncController(lambda: next(engines))
    try:
        controller.start()
        wait_for(lambda: controller.state == SyncState.ERROR)
        wait_for(lambda: not controller._thread.is_alive())
        assert controller.last_error == "disk unavailable"
        broken.log.error.assert_called_once()
        broken.log.flush.assert_called_once()
        assert controller.start()
        wait_for(lambda: controller.state == SyncState.RUNNING)
        assert controller.last_error == ""
    finally:
        controller.close()


def test_factory_failure_does_not_leave_worker_running():
    def factory():
        raise ValueError("invalid configuration")

    controller = SyncController(factory)
    try:
        controller.start()
        wait_for(lambda: controller.state == SyncState.ERROR)
        assert "invalid configuration" in controller.last_error
    finally:
        controller.close()
    assert not controller._thread.is_alive()


def test_missing_tray_extra_has_actionable_error(monkeypatch):
    import sys
    from obsidian_sync.tray import load_tray_dependencies

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pystray", None)
    with pytest.raises(RuntimeError, match="pip install"):
        load_tray_dependencies()


def test_tray_rejects_other_platforms(monkeypatch):
    import sys
    from obsidian_sync.tray import load_tray_dependencies

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows"):
        load_tray_dependencies()
