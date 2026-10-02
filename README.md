# Obsidian iCloud Windows Sync

A highly optimized, asynchronous, three-way sync engine designed to solve the notorious issues between Obsidian, iCloud Drive, and Windows.

![terminal image](assets/image.png)

## Installation & Setup
1. Install iCloud for Windows [Link](https://support.apple.com/en-ca/103232)
2. Clone the repository using `git clone git@github.com:gursimar/obsidian-icloud-windows-sync.git`
3. Install as a package:
   ```bash
   pip install .
   ```
4. Copy and edit the config file with your actual paths:
   ```bash
   cp config.yaml my-config.yaml
   ```
   ```yaml
   paths:
     local_vault: "C:\\Obsidian\\Vault"
     icloud_vault: "C:\\Users\\user\\iCloudDrive\\iCloud~md~obsidian"
     history_dir: "C:\\Obsidian\\History"
     logs_dir: "C:\\Obsidian\\Logs"
   ```
5. Run:
   ```bash
   obsidian-sync --config config.yaml
   ```
   Or directly:
   ```bash
   python -m obsidian_sync --config config.yaml
   ```

> If you have any trouble in setup, raise issue on git.
> Run natively on Windows, not WSL — iCloud placeholders behave incorrectly under WSL.

## Project Structure

```
obsidian_sync/
├── __main__.py      # CLI entry point
├── config.py        # YAML config loading & validation
├── logger.py        # Structured logging (console + file)
├── disk_io.py       # Atomic copy, delete, Windows API
├── hasher.py        # SHA-256 hashing with mtime/size cache
├── icloud_status.py # Windows file attributes for iCloud sync state
├── duplicates.py    # Startup duplicate/conflict scanner
├── sync_engine.py   # File watchers + per-file event queues
└── sync_worker.py   # Three-way sync logic with atomic operations
```

### Modes of Operation

| Mode | Config | Behavior |
|---|---|---|
| **One-Shot** | `run_continuously: false` | Single full pass, then exits. Use with Task Scheduler. |
| **Daemon** | `run_continuously: true` | Runs continuously, polling every `poll_interval` seconds. |

#### Autostart via Task Scheduler

Create `run-sync.ps1`:
```powershell
& py -m obsidian_sync --config "$PSScriptRoot\config.yaml"
```

In `taskschd.msc` → Create Task:
- **Triggers**: At log on, delay 1 minute
- **Actions**: `C:\Windows\System32\conhost.exe` with arguments:
   `--headless powershell.exe -WindowStyle Hidden -NoProfile -NonInteractive -file "C:\PATH\TO\run-sync.ps1"`
- **Settings**: Restart on failure every 1 minute, up to 99 times

---

## Windows system tray (optional)

Install the tray dependencies from the repository:

```powershell
python -m pip install ".[tray]"
python -m obsidian_sync --tray --config "C:\Obsidian\my-config.yaml"
```

The sync engine runs in the same Python process as the icon. The usual console
command still works without the tray dependencies.

The icon appears automatically when tray mode starts. Right-click it to start or
stop sync, open the log folder, or quit. Stop keeps the icon available so sync can
be started again. Quit stops sync and removes the icon. Both request a graceful
shutdown: file watchers stop, pending workers are
cancelled, and the hash cache and logs are saved before the worker exits. Start
creates a fresh worker using the configuration loaded when the tray was opened.
Reopen the tray to apply configuration changes.

| Icon | Meaning |
|---|---|
| Green | Sync worker is running |
| Gray | Sync is stopped |
| Orange | Sync is starting or stopping |
| Red | The worker failed; open the logs, fix the problem, and use Start sync |

Green indicates that the worker is running. It does not confirm that iCloud has
uploaded every file or that another device has received it. Windows may place the
icon under the notification area's hidden-icons arrow.

Tray mode skips the interactive duplicate cleanup prompt and preserves those
files. Run the console command if you want to review that cleanup. Tray mode
requires `run_continuously: true` and a valid configuration. Relative vault,
history, and log paths are resolved from the configuration file's folder in tray
mode, so logon does not depend on a particular working directory.

### Start at Windows logon

Use a permanent Python installation or virtual environment, then run:

```powershell
python -m obsidian_sync --install-autostart --config "C:\Obsidian\my-config.yaml"
```

This registers a `pythonw.exe` command under the current user's Windows Run key.
It takes effect at the next logon, shows no console window, and needs no
administrator privileges. Paths are stored as absolute paths; keep the Python
environment and configuration at those locations. Very long commands are rejected
because Windows Run entries are limited to 260 characters.

Remove the entry before moving the configuration or uninstalling the package:

```powershell
python -m obsidian_sync --remove-autostart --config "C:\Obsidian\my-config.yaml"
```

Removal is safe to repeat and does not stop the current process. Disable any older
scheduled task or startup shortcut when switching to this method. Both console
and tray launches use a Windows mutex to prevent a second updated instance with
the same configuration path in the same login session. Configurations that use
different files must point to separate vaults/history directories.

To launch the tray manually without a console, use `pythonw -X utf8 -m
obsidian_sync --tray --config "C:\Obsidian\my-config.yaml"`. If the icon does not
appear, run the regular `python` command in a terminal to see startup errors.
Autostart can also be disabled in Windows Settings > Apps > Startup.

---

## How It Works

Three locations are tracked per file:
- **L** = Local vault (`local_vault`)
- **C** = iCloud copy (`icloud_vault`)
- **H** = History snapshot (`history_dir`) — last known good state

Each sync pass walks the union of all three directories and applies these rules:

| State | Action |
|---|---|
| `L` only | New local file → stabilize → push to `C`, seed `H` |
| `C` only | New remote file → stabilize → restore to `L`, seed `H` |
| `L == C == H` | Nothing to do |
| `L != H`, `C == H` | Local changed → push `L` → `C`, update `H` |
| `C != H`, `L == H` | Remote changed → restore `L` from `C`, update `H` |
| `L != H`, `C != H` | Conflict → stabilize → pick newer by mtime, backup loser as `_CONFLICT_TIMESTAMP` |
| `L` missing, `C == H` | Confirmed local delete → remove `C` and `H` |
| `L` missing, `C != H` | Remote changed → restore `L` from `C` |
| `C` missing, `L == H` | Confirmed remote delete → remove `L` and `H` |
| `C` missing, `L != H` | Local changed → push `L` → `C` |
| `L` and `C` missing | Remove orphaned `H` |

### Key Protections

- **Per-file event queues**: each file has dedicated queue and worker task for complete isolation
- **Atomic snapshot propagation**: single source read to temp file, then fan out to all destinations
- **Stabilization** (`stability_window`): waits before acting on creates/deletes to avoid reacting to mid-save or rename workflows
- **Conflict wait** (`stabilize_wait`): longer wait on both-changed scenarios to detect still-active edits
- **Atomic writes**: write to `.tmp` then `os.replace()`, with retries and Win32 `MoveFileEx` fallback
- **Conflict duplicates**: losing side saved as `filename_CONFLICT_TIMESTAMP.ext` before overwrite

---

## Tuning

All settings live in `config.yaml`:

| Setting | Default | Notes |
|---|---|---|
| `stability_window` | `3s` | Increase for slow disks or large files |
| `stabilize_wait` | `8s` | Increase if you edit very slowly |
| `tiny_threshold` | `8 bytes` | Minimum file size to sync (prevents empty/placeholder files) |
| `max_concurrent_io` | `50` | Maximum concurrent I/O operations |
| `ignore.patterns` | `[]` | Exclude folders like `.obsidian/cache` |
