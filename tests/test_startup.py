import sys
from unittest.mock import MagicMock

import pytest

from obsidian_sync import startup


def registry(monkeypatch):
    module = MagicMock()
    monkeypatch.setitem(sys.modules, "winreg", module)
    monkeypatch.setattr(sys, "platform", "win32")
    return module


def test_startup_command_quotes_paths_with_spaces(tmp_path, monkeypatch):
    import subprocess

    install = tmp_path / "Python Install"
    install.mkdir()
    (install / "pythonw.exe").touch()
    monkeypatch.setattr(sys, "executable", str(install / "python.exe"))
    config = tmp_path / "My Vault" / "config.yaml"
    command = startup.startup_command(str(config))
    assert command == subprocess.list2cmdline([
        str(install / "pythonw.exe"), "-X", "utf8", "-m", "obsidian_sync",
        "--tray", "--config", str(config.resolve()),
    ])
    assert '"' in command


def test_missing_pythonw_does_not_register(tmp_path, monkeypatch):
    reg = registry(monkeypatch)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    with pytest.raises(RuntimeError, match="pythonw"):
        startup.configure_startup(str(tmp_path / "config.yaml"))
    reg.CreateKeyEx.assert_not_called()


def test_install_is_current_user_and_updates_same_entry(tmp_path, monkeypatch):
    reg = registry(monkeypatch)
    monkeypatch.setattr(startup, "startup_command", lambda path: '"pythonw.exe" --tray')
    config = str(tmp_path / "config.yaml")
    startup.configure_startup(config)
    startup.configure_startup(config)
    assert reg.SetValueEx.call_count == 2
    for call in reg.CreateKeyEx.call_args_list:
        assert call.args[0] == reg.HKEY_CURRENT_USER
    assert reg.SetValueEx.call_args.args[1] == startup.startup_name(config)


def test_remove_targets_only_given_config(tmp_path, monkeypatch):
    reg = registry(monkeypatch)
    config = str(tmp_path / "config.yaml")
    startup.configure_startup(config, remove=True)
    assert reg.DeleteValue.call_args.args[1] == startup.startup_name(config)
    reg.SetValueEx.assert_not_called()


def test_remove_missing_entry_is_idempotent(tmp_path, monkeypatch):
    reg = registry(monkeypatch)
    reg.DeleteValue.side_effect = FileNotFoundError
    startup.configure_startup(str(tmp_path / "deleted.yaml"), remove=True)


def test_remove_propagates_permission_errors(tmp_path, monkeypatch):
    reg = registry(monkeypatch)
    reg.OpenKey.side_effect = PermissionError("access denied")
    with pytest.raises(PermissionError):
        startup.configure_startup(str(tmp_path / "config.yaml"), remove=True)


def test_too_long_command_does_not_register(tmp_path, monkeypatch):
    reg = registry(monkeypatch)
    monkeypatch.setattr(startup, "startup_command", lambda path: "x" * 261)
    with pytest.raises(RuntimeError, match="260"):
        startup.configure_startup(str(tmp_path / "config.yaml"))
    reg.SetValueEx.assert_not_called()


def test_unsupported_platform_cannot_change_registry(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows"):
        startup.configure_startup("config.yaml")
