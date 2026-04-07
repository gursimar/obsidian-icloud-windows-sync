# Obsidian iCloud Windows Sync

A highly optimized, asynchronous, three-way sync engine designed to solve the notorious issues between Obsidian, iCloud Drive, and Windows.

![terminal image](assets/image.png)

## Installation & Setup

1. Clone the repository.
2. Install as a package:
   ```bash
   pip install .
   ```
3. Copy and edit the config file with your actual paths:
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
4. Run:
   ```bash
   obsidian-sync --config config.yaml
   ```
   Or directly:
   ```bash
   python -m obsidian_sync --config config.yaml
   ```

> Run natively on Windows, not WSL — iCloud placeholders behave incorrectly under WSL.


## Portal/ Web UI Mode

![portal mode screenshot](assets/portal-mode.png)

Portal mode runs the sync engine in the background and shows live file status in a web UI.

### Run portal mode

```bash
obsidian-sync --config config.yaml --portal --port 8384
```

Or directly:

```bash
python -m obsidian_sync --config config.yaml --portal --port 8384
```

Open:

`http://127.0.0.1:8384`


## Project Structure

```
src/obsidian_sync/
├── __main__.py      # CLI entry point
├── config.py        # YAML config loading & validation
├── logger.py        # Structured logging (console + file)
├── disk_io.py       # Atomic copy, delete, Windows API
├── hasher.py        # SHA-256 hashing with mtime/size cache
├── duplicates.py    # Startup duplicate/conflict scanner
├── sync_engine.py   # Main loop + three-way sync logic
├── portal_nicegui.py# NiceGUI portal UI
└── portal/scanner.py# Portal data scanner
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

- **Stabilization** (`stability_window`): waits before acting on creates/deletes to avoid reacting to mid-save or rename workflows
- **Conflict wait** (`stabilize_wait`): longer wait on both-changed scenarios to detect still-active edits
- **Cooldowns**: skips recently synced files to prevent autosave thrash; longer for large files (`big_file_cooldown`)
- **Atomic writes**: write to `.tmp` then `os.replace()`, with retries and Win32 `MoveFileEx` fallback
- **Conflict duplicates**: losing side saved as `filename_CONFLICT_TIMESTAMP.ext` before overwrite

---

## Tuning

All settings live in `config.yaml`:

| Setting | Default | Notes |
|---|---|---|
| `stability_window` | `3s` | Increase for slow disks or large files |
| `stabilize_wait` | `8s` | Increase if you edit very slowly |
| `cooldown_seconds` | `3s` | Increase if autosave causes loops |
| `big_file_threshold` | `100KB` | Files above this get `big_file_cooldown` |
| `ignore.patterns` | `[]` | Exclude folders like `.obsidian/cache` |