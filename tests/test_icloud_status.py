import os
import asyncio
import pytest
from unittest.mock import patch, MagicMock

from conftest import (FILE_ATTRIBUTE_PINNED, FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,ICloudStatusChecker, ICloudSyncState)
from obsidian_sync.icloud_status import ICloudFileSnapshot

#  Fixtures

@pytest.fixture
def checker():
    with patch("platform.system", return_value="Windows"):
        c = ICloudStatusChecker()
    mock_k32 = MagicMock()
    mock_k32.GetFileAttributesW.return_value = 0x00000020
    mock_k32.GetFileAttributesW.argtypes = None
    mock_k32.GetFileAttributesW.restype = None
    c._k32 = mock_k32
    c._available = True
    return c

#  ICloudSyncState

class TestSyncStateIsSafe:
    def test_local_is_safe(self):
        assert ICloudSyncState.LOCAL.is_safe is True

    def test_pinned_is_safe(self):
        assert ICloudSyncState.PINNED.is_safe is True

    def test_cloud_only_not_safe(self):
        assert ICloudSyncState.CLOUD_ONLY.is_safe is False

    def test_downloading_not_safe(self):
        assert ICloudSyncState.DOWNLOADING.is_safe is False

    def test_unknown_enum_not_safe(self):
        assert ICloudSyncState.UNKNOWN.is_safe is False

    def test_all_states_have_status(self):
        for state in ICloudSyncState:
            assert hasattr(state, "status"), f"{state} missing status attribute"
            assert isinstance(state.status, str)
            assert len(state.status) > 0

#  ICloudStatusChecker

class TestDetect:
    def test_local_no_cloud_flags(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0x00000020
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.LOCAL

    def test_pinned(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_PINNED
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.PINNED

    def test_cloud_only_via_offline(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.CLOUD_ONLY

    def test_cloud_only_via_recall(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.CLOUD_ONLY

    def test_downloading_offline_plus_pinned(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_PINNED
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.DOWNLOADING

    def test_invalid_file_attributes_returns_unknown(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0xFFFFFFFF
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.UNKNOWN

    def test_winapi_exception_returns_unknown(self, checker):
        checker._k32.GetFileAttributesW.side_effect = OSError("WinAPI crash")
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.UNKNOWN

    def test_unavailable_checker_returns_unknown(self):
        with patch("platform.system", return_value="Linux"):
            c = ICloudStatusChecker()
            assert c.detect("C:/fake/file.md") == ICloudSyncState.UNKNOWN

#  Is Safe

class TestIsSafe:
    def test_local_is_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0x00000020
        assert checker.is_safe("C:/fake/file.md") is True

    def test_pinned_is_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_PINNED
        assert checker.is_safe("C:/fake/file.md") is True

    def test_cloud_only_is_not_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE
        assert checker.is_safe("C:/fake/file.md") is False

    def test_downloading_is_not_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_PINNED
        assert checker.is_safe("C:/fake/file.md") is False

    def test_unknown_is_safe_fail_open(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0xFFFFFFFF
        assert checker.is_safe("C:/fake/file.md") is True

    def test_unavailable_platform_is_safe(self):
        with patch("platform.system", return_value="Darwin"):
            c = ICloudStatusChecker()
            assert c.is_safe("C:/fake/file.md") is True


class TestShellAvailabilityStatus:
    def test_probe_does_not_create_console_window(self, checker):
        import subprocess

        with patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             patch.object(subprocess, "run") as run:
            run.return_value.stdout = "Available on this device\n"
            assert checker.shell_availability_status("C:/vault/note.md") == "Available on this device"
        assert run.call_args.kwargs["creationflags"] == 0x08000000
        assert "-NonInteractive" in run.call_args.args[0]
        assert run.call_args.kwargs["capture_output"] is True

    def test_probe_timeout_still_returns_unknown(self, checker):
        import subprocess

        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 3)):
            assert checker.shell_availability_status("C:/vault/note.md") is None


class TestWaitUntilUploaded:
    @pytest.mark.asyncio
    async def test_waits_for_pending_to_clear_before_success(self, checker):
        pending = ICloudFileSnapshot(
            state=ICloudSyncState.LOCAL,
            size_logical=10,
            size_on_disk=10,
            mtime_ns=100,
            shell_status="Sync pending",
        )
        settled = ICloudFileSnapshot(
            state=ICloudSyncState.LOCAL,
            size_logical=10,
            size_on_disk=10,
            mtime_ns=100,
            shell_status="Available on this device",
        )

        snapshots = [pending, settled, settled, settled]

        def fake_snapshot(_path):
            if snapshots:
                return snapshots.pop(0)
            return settled

        checker.snapshot = MagicMock(side_effect=fake_snapshot)
        updates: list[str] = []

        ok = await checker.wait_until_uploaded(
            "C:/fake/file.md",
            stable_seconds=0.01,
            poll_seconds=0.01,
            timeout_seconds=0.2,
            on_update=updates.append,
        )

        assert ok is True
        assert any("pending=True" in update for update in updates)
        assert "settled after" in updates[-1]  # Message now includes checkmark emoji prefix

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
