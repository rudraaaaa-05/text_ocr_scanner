# OCR Scanner

Read text from your screen with **offline local OCR** or free (and paid) AI APIs.

## Features

- **Full-screen recording** with Start / Pause / Stop
- **Region recording**: pick a region once, then record only that
- **Select a new region and scan**: press a key, drag a region, text is read once. Repeat for each scan
- **Scan current**: press a key and the *already configured* target is read once, with no selector: the stored region in region mode, the configured monitor (or all monitors) in full-screen mode
- **Background daemon**: recording, hotkeys and OCR keep running when the window is closed; autostart via a systemd user service (Linux) or a startup entry (Windows)
- **Local OCR (offline)**: reads text on your computer. No API key, no upload, no rate limits. It scores every line and drops anything it is unsure of instead of guessing
- **AI OCR**: optional cloud providers (Gemini, Groq, OpenRouter, Mistral, DeepSeek, Ollama or any OpenAI-compatible endpoint). Local OCR can serve as the automatic fallback when one fails

## Platform support

Verified compatibility per distribution, desktop and Windows is tracked separately in [COMPATIBILITY.md](docs/COMPATIBILITY.md).

## Install

Requires Python 3.9+ for the cloud providers. **Local OCR needs Python 3.11+** (a requirement of ONNX Runtime) and was tested on 3.12.

```bash
# Arch / Hyprland
sudo pacman -S grim slurp

git clone https://github.com/rudraaaaa-05/text_ocr_scanner
cd text_ocr_scanner
./run.sh          # Windows: run.bat
```

`run.sh` creates a private virtualenv, installs `requirements.txt`, and launches the app. `requirements.txt` includes `pynput` (global hotkeys on Windows and X11) and the local OCR packages. The default model ships inside the `rapidocr` package, so no extra download is needed.

## Setup

### Option A: Local OCR (no key, recommended to start)

