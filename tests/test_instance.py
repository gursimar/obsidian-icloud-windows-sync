import subprocess
import sys

import pytest

from obsidian_sync.instance import AlreadyRunningError, InstanceLock, config_identity


def test_relative_and_absolute_config_share_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config_identity("config.yaml") == config_identity(str(tmp_path / "config.yaml"))
    assert config_identity("config.yaml") != config_identity("other.yaml")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex")
def test_mutex_prevents_second_instance_and_releases(tmp_path):
    config = str(tmp_path / "config.yaml")
    with InstanceLock(config):
        with pytest.raises(AlreadyRunningError):
            with InstanceLock(config):
                pass
        with InstanceLock(str(tmp_path / "other.yaml")):
            pass
    with InstanceLock(config):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex")
def test_mutex_protects_against_other_process(tmp_path):
    config = str(tmp_path / "config.yaml")
    script = """
import sys
from obsidian_sync.instance import AlreadyRunningError, InstanceLock
try:
    with InstanceLock(sys.argv[1]):
        sys.exit(1)
except AlreadyRunningError:
    sys.exit(0)
"""
    with InstanceLock(config):
        result = subprocess.run([sys.executable, "-c", script, config],
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    assert result.returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex")
def test_crashed_process_does_not_leave_stale_lock(tmp_path):
    config = str(tmp_path / "config.yaml")
    script = "from obsidian_sync.instance import InstanceLock; import os, sys; lock = InstanceLock(sys.argv[1]); lock.__enter__(); os._exit(0)"
    subprocess.run([sys.executable, "-c", script, config], check=True, timeout=10,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    with InstanceLock(config):
        pass
