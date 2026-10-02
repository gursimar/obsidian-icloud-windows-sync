import os
import time
import hashlib
import asyncio
import pytest
from obsidian_sync.disk_io import DiskIO
from unittest.mock import patch, MagicMock, AsyncMock
from conftest import SyncEngine, FileSynchronizer, FileHasher, DiskIO
from obsidian_sync.sync_engine import FileSyncEvent

#  Fixtures

@pytest.fixture
def eng(cfg, mock_log, tmp_path):
    with patch("platform.system", return_value="Windows"):
        real_io = DiskIO(cfg, mock_log)
    h   = FileHasher(cfg, mock_log)
    dup = MagicMock()
    return FileSynchronizer(cfg, mock_log, h, real_io, dup)

@pytest.fixture
def engine(cfg, mock_log, tmp_path):
    with patch("platform.system", return_value="Windows"):
        real_io = DiskIO(cfg, mock_log)
    h = FileHasher(cfg, mock_log)
    return SyncEngine(cfg, mock_log, h, real_io, MagicMock())

def _write(path: str, content: str = "content"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

def _local(cfg, rel): return os.path.join(cfg.local_vault,  rel)
def _icloud(cfg, rel): return os.path.join(cfg.icloud_vault, rel)
def _history(cfg, rel): return os.path.join(cfg.history_dir,  rel)

#  gather_rel_paths

class TestGatherRelPaths:
    @pytest.mark.asyncio
    async def test_collects_files_from_local(self, engine, cfg):
        _write(_local(cfg, "a.md"))
        _write(_local(cfg, "b.md"))
        paths = engine.gather_rel_paths()
        assert "a.md" in paths
        assert "b.md" in paths

    @pytest.mark.asyncio
    async def test_collects_files_from_icloud(self, engine, cfg):
        _write(_icloud(cfg, "c.md"))
        paths = engine.gather_rel_paths()
        assert "c.md" in paths

    @pytest.mark.asyncio
    async def test_deduplicates_across_vaults(self, engine, cfg):
        _write(_local(cfg, "same.md"))
        _write(_icloud(cfg, "same.md"))
        paths = engine.gather_rel_paths()
        assert "same.md" in paths

    @pytest.mark.asyncio
    async def test_filters_tmp_files(self, engine, cfg):
        _write(_local(cfg, "draft.tmp"))
        paths = engine.gather_rel_paths()
        assert "draft.tmp" not in paths

    @pytest.mark.asyncio
    async def test_filters_trailing_dot_files(self, engine, cfg):
        _write(_local(cfg, "file.md."))
        paths = engine.gather_rel_paths()
        assert "file.md." not in paths

    @pytest.mark.asyncio
    async def test_filters_temp_save_files(self, engine, cfg):
        _write(_local(cfg, "file.mds."))
        _write(_local(cfg, "note.mdx."))
        paths = engine.gather_rel_paths()
        assert "file.mds." not in paths
        assert "note.mdx." not in paths

    @pytest.mark.asyncio
    async def test_filters_backup_tilde_files(self, engine, cfg):
        _write(_local(cfg, "draft.md~"))
        paths = engine.gather_rel_paths()
        assert "draft.md~" not in paths

    @pytest.mark.asyncio
    async def test_filters_dotunderscore_files(self, engine, cfg):
        _write(_local(cfg, "._something.md"))
        paths = engine.gather_rel_paths()
        assert all("._" not in p for p in paths)

    @pytest.mark.asyncio
    async def test_filters_page_preview(self, engine, cfg):
        _write(_local(cfg, "page-preview.md"))
        paths = engine.gather_rel_paths()
        assert "page-preview.md" not in paths

    @pytest.mark.asyncio
    async def test_filters_ignored_patterns(self, engine, cfg):
        cfg.ignore_patterns = ["private/*.md"]
        os.makedirs(os.path.join(cfg.local_vault, "private"), exist_ok=True)
        _write(_local(cfg, "private/secret.md"))
        paths = engine.gather_rel_paths()
        assert os.path.normpath("private/secret.md") not in paths

    @pytest.mark.asyncio
    async def test_filters_ignored_dirs(self, engine, cfg):
        cfg.ignored_dirs = [".git"]
        os.makedirs(os.path.join(cfg.local_vault, ".git"), exist_ok=True)
        _write(_local(cfg, ".git/HEAD"))
        paths = engine.gather_rel_paths()
        assert ".git/HEAD" not in paths

    @pytest.mark.asyncio
    async def test_handles_empty_vaults(self, engine, cfg):
        paths = engine.gather_rel_paths()
        assert isinstance(paths, (list, set))
        assert len(paths) == 0

#  sync_file L/C/H

class TestSyncFileStates:
    @pytest.mark.asyncio
    async def test_all_missing_does_nothing(self, eng, cfg):
        await eng.sync_file("ghost.md")
        eng.log.success.assert_not_called()
        eng.log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_history_cleans_it_up(self, eng, cfg):
        _write(_history(cfg, "orphan.md"))
        await eng.sync_file("orphan.md")
        assert not os.path.exists(_history(cfg, "orphan.md"))

    @pytest.mark.asyncio
    async def test_local_only_pushes_to_icloud(self, eng, cfg):
        _write(_local(cfg, "new.md"), "fresh content")
        await eng.sync_file("new.md")
        assert os.path.exists(_icloud(cfg, "new.md"))
        assert os.path.exists(_history(cfg, "new.md"))

    @pytest.mark.asyncio
    async def test_icloud_only_pulls_to_local(self, eng, cfg):
        _write(_icloud(cfg, "remote.md"), "from cloud")
        await eng.sync_file("remote.md")
        assert os.path.exists(_local(cfg, "remote.md"))
        assert os.path.exists(_history(cfg, "remote.md"))

    @pytest.mark.asyncio
    async def test_identical_l_and_c_no_h_seeds_history(self, eng, cfg):
        content = "same content"
        _write(_local(cfg, "sync.md"), content)
        _write(_icloud(cfg, "sync.md"), content)
        await eng.sync_file("sync.md")
        assert os.path.exists(_history(cfg, "sync.md"))

    @pytest.mark.asyncio
    async def test_identical_l_c_h_skips(self, eng, cfg):
        content = "stable content"
        for f in (_local(cfg, "stable.md"), _icloud(cfg, "stable.md"), _history(cfg, "stable.md")):
            _write(f, content)
        h = hashlib.sha256(content.encode()).hexdigest()
        eng.hasher.state["stable.md"] = {
            "L": {"mtime": os.path.getmtime(_local(cfg, "stable.md")), "size": os.path.getsize(_local(cfg, "stable.md")), "hash": h},
            "C": {"mtime": os.path.getmtime(_icloud(cfg, "stable.md")), "size": os.path.getsize(_icloud(cfg, "stable.md")), "hash": h},
            "H": {"mtime": os.path.getmtime(_history(cfg, "stable.md")), "size": os.path.getsize(_history(cfg, "stable.md")), "hash": h},
        }
        eng.log.success.reset_mock()
        await eng.sync_file("stable.md")
        content_icloud = open(_icloud(cfg, "stable.md")).read()
        content_local = open(_local(cfg, "stable.md")).read()
        assert content_icloud == "stable content"
        assert content_local == "stable content"

    @pytest.mark.asyncio
    async def test_local_changed_pushes(self, eng, cfg):
        old = "old content"
        new = "new local content"
        for f in (_local(cfg, "mod.md"), _icloud(cfg, "mod.md"), _history(cfg, "mod.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        old_mtime = os.path.getmtime(_history(cfg, "mod.md"))
        eng.hasher.state["mod.md"] = {
            "L": {"mtime": old_mtime, "size": len(old), "hash": h},
            "C": {"mtime": old_mtime, "size": len(old), "hash": h},
            "H": {"mtime": old_mtime, "size": len(old), "hash": h},
        }
        time.sleep(0.01)
        _write(_local(cfg, "mod.md"), new)
        await eng.sync_file("mod.md")
        assert open(_icloud(cfg, "mod.md")).read() == new

    @pytest.mark.asyncio
    async def test_icloud_changed_pulls(self, eng, cfg):
        old = "shared base"
        new_cloud = "new icloud content"
        for f in (_local(cfg, "pull.md"), _icloud(cfg, "pull.md"), _history(cfg, "pull.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        old_mtime = os.path.getmtime(_local(cfg, "pull.md"))
        eng.hasher.state["pull.md"] = {
            "L": {"mtime": old_mtime, "size": len(old), "hash": h},
            "C": {"mtime": old_mtime, "size": len(old), "hash": h},
            "H": {"mtime": old_mtime, "size": len(old), "hash": h},
        }
        time.sleep(0.01)
        _write(_icloud(cfg, "pull.md"), new_cloud)
        await eng.sync_file("pull.md")
        assert open(_local(cfg, "pull.md")).read() == new_cloud

    @pytest.mark.asyncio
    async def test_both_changed_conflict_resolves(self, eng, cfg):
        base = "base version"
        local_new = "local changed"
        cloud_new = "cloud changed"
        for f in (_local(cfg, "conflict.md"), _icloud(cfg, "conflict.md"), _history(cfg, "conflict.md")):
            _write(f, base)
        h = hashlib.sha256(base.encode()).hexdigest()
        old_t = os.path.getmtime(_history(cfg, "conflict.md"))
        eng.hasher.state["conflict.md"] = {
            "L": {"mtime": old_t, "size": len(base), "hash": h},
            "C": {"mtime": old_t, "size": len(base), "hash": h},
            "H": {"mtime": old_t, "size": len(base), "hash": h},
        }
        time.sleep(0.01)
        _write(_local(cfg, "conflict.md"), local_new)
        time.sleep(0.01)
        _write(_icloud(cfg, "conflict.md"), cloud_new)
        await eng.sync_file("conflict.md")
        content = open(_local(cfg, "conflict.md")).read()
        assert content == cloud_new

    @pytest.mark.asyncio
    async def test_user_deleted_local_removes_everywhere(self, eng, cfg):
        old = "to delete"
        for f in (_icloud(cfg, "del.md"), _history(cfg, "del.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_icloud(cfg, "del.md"))
        eng.hasher.state["del.md"] = {
            "C": {"mtime": t, "size": len(old), "hash": h},
            "H": {"mtime": t, "size": len(old), "hash": h},
        }
        await eng.sync_file("del.md")
        assert not os.path.exists(_icloud(cfg, "del.md"))
        assert not os.path.exists(_history(cfg, "del.md"))

    @pytest.mark.asyncio
    async def test_user_deleted_icloud_removes_everywhere(self, eng, cfg):
        old = "to delete"
        for f in (_local(cfg, "del2.md"), _history(cfg, "del2.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_local(cfg, "del2.md"))
        eng.hasher.state["del2.md"] = {
            "L": {"mtime": t, "size": len(old), "hash": h},
            "H": {"mtime": t, "size": len(old), "hash": h},
        }
        await eng.sync_file("del2.md")
        assert not os.path.exists(_local(cfg, "del2.md"))
        assert not os.path.exists(_history(cfg, "del2.md"))

    @pytest.mark.asyncio
    async def test_no_local_but_icloud_changed_restores(self, eng, cfg):
        old = "shared base"
        new = "cloud updated"
        _write(_history(cfg, "restore.md"), old)
        _write(_icloud(cfg, "restore.md"), new)
        h_old = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_history(cfg, "restore.md"))
        eng.hasher.state["restore.md"] = {
            "H": {"mtime": t, "size": len(old), "hash": h_old},
        }
        await eng.sync_file("restore.md")
        assert os.path.exists(_local(cfg, "restore.md"))

    @pytest.mark.asyncio
    async def test_no_icloud_but_local_changed_pushes(self, eng, cfg):
        old = "shared base"
        new = "local updated"
        _write(_history(cfg, "push2.md"), old)
        _write(_local(cfg, "push2.md"), new)
        h_old = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_history(cfg, "push2.md"))
        eng.hasher.state["push2.md"] = {
            "H": {"mtime": t, "size": len(old), "hash": h_old},
        }
        await eng.sync_file("push2.md")
        assert os.path.exists(_icloud(cfg, "push2.md"))

#  Tiny file guard

class TestTinyFiles:
    @pytest.mark.asyncio
    async def test_tiny_file_skipped_from_local(self, eng, cfg):
        cfg.tiny_threshold = 10
        _write(_local(cfg, "tiny.md"), "hi")
        await eng.sync_file("tiny.md")
        assert not os.path.exists(_icloud(cfg, "tiny.md"))

    @pytest.mark.asyncio
    async def test_obsidian_settings_not_skipped(self, eng, cfg):
        cfg.tiny_threshold = 10
        os.makedirs(os.path.join(cfg.local_vault, ".obsidian"), exist_ok=True)
        _write(os.path.join(cfg.local_vault, ".obsidian", "app.json"), "{}")
        await eng.sync_file(".obsidian/app.json")
        assert os.path.exists(os.path.join(cfg.icloud_vault, ".obsidian", "app.json"))


class TestPerFileQueue:
    @pytest.mark.asyncio
    async def test_drains_duplicate_events_while_worker_busy(self, engine):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def fake_sync(rel):
            started.set()
            await gate.wait()

        engine.synchronizer.sync_wrapper = AsyncMock(side_effect=fake_sync)

        engine.enqueue_file_event(FileSyncEvent("modified", "note.md"))
        await asyncio.wait_for(started.wait(), timeout=1)

        engine.enqueue_file_event(FileSyncEvent("modified", "note.md"))
        engine.enqueue_file_event(FileSyncEvent("modified", "note.md"))

        # Now both events are in queue
        assert engine.file_queues["note.md"].qsize() == 2

        gate.set()
        await asyncio.wait_for(engine.file_queues["note.md"].join(), timeout=1)

        # Worker processes once more and drains pending events
        assert engine.synchronizer.sync_wrapper.await_count == 2


class TestWatcherScopeAndSuppression:
    def test_observer_watches_only_local_and_icloud(self, engine, cfg):
        with patch("obsidian_sync.sync_engine.Observer") as observer_cls:
            observer = MagicMock()
            observer_cls.return_value = observer

            engine.create_observer()

            scheduled_roots = [call.args[1] for call in observer.schedule.call_args_list]
            assert os.path.abspath(cfg.local_vault) in scheduled_roots
            assert os.path.abspath(cfg.icloud_vault) in scheduled_roots
            assert os.path.abspath(cfg.history_dir) not in scheduled_roots


class TestGracefulStop:
    @pytest.mark.asyncio
    async def test_stop_event_wakes_long_poll_and_cleans_workers(self, engine, cfg):
        cfg.poll_interval = 60
        engine.log.cleanup_old_logs = AsyncMock()
        engine.hasher.save_state = MagicMock()
        observer = MagicMock()
        engine.create_observer = MagicMock(return_value=observer)
        started = asyncio.Event()
        cleaned = asyncio.Event()
        stop = asyncio.Event()

        async def pending_sync(rel):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        async def seed():
            engine.enqueue_file_event(FileSyncEvent("initial", "note.md"))
            await started.wait()

        engine.seed_existing_files = seed
        engine.synchronizer.sync_wrapper = pending_sync
        task = asyncio.create_task(engine.run(stop_event=stop))
        await asyncio.wait_for(started.wait(), timeout=1)
        engine.hasher.save_state.side_effect = lambda: pytest.fail("State saved before worker cleanup") if not cleaned.is_set() else None
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        observer.stop.assert_called_once()
        observer.join.assert_called_once()
        assert cleaned.is_set()
        assert all(worker.done() for worker in engine.file_workers.values())
        engine.hasher.save_state.assert_called_once()


#  push_to_icloud / restore_from_icloud

class TestPushRestore:
    @pytest.mark.asyncio
    async def test_push_copies_to_icloud_and_history(self, eng, cfg):
        _write(_local(cfg, "push.md"), "push content")
        await eng.push_to_icloud("push.md")
        assert os.path.exists(_icloud(cfg, "push.md"))
        assert os.path.exists(_history(cfg, "push.md"))

    @pytest.mark.asyncio
    async def test_restore_copies_to_local_and_history(self, eng, cfg):
        _write(_icloud(cfg, "restore.md"), "cloud content")
        await eng.restore_from_icloud("restore.md")
        assert os.path.exists(_local(cfg, "restore.md"))
        assert os.path.exists(_history(cfg, "restore.md"))

    @pytest.mark.asyncio
    async def test_restore_uses_icloud_read_guard(self, eng, cfg):
        eng.io.copy_from_icloud = AsyncMock()
        await eng.restore_from_icloud("guarded.md")
        assert eng.io.copy_from_icloud.call_count == 1

#  sync_wrapper

class TestSyncWrapper:
    @pytest.mark.asyncio
    async def test_exception_is_caught_and_logged(self, eng, cfg):
        eng.io.copy_to_icloud = AsyncMock(side_effect=RuntimeError("disk full"))
        _write(_local(cfg, "broken.md"), "x" * 50)
        await eng.sync_wrapper("broken.md")
        eng.log.error.assert_called()

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
