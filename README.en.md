# Folder Sync Backup (資料夾同步備份)

**English** · [繁體中文](README.md)

Backs up chosen folders on your Mac to an external drive in real time. It runs quietly in the background, and how much it uses the CPU and disk can be adjusted.

The interface is in Traditional Chinese. Requires Python 3 (PySide6 + watchdog).

## Install and launch

```bash
git clone https://github.com/liao2k/syncbackup.git
cd syncbackup
```

1. Double-click `安裝.command` (only needed the first time). It creates a Python virtual environment, installs the packages and builds `資料夾同步備份.app`.
2. Double-click `資料夾同步備份.app` to open it.

> If macOS says the app can't be opened, right-click it → Open.
> No Python yet? Install it first, e.g. `brew install python`.

## Features

| Tab | Contents |
|---|---|
| **備份任務** (Backup tasks) | Add / edit / remove tasks, enable switch, sync now, preview changes, open source / backup / version history |
| **活動紀錄** (Activity log) | Every file added, updated, deleted or failed; filter by task or action, or search by file name |
| **同步紀錄** (Sync history) | Summary of each sync run (added / updated / deleted counts, data transferred, duration, result) |
| **效能設定** (Performance) | Quiet / Balanced / Full speed / Custom; speed limit, CPU cap, priority, auto-pause |
| **一般設定** (General) | Launch at login, run in background, hide Dock icon, notifications |

### Sync modes
- **Mirror**: the backup matches the source exactly; files deleted from the source are deleted from the backup too
- **Accumulate**: only adds and updates, never deletes anything from the backup

### Safety
- **Version history**: overwritten or deleted files are moved to `_備份歷史版本/<date-time>/` on the backup drive, kept for a configurable number of days
- **Mass-deletion protection**: if a sync would delete more than 30% of the backed-up files, it pauses and waits for your confirmation
- **Disconnected-drive detection**: nothing is written while the external drive is unplugged; changes made in the meantime are caught up when it's reconnected
- If a source folder can't be read, its copies in the backup are not deleted
- Files are written to a temporary file first and swapped in only when complete, so an interruption never leaves a corrupted file

### Performance controls
| Setting | Description |
|---|---|
| Max transfer speed | Limits how much data per second is written to the external drive |
| CPU cap | Share of CPU time the sync thread may use |
| Priority | macOS QoS: the "background" level prefers efficiency cores and yields disk I/O to other apps |
| Pause between files | Reduces disk load when there are many small files |
| Wait after changes | Merges a burst of changes into a single backup |
| Periodic full check | Catches changes that live monitoring might have missed |
| Auto-pause | When on battery power, or when the system is busy |

When the window is closed, only a menu-bar icon remains and the interface stops updating, keeping resource use as low as possible.

## File locations
- Settings and logs: `~/Library/Application Support/FolderSyncBackup/`
- Launch at login: `~/Library/LaunchAgents/com.local.foldersyncbackup.plist`

## Notes
- The first time you back up Documents, Desktop or Downloads, macOS asks for access; click "Allow". If it still fails, add Python under System Settings › Privacy & Security › Full Disk Access
- Aliases (symbolic links) are not backed up
- If the external drive is formatted as exFAT / FAT, file names with certain special characters may not be writable; these show up as errors in the activity log

## Project structure
```
app.py                     entry point (single instance, menu bar)
build_app.py               builds 資料夾同步備份.app (runs app.py with this folder's .venv)
syncbackup/config.py       settings, performance modes
syncbackup/engine.py       sync engine (scan, compare, copy, version history)
syncbackup/service.py      background service (live monitoring, scheduling, pausing, drive detection)
syncbackup/throttle.py     speed limit, CPU cap, speed measurement
syncbackup/history.py      SQLite activity log
syncbackup/sysutil.py      macOS: QoS, power, notifications, launch at login, Dock
syncbackup/gui/            interface
```

## License

Released under the [MIT License](LICENSE). You're free to use, modify and distribute it; please keep the copyright and license notice.