1. Open the **AI Keys** tab and select **Local OCR (offline)** with its radio button. It is the default on a fresh install.
2. Download the Local OCR ```pip install rapidocr onnxruntime```. Restart
3. Press **Test**. The first press loads the engine, then it should report `Works. Read: OCR TEST 123`.
4. Open **Settings** and set the **Language of the text** (Japanese, Chinese, English or a Latin-script language).
5. Open **Settings** and choose the output file name (a timestamp is added per launch, see [Output files](#output-files)).
6. Go to **Capture** and press Start, or use one of the scan hotkeys.

### Option B: Cloud AI

1. Open the **AI Keys** tab, click **Get free key** for a provider, paste the key, press **Test**.
2. Pick that provider as active (the radio button).
3. Continue with steps 3 to 5 above.

Several keys in one provider (comma separated) are used in turn, which stretches free-tier rate limits.

You can combine both. Leave **Use as fallback** ticked on Local OCR and it takes over automatically whenever the active cloud provider fails or hits a rate limit.

## Using Local OCR

Local OCR finds each line of text in the image, reads it, and gives it a confidence score. Lines below your minimum confidence are discarded, so a blurry icon or texture produces a gap rather than invented text. Tune it on the **AI Keys** tab, in the Local OCR card:

| Setting                                  | What it does                                                                                                             | When to change it                                                                          |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| **Model**                                | **Small**: fast and included with the install. <br>**Medium**: more accurate but slower, and downloads once on first use | Try Medium if Small misreads your font                                                     |
| **Minimum confidence** (default 70%)     | Lines scoring lower are dropped                                                                                          | Raise it if junk characters get through. Lower it if real text goes missing                |
| **Largest image side** (default 1280 px) | The capture is shrunk to this size before reading                                                                        | Raise it (for example 1920) if small text is missed. Lower it for faster full-screen scans |

What it covers:

- **Languages:** Japanese, Chinese (Simplified and Traditional), English, and Latin-script languages such as German, French, Spanish, Portuguese, Italian and Vietnamese. **Korean, Russian, Arabic, Hindi and Thai are not covered.** Use a cloud provider for those.
- **Extra instructions** in Settings apply to cloud providers only. Local OCR reads exactly what is printed.

## Background daemon

Capture, OCR, recording, hotkeys and the output file run in a **background daemon** (`ocr_daemon.py`, no PySide6 needed). The window (`ocr_scanner.py`) is a client of it. **Closing the window does not stop recording.** Opening the window again reconnects to the running daemon; if none is running, the window starts one.

Linux (from the project folder; `run_daemon.sh` uses the same virtualenv as `run.sh`):

```bash
./run_daemon.sh start              # start daemon and tray GUI in the background (no-op if running)
./run_daemon.sh status             # state, scans, output file, hotkeys, GUI connected?
./run_daemon.sh stop               # graceful shutdown
./run_daemon.sh run                # foreground, for debugging
./run_daemon.sh autostart enable   # install + enable + start daemon and tray GUI at login (no root)
./run_daemon.sh autostart disable
./run_daemon.sh autostart status
./run_daemon.sh service restart    # systemctl --user start|stop|restart|status
journalctl --user -u ocr-scanner.service -f   # daemon log; GUI service: ocr-scanner-gui.service
```

Windows: `run_daemon.bat start | stop | status | autostart enable | autostart disable | autostart status`. Autostart writes a per-user `Run` registry value (no administrator needed).

Run `autostart enable` from the same virtualenv you use for the app: the unit / registry value records that Python.

Tray menu (when a tray is available): **Open Window**, **Open Output Folder**, and **Stop Daemon**. Closing the window hides it in the tray; **Stop Daemon** closes both the GUI and daemon. GNOME needs an AppIndicator/System Tray extension, and KDE needs a System Tray widget in the panel. If no tray host is available, the app keeps the window visible instead of hiding an inaccessible GUI.

## Output files

Every time the **daemon** starts it creates a **new** output file and uses it for everything it reads until it exits. Opening and closing the window does not change the file. The name comes from the *Output file* path in Settings plus the launch time:

```
ocr_output.txt   ->   ocr_output_2026-01-31_14-05-09.txt
```

- Files from earlier launches are never opened for writing, so they are never overwritten or changed. The new file is created exclusively; if the name is taken (two launches in the same second) a `_2`, `_3`, ... suffix is added.
- The path you set is only the base name. Changing it applies the next time the **daemon** starts; the running session keeps its file. The active file is shown in Settings.
- **Open Output File** and **Open Output Folder** (Settings and the tray menu) always refer to the active session file.
- Duplicate filtering ("skip identical text", "only save new lines") works for the daemon's whole run, including across Start/Stop and scans.

## Capture modes

- **Full screen**: reads the monitor chosen on the Capture page, or all monitors.
- **Region**: reads only the stored region. A region is never silently replaced by a full-screen capture; if it is missing or invalid you get an error and no OCR runs.

A stored region is rejected when it is empty, has zero or negative size, lies outside the current desktop, or (X11 / Windows) was selected on a different monitor layout than the current one, because the same coordinates could then point at different content. Select the region again to fix it. Monitors may sit at negative coordinates and have different display scaling; the selection is mapped to physical screenshot pixels.

Pressing **Escape** or right-click cancels a selection. A cancelled or too-small (< 8 px) selection never runs OCR.

## Hotkeys

There are two different scan actions:

| Action | Default key | `--trigger` | What it does |
|---|---|---|---|
| **Select a new region and scan** | `Ctrl+Alt+O` | `region` | Opens the selector, reads the area you drag once. The region is **not** stored |
| **Scan current** | `Ctrl+Alt+S` | `current` | No selector. Region mode: reads the **stored** region. Full-screen mode: reads the configured monitor / all monitors. Works while recording is stopped and the window is unfocused or hidden. Duplicate filtering applies |
| Start / stop recording | `Ctrl+Alt+R` | `toggle` | |
| Pause / resume | `Ctrl+Alt+P` | `pause` | |

The stored region is set with **Select region** on the Capture page (or when you start region recording without one). If region mode has no valid stored region, *Scan current* shows an error and does nothing else.

Other triggers: `start`, `stop`, `show`, plus `resume` (resume only; `pause` toggles) and `status` / `shutdown`. All of them: `region`, `current`, `toggle`, `start`, `stop`, `pause`, `show`, `resume`, `status`, `shutdown`. The **daemon** must be running (`python ocr_daemon.py start`, or open the window once). `ocr_scanner.py --trigger ...` is unchanged and does not need Qt. If region mode has no valid stored region, `start` / `toggle` report an error instead of opening a selector (use *Select region* in the window first).

### Windows, X11

Global hotkeys run inside the daemon through `pynput` and can be changed in Settings. If registration fails (pynput missing, no X display, a key assigned twice, etc) the reason is shown in Settings, in the activity log and as a notification; nothing fails silently. Hotkeys keep working with the window closed and are released when the daemon exits. Two actions cannot share one key.

### Wayland

Wayland apps cannot grab global keys themselves. On desktops whose XDG portal offers `GlobalShortcuts` (for example KDE Plasma and recent GNOME) the daemon registers the keys through the portal; the desktop may show a confirmation dialog the first time. Hyprland keeps compositor binds by default. Where the portal is not available, you bind keys in your desktop to call `--trigger`, and Settings shows ready-made instructions for your desktop. `wayland_hotkeys` in `config.json` can be `auto` (default), `portal` or `manual`. What has and has not been verified on real systems is listed in [COMPATIBILITY.md](COMPATIBILITY.md). Example (Hyprland Lua):

```lua
hl.bind("CTRL + ALT + O", hl.dsp.exec_cmd("/path/to/.venv/bin/python /path/to/ocr_scanner.py --trigger region"))
hl.bind("CTRL + ALT + S", hl.dsp.exec_cmd("/path/to/.venv/bin/python /path/to/ocr_scanner.py --trigger current"))
```

## Privacy

**Local OCR never leaves your computer.** If a cloud provider is active, or Local OCR falls back to one, screenshots are sent to that provider. Ollama is another way to keep everything on your machine. Settings, including API keys, are stored in plain JSON (mode 600) at `~/.config/ocr-scanner/config.json` (`%APPDATA%\OCRScanner` on Windows, `~/Library/Application Support/OCRScanner` on macOS).

## Tips

- Free cloud tiers allow roughly 10-30 requests per minute. Keep the delay at 3000 ms or more. Local OCR has no such limit, so you can use a shorter delay.
- Region mode is more accurate and faster than full screen for small text, and much faster with Local OCR.
- Set the language hint in Settings. Local OCR uses it to decide whether it can read the text at all.
- On tiling window managers, turn off "Hide this window while selecting a region".

## Troubleshooting

| Problem | Fix |
|---|---|
| `grim` / `slurp` not found | Install them (`sudo pacman -S grim slurp`). On GNOME / KDE they are not used |
| `compositor doesn't support zwlr_layer_shell_v1` or `compositor doesn't support the screen capture protocol` | The compositor (GNOME, KDE, ...) cannot run `grim` / `slurp`. Update to this version, which uses the screenshot portal there instead. Check `./run_daemon.sh status` for the backend in use, and install `xdg-desktop-portal-gnome` / `-kde` if capture then reports a missing portal |
| Portal capture: "refused the screenshot" | Allow this app to take screenshots in the desktop's permission prompt or privacy settings, then try again |
| Hotkey does nothing on Wayland | Add the compositor binds from Settings and make sure the daemon is running (`./run_daemon.sh status`) |
| `--trigger` says the daemon is not running | `./run_daemon.sh start`, or open the window once |
| Daemon works from a terminal but not from systemd | The service needs your session's `DISPLAY` / `WAYLAND_DISPLAY`. `autostart enable` imports them; re-run it from inside your desktop session. On Hyprland, the autostart setup also restarts both OCR services from the compositor startup hook so they come back after a manual stop on the previous login |
| Hotkeys fail on Windows / X11 | Read the message in Settings. Install `pynput` (`pip install pynput`) and check that no two actions share a key |
| "Scan current" says the region is not usable | The stored region is missing, empty, off-screen, or from another monitor layout. Select it again on the Capture page |
| HTTP 404 on a model | Model was renamed or retired. Use **Fetch models** |
| HTTP 429 | Rate limit. Raise the delay, add more keys, or switch to Local OCR |
| "Local OCR is not installed" | Run `pip install rapidocr onnxruntime` in the app's virtualenv. Needs Python 3.11+ |
| Local OCR: junk characters in the file | Raise **Minimum confidence** |
| Local OCR: real text missing | Lower **Minimum confidence**, or raise **Largest image side**. Try the **Medium** model |
| "Local OCR does not cover Korean" (or Russian, Arabic, Hindi, Thai) | Not supported locally. Switch to a cloud provider |
| Medium model fails to load | It downloads on first use and needs internet. Switch back to Small, which works offline |
| Qt fails to start on X11 | Install `libxcb-cursor0` (Ubuntu) or `xcb-util-cursor` (Arch) |

## Roadmap

The goal is to grow this from a script into a proper installable app that runs quietly in the background and works fully on Windows and Linux. 

### 1. Background service
- [x] Create Core Daemon and connect GUI
- [x] GUI in Tray
- [x] systemd user service and autostart

### 2. Packaging
Goal: install it without touching Python or a terminal.

| Platform | Target |
|---|---|
| Windows | Installer (`.exe` / MSI) |
| macOS | Signed and notarized `.app` / `.dmg` |
| Linux | AppImage first, then an AUR package and `.deb` |

Notes:
- Candidate tooling: PyInstaller, Briefcase or Nuitka. Pick one and test the bundle size and startup time.
- Unsigned Windows builds trigger SmartScreen warnings, and macOS requires an Apple Developer account ($99/year) to notarize. Budget for both.
- Flatpak is attractive but its sandbox conflicts with `grim`, `hyprctl` and global hotkeys. Evaluate it later, not first.

### 3. Full cross-platform compatibility

See [COMPATIBILITY.md](docs/COMPATIBILITY.md) 
### 4. Quality and releases
- [ ] GitHub Actions CI on Linux, Windows with headless tests, covering the OCR pipeline, config handling, hotkey parsing and IPC.
- [ ] Tagged releases that build and attach the installers automatically.
- [ ] In-app update check (notify only; no silent updates).

### 5. Ideas (not committed)
- Language-learning extras: word lookup, furigana, Anki export.
- Per-profile settings (for example one for games, one for manga).