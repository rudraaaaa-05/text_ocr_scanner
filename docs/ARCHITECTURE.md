# Architecture 

OCR Scanner is two processes that talk over local IPC.

```
                 +--------------------------------------+
 hotkeys (pynput)|  ocr_daemon.py  (no PySide6)         |
 --trigger  ---->|  Daemon: commands, event buffer      |----> session output file
 systemd / Run   |  Recorder, ScanService, TextSink     |----> screen (mss / grim+slurp)
                 |  HotkeyManager, Capturer, OCRClient  |----> OCR providers
                 +------------------^-------------------+
                                    | Unix socket (Linux) / loopback TCP + token (Windows)
                 +------------------+-------------------+
                 |  ocr_scanner.py  (PySide6 GUI)       |
                 |  settings, results, log, tray,       |
                 |  region selector overlay             |
                 +--------------------------------------+
```

## Modules

| File | Imports Qt | Role |
|---|---|---|
| `ocr_core.py` | no | Everything that used to be Qt-free in the old single file: config, `Capturer`, region validation, OCR providers, `LocalOCR`, `OCRClient`, `TextSink`, `ScanService`, `HotkeyManager`, `Recorder`, `SessionOutput`, hotkey helpers. Moved, not rewritten |
| `ocr_ipc.py` | no | Protocol, `IPCServer`, client (`request`, `send_command`), `InstanceLock`, `cli_trigger`, process spawning |
| `ocr_daemon.py` | no | `Daemon` (command handlers, event buffer, watchdog), CLI: `run/start/stop/status/autostart/service` |
| `ocr_autostart.py` | no | systemd user unit (Linux), `HKCU\...\Run` value (Windows) |
| `ocr_client.py` | no | `DaemonLink`: polls the daemon for events from a background thread |
| `ocr_scanner.py` | yes | The GUI. Re-exports `ocr_core` so old imports keep working. Handles `--trigger` *before* importing Qt |

## Who owns what

| Concern | Owner |
|---|---|
| Screen capture, OCR, recording loop, one-shot scans | daemon |
| Output file (one per daemon start, exclusive create, `_2` suffix on clash) | daemon |
| Global hotkeys (pynput) and the hotkey watchdog | daemon |
| Wayland region selection (`slurp`) | daemon |
| X11 / Windows region selection overlay | GUI (a Qt overlay); the daemon asks for it |
| Settings file (`config.json`, API keys) | **GUI is the only writer**; the daemon only reads it (`reload_config`) |
| Tray icon, notifications while the GUI runs | GUI. With no GUI connected the daemon uses `notify-send` on Linux |

The daemon never writes `config.json`, so there are no two-writer races, and credentials never cross IPC.

## IPC

One request and one reply per connection, one UTF-8 line each, max 64 KiB, 5 s server timeout, at most 16 simultaneous connections.

```
request  {"cmd": "status", "args": {}, "token": "<windows only>"}
reply    {"ok": true, "data": {...}}
         {"ok": false, "error": {"code": "region_invalid", "message": "..."}}
```

Error codes: `bad_request`, `unknown_command`, `request_too_large`, `unauthorized`, `bad_argument`, `busy`, `region_invalid`, `capture_unavailable`, `ocr_unavailable`, `gui_unavailable`, `internal`.

| Command | Meaning |
|---|---|
| `start`, `stop`, `toggle` | recording (start is idempotent: never a second loop) |
| `pause` | toggles pause/resume (as the hotkey always did); `resume` only resumes |
| `current` | scan the stored region / configured screen once (duplicate filter on) |
| `region` | pick a **new** region and scan it once (not stored) |
| `scan_region` | GUI → daemon: the region the user picked (`null` = cancelled) |
| `select_ack` | GUI → daemon: "I took the selection request" |
| `show` | bring the GUI forward; starts one (`--hidden`) if none is connected |
| `status`, `ping`, `events` | state, liveness, event polling |
| `reload_config` | re-read `config.json`, re-register hotkeys |
| `shutdown` | graceful exit |

The old plain-text form (`region\n`) is still accepted on Unix and answered with `ok` / `error: ...`.

**Security.** Linux: socket in `$XDG_RUNTIME_DIR` (per-user, 0700), created 0600, peer uid checked with `SO_PEERCRED`. Windows: loopback TCP on an ephemeral port; port and a random 192-bit token are written to `%APPDATA%\OCRScanner\ipc-endpoint.json` (per-user profile ACLs) and every request must carry the token. Nothing listens on a non-loopback interface. The Windows transport is therefore not reachable by raw text commands without the token; `--trigger` reads the token itself.

## GUI ⇄ daemon

The GUI polls `events` every 0.5 s from a background thread. A reply holds the new events since a cursor, plus state, counters, session file, hotkey status and any pending selection request. The first batch after connecting is flagged *replay* (history for the log; no notifications, no window-raise). If the daemon restarts, its event counter restarts; the GUI notices and resyncs. The daemon treats a GUI as connected if it polled within 4 s.

Region selection flow (`region` command, X11/Windows): daemon creates a pending request (20 s TTL, 45 s if it had to start a GUI) → GUI sees it in `events`, sends `select_ack`, shows the overlay → GUI sends `scan_region` with the chosen region (or `null`) → daemon captures that region and scans it with `force=True`.

## Process lifecycle

* **Single instance**: an OS file lock (`flock` / `msvcrt.locking`) in the config dir, released automatically if the process dies. The GUI has its own lock; a second GUI asks the daemon to `show` the first.
* **Start**: the GUI starts the daemon if it does not answer (`start_daemon`). A stale socket from a crash is detected (connect fails) and replaced.
* **Stop**: `SIGTERM`/`SIGINT` (Linux), `SIGBREAK`/Ctrl+C or the `shutdown` command (both). Recorder stopped, hotkeys joined, slurp cancelled, endpoint removed, lock released. Idempotent.
* **Failure handling**: recorder gives up after 5 consecutive failures (state → idle, notification); a dead hotkey listener is detected every 5 s and reported; a failing command never kills the daemon; systemd restarts it on failure.
* **Closing the GUI hides it in the tray without touching the daemon.** The tray has *Open Window*, *Open Output Folder*, and *Stop Daemon*.

## Autostart

* Linux: `~/.config/systemd/user/ocr-scanner.service` runs the daemon and `ocr-scanner-gui.service` supervises the tray GUI independently (`Restart=always`). Both are enabled for `hyprland-session.target`; the Hyprland startup hook restarts them on every compositor login, including after a manual stop in an earlier session. `enable` also imports the session variables (`DISPLAY`, `WAYLAND_DISPLAY`, ...) into the user manager so grim/slurp/mss/pynput work. No root.
* Windows: value `OCRScanner` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, using `pythonw.exe` so no console appears. No admin rights.
