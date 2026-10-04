# OCR Scanner

Read text from your screen with **offline local OCR** or free (and paid) AI APIs, and append it to a plain text file, one line per line of text.

## Features

- **Full-screen recording** with Start / Pause / Stop
- **Region recording**: pick a region once, then record only that
- **Hotkey scan**: press a key, drag a region, text is read once. Repeat for each scan
- **Local OCR (offline)**: reads text on your computer. No API key, no upload, no rate limits. It scores every line and drops anything it is unsure of instead of guessing
- **AI OCR**: optional cloud providers (Gemini, Groq, OpenRouter, Mistral, DeepSeek, Ollama or any OpenAI-compatible endpoint). Local OCR can serve as the automatic fallback when one fails

## Platform support

| Platform | Status |
|---|---|
| Linux, Hyprland / sway / other wlroots Wayland | Primary target (uses `grim` + `slurp`) |
| Linux, X11 | Supported (built-in region selector and global hotkeys) |
| Linux, GNOME / KDE on Wayland | Not supported (use an X11 session) |
| Windows, macOS | Written for, but not tested |

## Install

Requires Python 3.9+ for the cloud providers. **Local OCR needs Python 3.11+** (a requirement of ONNX Runtime) and was tested on 3.12.

```bash
# Arch / Hyprland
sudo pacman -S grim slurp

git clone https://github.com/rudraaaaa-05/text_ocr_scanner
cd text_ocr_scanner
./run.sh          # Windows: run.bat
```

`run.sh` creates a private virtualenv, installs `requirements.txt`, and launches the app. `requirements.txt` includes `rapidocr` and `onnxruntime`, which power Local OCR. The default model ships inside the `rapidocr` package, so no extra download is needed.

## Setup

### Option A: Local OCR (no key, recommended to start)

1. Open the **AI Keys** tab and select **Local OCR (offline)** with its radio button. It is the default on a fresh install.
2. Press **Test**. The first press loads the engine, then it should report `Works. Read: OCR TEST 123`.
3. Open **Settings** and set the **Language of the text** (Japanese, Chinese, English or a Latin-script language).
4. Open **Settings** and choose where the text file is saved.
5. Go to **Capture** and press Start, or use the region hotkey.

### Option B: Cloud AI

1. Open the **AI Keys** tab, click **Get free key** for a provider, paste the key, press **Test**.
2. Pick that provider as active (the radio button).
3. Continue with steps 3 to 5 above.

Several keys in one provider (comma separated) are used in turn, which stretches free-tier rate limits.

You can combine both. Leave **Use as fallback** ticked on Local OCR and it takes over automatically whenever the active cloud provider fails or hits a rate limit.

## Using Local OCR

Local OCR finds each line of text in the image, reads it, and gives it a confidence score. Lines below your minimum confidence are discarded, so a blurry icon or texture produces a gap rather than invented text. Tune it on the **AI Keys** tab, in the Local OCR card:

| Setting | What it does | When to change it |
|---|---|---|
| **Model** | **Small** is fast and included with the install. **Medium** is more accurate but slower, and downloads once on first use | Try Medium if Small misreads your font |
| **Minimum confidence** (default 70%) | Lines scoring lower are dropped | Raise it if junk characters get through. Lower it if real text goes missing |
| **Largest image side** (default 1280 px) | The capture is shrunk to this size before reading | Raise it (for example 1920) if small text is missed. Lower it for faster full-screen scans |

What it covers:

- **Languages:** Japanese, Chinese (Simplified and Traditional), English, and Latin-script languages such as German, French, Spanish, Portuguese, Italian and Vietnamese. **Korean, Russian, Arabic, Hindi and Thai are not covered.** Use a cloud provider for those.
- **Extra instructions** in Settings apply to cloud providers only. Local OCR reads exactly what is printed.
- **Furigana** is read as its own line above the main line.
- **Vertical Japanese** is only lightly tested, and right-to-left column order is not handled.
- The engine loads in the background at startup, so the first scan is not the slow one.

## Hotkeys

On X11, Windows and macOS the hotkeys work inside the app. Wayland apps cannot grab global keys, so on Hyprland you bind them in your compositor.
Example (Hyprland Lua):

```lua
hl.bind("CTRL + ALT + O", hl.dsp.exec_cmd("/path/to/.venv/bin/python /path/to/ocr_scanner.py --trigger region"))
```

hyprlang:

```
bind = CTRL ALT, O, exec, /path/to/.venv/bin/python /path/to/ocr_scanner.py --trigger region
```

Available triggers: `region`, `toggle`, `start`, `stop`, `pause`, `show`. The app must already be running.

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
| `grim` / `slurp` not found | Install them (`sudo pacman -S grim slurp`) |
| Hotkey does nothing on Wayland | Add the compositor binds from Settings and make sure the app is running |
| HTTP 404 on a model | Model was renamed or retired. Use **Fetch models** |
| HTTP 429 | Rate limit. Raise the delay, add more keys, or switch to Local OCR |
| "Local OCR is not installed" | Run `pip install rapidocr onnxruntime` in the app's virtualenv. Needs Python 3.11+ |
| Local OCR: junk characters in the file | Raise **Minimum confidence** |
| Local OCR: real text missing | Lower **Minimum confidence**, or raise **Largest image side**. Try the **Medium** model |
| "Local OCR does not cover Korean" (or Russian, Arabic, Hindi, Thai) | Not supported locally. Switch to a cloud provider |
| Medium model fails to load | It downloads on first use and needs internet. Switch back to Small, which works offline |
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
- More providers.
- Per-profile settings (for example one for games, one for manga).