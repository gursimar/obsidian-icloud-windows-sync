import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from obsidian_sync import __main__ as cli
from obsidian_sync.instance import AlreadyRunningError


def test_console_mode_does_not_load_tray_dependencies(cfg, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["obsidian-sync", "--config", "config.yaml"])
    monkeypatch.setattr(cli.SyncConfig, "from_yaml", lambda path: cfg)
    engine = MagicMock()
    engine.run = AsyncMock()
    monkeypatch.setattr(cli, "create_engine", lambda config: engine)
    monkeypatch.setattr(cli, "InstanceLock", MagicMock())
    monkeypatch.setitem(sys.modules, "pystray", None)
    cli.main()
    engine.run.assert_awaited_once_with()


def test_hidden_tray_skips_cleanup(cfg, monkeypatch):
    cfg.run_continuously = True
    engine = cli.create_engine(cfg, cleanup=False)
    monkeypatch.setattr(sys, "argv", ["obsidian-sync", "--tray"])
    monkeypatch.setattr(cli.SyncConfig, "from_yaml", lambda path: cfg)
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr(cli, "create_engine", factory)
    monkeypatch.setattr(cli, "InstanceLock", MagicMock())
    from obsidian_sync import tray
    monkeypatch.setattr(tray, "load_tray_dependencies", lambda: None)
    monkeypatch.setattr(tray, "run_tray", lambda engine_factory, logs: engine_factory())
    cli.main()
    factory.assert_called_once_with(cfg, cleanup=False)


def test_create_engine_never_prompts_in_hidden_mode(cfg, monkeypatch):
    scanner = MagicMock()
    monkeypatch.setattr(cli, "DuplicateScanner", scanner)
    cli.create_engine(cfg, cleanup=False)
    scanner.return_value.scan_and_clean.assert_not_called()
    cli.create_engine(cfg)
    scanner.return_value.scan_and_clean.assert_called_once()


def test_duplicate_launch_does_not_start_sync(cfg, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["obsidian-sync"])
    monkeypatch.setattr(cli.SyncConfig, "from_yaml", lambda path: cfg)
    lock = MagicMock()
    lock.return_value.__enter__.side_effect = AlreadyRunningError("Already open")
    monkeypatch.setattr(cli, "InstanceLock", lock)
    factory = MagicMock()
    monkeypatch.setattr(cli, "create_engine", factory)
    cli.main()
    factory.assert_not_called()
    assert "Already open" in capsys.readouterr().out


def test_remove_autostart_does_not_need_existing_config(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["obsidian-sync", "--remove-autostart", "--config", "removed.yaml"])
    from obsidian_sync import startup
    remove = MagicMock()
    monkeypatch.setattr(startup, "configure_startup", remove)
    config = MagicMock(side_effect=AssertionError("Config must not be loaded"))
    monkeypatch.setattr(cli.SyncConfig, "from_yaml", config)
    cli.main()
    remove.assert_called_once_with("removed.yaml", remove=True)


def test_invalid_tray_config_does_not_launch(cfg, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["obsidian-sync", "--tray"])
    cfg.local_vault = ""
    monkeypatch.setattr(cli.SyncConfig, "from_yaml", lambda path: cfg)
    from obsidian_sync import tray
    monkeypatch.setattr(tray, "load_tray_dependencies", lambda: None)
    launch = MagicMock()
    monkeypatch.setattr(tray, "run_tray", launch)
    with pytest.raises(SystemExit):
        cli.main()
    launch.assert_not_called()


def test_relative_tray_paths_are_resolved_beside_config(cfg, tmp_path, monkeypatch):
    cfg.run_continuously = True
    cfg.local_vault = "local"
    cfg.icloud_vault = "icloud"
    cfg.history_dir = "history"
    cfg.logs_dir = "logs"
    monkeypatch.setattr(sys, "argv", ["obsidian-sync", "--tray", "--config", str(tmp_path / "config.yaml")])
    monkeypatch.setattr(cli.SyncConfig, "from_yaml", lambda path: cfg)
    monkeypatch.setattr(cli, "InstanceLock", MagicMock())
    from obsidian_sync import tray
    monkeypatch.setattr(tray, "load_tray_dependencies", lambda: None)
    launch = MagicMock()
    monkeypatch.setattr(tray, "run_tray", launch)
    cli.main()
    assert cfg.local_vault == str(tmp_path / "local")
    assert cfg.logs_dir == str(tmp_path / "logs")
    launch.assert_called_once()
