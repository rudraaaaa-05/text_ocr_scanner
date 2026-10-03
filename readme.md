# OCR Scanner

Read text from your screen with free (and paid) AI APIs and append it to a plain text file, one line per line of text.
Built as a project for fun.

## Features

- **Full-screen recording** with Start / Pause / Stop
- **Region recording**: pick a region once, then record only that
- **Hotkey scan**: press a key, drag a region, text is read once. Repeat for each scan
- **AI OCR** Uses AI tools to read the text on the screen

## Platform support

| Platform | Status |
|---|---|
| Linux, Hyprland / sway / other wlroots Wayland | Primary target (uses `grim` + `slurp`) |
| Linux, X11 | Supported (built-in region selector and global hotkeys) |
| Linux, GNOME / KDE on Wayland | Not supported (use an X11 session) |
| Windows, macOS | Written for, but not tested |

## Install

Requires Python 3.9+.

```bash
# Arch / Hyprland
sudo pacman -S grim slurp

git clone https://github.com/<you>/ocr-scanner
cd ocr-scanner
./run.sh          # Windows: run.bat
```

`run.sh` creates a private virtualenv, installs `requirements.txt`, and launches the app.

## Setup

1. Open the **AI Keys** tab, click **Get free key** for a provider, paste the key, press **Test**.
2. Pick that provider as active (the radio button).
3. Open **Settings** and choose where the text file is saved.
4. Go to **Capture** and press Start.

Several keys in one provider (comma separated) are used in turn, which stretches free-tier rate limits.

## Hotkeys

On X11, Windows and macOS the hotkeys work inside the app. Wayland apps cannot grab global keys, so on Hyprland you bind them in your compositor. The **Settings** tab generates the exact lines for your configured keys. Example (Hyprland Lua):

```lua
hl.bind("CTRL + ALT + O", hl.dsp.exec_cmd("/path/to/.venv/bin/python /path/to/ocr_scanner.py --trigger region"))
```

hyprlang:

```
bind = CTRL ALT, O, exec, /path/to/.venv/bin/python /path/to/ocr_scanner.py --trigger region
```

Available triggers: `region`, `toggle`, `start`, `stop`, `pause`, `show`. The app must already be running.

## Privacy

Screenshots are sent to the provider you choose. Use Ollama to keep everything on your machine. Settings, including API keys, are stored in plain JSON (mode 600) at `~/.config/ocr-scanner/config.json` (`%APPDATA%\OCRScanner` on Windows, `~/Library/Application Support/OCRScanner` on macOS).

## Tips

- Free tiers allow roughly 10-30 requests per minute. Keep the delay at 3000 ms or more.
- Region mode is more accurate and cheaper than full screen for small text.
- Set the language hint in Settings
- On tiling window managers, turn off "Hide this window while selecting a region".

## Troubleshooting

| Problem | Fix |
|---|---|
| `grim` / `slurp` not found | Install them (`sudo pacman -S grim slurp`) |
| Hotkey does nothing on Wayland | Add the compositor binds from Settings and make sure the app is running |
| HTTP 404 on a model | Model was renamed or retired. Use **Fetch models** |
| HTTP 429 | Rate limit. Raise the delay or add more keys |
| Qt fails to start on X11 | Install `libxcb-cursor0` (Ubuntu) or `xcb-util-cursor` (Arch) |
| macOS hotkeys silent | Grant Screen Recording and Accessibility permission |











## Roadmap

The goal is to grow this from a script into a proper installable app that runs quietly in the background and works fully on Windows, macOS and Linux. Nothing below is done yet. It is ordered roughly by priority.

### 1. Background service
The GUI, capture loop, hotkeys and command channel all live in one process. The plan is to split them:

- **Core daemon**: capture, OCR, file writing, hotkeys and the command channel, with no GUI dependency.
- **GUI**: a thin client that talks to the daemon. Closing the window no longer stops recording.
- **Autostart per platform**: a systemd user service on Linux, a LaunchAgent on macOS, and a startup entry or scheduled task on Windows.
- **System tray**: start/stop, scan region, open settings. The existing `--trigger` command channel is the starting point for this IPC.

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

| Gap | Plan |
|---|---|
| GNOME / KDE on Wayland has no capture | Use the xdg-desktop-portal Screenshot / ScreenCast APIs |
| Wayland global hotkeys need manual compositor binds | Use the GlobalShortcuts portal where the compositor supports it (verify support per compositor) |
| Windows and macOS are barely tested | Test on real machines: multi-monitor, mixed DPI scaling, Retina |
| macOS permissions | Guide users through Screen Recording and Accessibility grants on first run |
| API keys are stored in plain JSON | Move to the OS keyring (Credential Manager, Keychain, Secret Service) |

### 4. Quality and releases
- GitHub Actions CI on Linux, Windows and macOS with headless tests, covering the OCR pipeline, config handling, hotkey parsing and IPC.
- Tagged releases that build and attach the installers automatically.
- In-app update check (notify only; no silent updates).

### 5. Ideas (not committed)
- Language-learning extras: word lookup, furigana, Anki export.
- More providers and local OCR engines as a no-network option.
- Per-profile settings (for example one for games, one for manga).

