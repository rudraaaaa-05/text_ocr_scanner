from __future__ import annotations

import base64
import html
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

APP_NAME = "OCR Scanner"
APP_ID = "ocr-scanner"
VERSION = "1.0"
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
SCRIPT_PATH = os.path.abspath(__file__)
VALID_COMMANDS = ("region", "toggle", "start", "stop", "pause", "show")


#platform detection helpers
#made this using hyprland arch linux so i have no idea if it works on other distros or non-hyprland
#でも、きっと大丈夫だと思う

def is_wayland() -> bool:
    return IS_LINUX and (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def is_hyprland() -> bool:
    return IS_LINUX and (
        bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
        or "hyprland" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    )


def config_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "OCRScanner"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "OCRScanner"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / APP_ID


IPC_PORT = 47653


def ipc_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    folder = Path(runtime) if runtime and os.path.isdir(runtime) else config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / f"{APP_ID}.sock")


def _ipc_connect(timeout: float) -> socket.socket:
    if IS_WINDOWS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("127.0.0.1", IPC_PORT))
    else:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(ipc_path())
    return s


def send_command(cmd: str, timeout: float = 1.5):
    """Send a command to the running app. Returns the reply, or None if it isn't running."""
    try:
        s = _ipc_connect(timeout)
    except OSError:
        return None
    try:
        with s:
            s.sendall((cmd + "\n").encode("utf-8"))
            data = s.recv(64)
        return data.decode("utf-8", "ignore").strip() or "ok"
    except OSError:
        return None


class IPCServer(threading.Thread):
    def __init__(self, handler):
        super().__init__(daemon=True)
        self.handler = handler
        self.sock = None
        self.path = None

    def bind(self) -> bool:
        try:
            if IS_WINDOWS:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", IPC_PORT))
            else:
                self.path = ipc_path()
                if os.path.exists(self.path):
                    if send_command("ping") is not None:
                        return False  # a live instance owns it
                    os.unlink(self.path)
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.bind(self.path)
                os.chmod(self.path, 0o600)
            s.listen(8)
            self.sock = s
            return True
        except OSError:
            return False

    def run(self):
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        with conn:
            conn.settimeout(2)
            try:
                data = conn.recv(256).decode("utf-8", "ignore").strip()
            except OSError:
                return
            cmd = data.split()[0] if data else ""
            if cmd in VALID_COMMANDS:
                self.handler(cmd)
            try:
                conn.sendall(b"ok\n")
            except OSError:
                pass

    def close(self):
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        if self.path and os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def cli_trigger(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in VALID_COMMANDS:
        sys.stderr.write(f"usage: {os.path.basename(SCRIPT_PATH)} --trigger {{{'|'.join(VALID_COMMANDS)}}}\n")
        return 2
    if send_command(cmd) is None:
        sys.stderr.write(f"{APP_NAME} is not running. Start it first.\n")
        return 1
    return 0


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--trigger":
    sys.exit(cli_trigger(sys.argv[1:]))

#third party imports

if is_wayland() and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"

try:
    import requests
    from PIL import Image, ImageChops, ImageDraw, ImageStat
    from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import (
        QAction, QColor, QDesktopServices, QGuiApplication, QIcon, QImage,
        QKeySequence, QPainter, QPen, QPixmap,
    )
    from PySide6.QtWidgets import (
        QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
        QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QMainWindow,
        QMenu, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox,
        QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
    )
except ImportError as _exc:  # pragma: no cover
    sys.stderr.write(
        f"Missing dependency: {getattr(_exc, 'name', None) or _exc}\n"
        "Install everything with:  pip install -r requirements.txt   (or just use run.sh / run.bat)\n"
    )
    sys.exit(1)


#ai models and providers
#only deepseek  is paid rest are free but you need to get api keys for them
#also as of making this gemini revoked its free api key so you need to get a paid key for it now?????? but i won't remove it


PROVIDERS = [
    dict(id="gemini", name="Google AI Studio (Gemini)", kind="gemini", free="Free tier", needs_key=True,
         base_url="https://generativelanguage.googleapis.com/v1beta", edit_base=False,
         key_url="https://aistudio.google.com/apikey", key_label="Get free key",
         model="gemini-flash-latest", fallback=True,
         note="Generous free tier, rate limited. On the free tier Google may use what you send to improve its models."),
    dict(id="groq", name="Groq", kind="openai", free="Free tier", needs_key=True,
         base_url="https://api.groq.com/openai/v1", edit_base=False,
         key_url="https://console.groq.com/keys", key_label="Get free key",
         model="meta-llama/llama-4-scout-17b-16e-instruct", fallback=True,
         note="Very fast. The free tier has request and token limits per minute and per day."),
    dict(id="openrouter", name="OpenRouter (free models)", kind="openai", free="Free models", needs_key=True,
         base_url="https://openrouter.ai/api/v1", edit_base=False,
         key_url="https://openrouter.ai/keys", key_label="Get free key",
         model="google/gemma-3-27b-it:free", fallback=True,
         note="The free model lineup changes often. Use 'Fetch models' to list the free vision models available today."),
    dict(id="mistral", name="Mistral", kind="openai", free="Free tier", needs_key=True,
         base_url="https://api.mistral.ai/v1", edit_base=False,
         key_url="https://console.mistral.ai/api-keys", key_label="Get free key",
         model="mistral-small-latest", fallback=True,
         note="Vision-capable models via the standard chat endpoint. Free tier limits apply."),
    dict(id="deepseek", name="DeepSeek (vision)", kind="openai", free="Paid", needs_key=True,
         base_url="https://api.deepseek.com", edit_base=False,
         key_url="https://platform.deepseek.com/api_keys", key_label="Get API key",
         model="deepseek-v4-flash-vision-exp", fallback=False,
         note="Not free. Only the vision-exp model accepts images; the other DeepSeek models reject them with an error."),     
    dict(id="ollama", name="Ollama (local, no key)", kind="openai", free="Local", needs_key=False,
         base_url="http://localhost:11434/v1", edit_base=True,
         key_url="https://ollama.com/download", key_label="Get Ollama",
         model="qwen2.5vl:7b", fallback=False,
         note="Runs on your own machine: free and private. Pull a vision model first, e.g.  ollama pull qwen2.5vl:7b"),
    dict(id="custom", name="Custom OpenAI-compatible", kind="openai", free="", needs_key=False,
         base_url="", edit_base=True, key_url="", key_label="",
         model="", fallback=False,
         note="Any OpenAI-style /chat/completions endpoint that accepts images."),
]
PROVIDER_BY_ID = {p["id"]: p for p in PROVIDERS}

HOTKEY_DEFAULTS = {"region": "Ctrl+Alt+O", "toggle": "Ctrl+Alt+R", "pause": "Ctrl+Alt+P"}
HOTKEY_LABELS = {
    "region": "Scan a region once",
    "toggle": "Start / stop recording",
    "pause": "Pause / resume",
}
LANGUAGES = [
    "Auto-detect", "Japanese", "English", "Chinese (Simplified)", "Chinese (Traditional)",
    "Korean", "Russian", "German", "French", "Spanish", "Portuguese", "Italian",
    "Arabic", "Hindi", "Thai", "Vietnamese",
]

#output stuff

def default_config() -> dict:
    return {
        "mode": "full",
        "monitor": "auto",
        "region": None,
        "output_file": str(Path.home() / "ocr_output.txt"),
        "timestamps": False,
        "skip_duplicates": True,
        "only_new_lines": False,
        "interval_ms": 3000,
        "skip_unchanged": True,
        "hide_on_select": not is_wayland(),
        "max_side": 2560,
        "language": "Auto-detect",
        "extra_prompt": "",
        "hotkeys": dict(HOTKEY_DEFAULTS),
        "active_provider": "gemini",
        "fallback": True,
        "providers": {
            p["id"]: {"keys": "", "model": p["model"], "base_url": p["base_url"], "fallback": p["fallback"]}
            for p in PROVIDERS
        },
        "tray_on_close": False,
        "notifications": True,
        "hypr_syntax": "auto",
    }


def _merge(base: dict, over: dict) -> None:
    for key, val in over.items():
        if key not in base:
            continue
        cur = base[key]
        if isinstance(cur, dict) and isinstance(val, dict):
            _merge(cur, val)
        elif cur is None or isinstance(val, type(cur)) and not (isinstance(cur, bool) != isinstance(val, bool)):
            base[key] = val


class Config:
    def __init__(self):
        self.path = config_dir() / "config.json"
        self.data = default_config()
        self._lock = threading.Lock()
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config root is not an object")
            _merge(self.data, raw)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:  # keep the unreadable file instead of silently overwriting it
                self.path.replace(self.path.with_suffix(".json.bad"))
            except OSError:
                pass
        for p in PROVIDERS:  # fixed endpoints are never user-editable
            if not p["edit_base"]:
                self.data["providers"][p["id"]]["base_url"] = p["base_url"]
        if self.data["active_provider"] not in PROVIDER_BY_ID:
            self.data["active_provider"] = "gemini"
        if self.data["mode"] not in ("full", "region"):
            self.data["mode"] = "full"

    def save(self):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                payload = json.dumps(self.data, indent=2, ensure_ascii=False)
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self.path)
                if not IS_WINDOWS:
                    os.chmod(self.path, 0o600)  # the file holds API keys
            except OSError:
                pass


#for hotkey stuff


MODS = ("Ctrl", "Alt", "Shift", "Meta")
PYNPUT_KEYS = {
    "Space": "<space>", "Return": "<enter>", "Enter": "<enter>", "Esc": "<esc>", "Tab": "<tab>",
    "Backspace": "<backspace>", "Del": "<delete>", "Ins": "<insert>", "PgUp": "<page_up>",
    "PgDown": "<page_down>", "Home": "<home>", "End": "<end>", "Left": "<left>", "Right": "<right>",
    "Up": "<up>", "Down": "<down>", "Print": "<print_screen>", "Pause": "<pause>",
}

#i tried to make it work on hyprland but it didn't work so i just kinda gave up
HYPR_KEYS = {
    "Space": "space", "Return": "Return", "Enter": "KP_Enter", "Esc": "Escape", "Tab": "Tab",
    "Backspace": "BackSpace", "Del": "Delete", "Ins": "Insert", "PgUp": "Prior", "PgDown": "Next",
    "Home": "Home", "End": "End", "Left": "Left", "Right": "Right", "Up": "Up", "Down": "Down",
    "Print": "Print", "Pause": "Pause", ",": "comma", ".": "period", "/": "slash", ";": "semicolon",
    "'": "apostrophe", "-": "minus", "=": "equal", "[": "bracketleft", "]": "bracketright",
    "\\": "backslash", "`": "grave",
}


def split_seq(seq: str):
    seq = (seq or "").strip()
    if not seq or seq.endswith("+"):
        return None
    parts = seq.split("+")
    mods, key = parts[:-1], parts[-1]
    if not key or any(m not in MODS for m in mods):
        return None
    return mods, key


def to_pynput(seq: str):
    sp = split_seq(seq)
    if not sp:
        return None
    mods, key = sp
    mm = {"Ctrl": "<ctrl>", "Alt": "<alt>", "Shift": "<shift>", "Meta": "<cmd>"}
    if IS_MAC:  # Qt maps Cmd -> "Ctrl" and Control -> "Meta" on macOS
        mm["Ctrl"], mm["Meta"] = "<cmd>", "<ctrl>"
    if re.fullmatch(r"F\d{1,2}", key):
        k = f"<{key.lower()}>"
    elif key in PYNPUT_KEYS:
        k = PYNPUT_KEYS[key]
    elif len(key) == 1 and key not in "<>+":
        k = key.lower()
    else:
        return None
    return "+".join([mm[m] for m in mods] + [k])


def validate_hotkey(seq: str):
    sp = split_seq(seq)
    if not sp:
        return "unsupported key combination"
    mods, key = sp
    if not mods and not re.fullmatch(r"F\d{1,2}", key):
        return "add a modifier (Ctrl, Alt, Shift or Meta)"
    if to_pynput(seq) is None:
        return "unsupported key"
    return None


def hypr_combo(seq: str, syntax: str):
    sp = split_seq(seq)
    if not sp:
        return None
    mods, key = sp
    mm = {"Ctrl": "CTRL", "Alt": "ALT", "Shift": "SHIFT", "Meta": "SUPER"}
    k = HYPR_KEYS.get(key, key.upper() if len(key) == 1 and key.isalpha() else key)
    ms = [mm[m] for m in mods]
    if syntax == "lua":
        return " + ".join(ms + [k])
    return " ".join(ms) + ", " + k


def trigger_cmd(action: str) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(SCRIPT_PATH)} --trigger {action}"


def lua_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def detect_hypr_syntax() -> str:
    d = Path.home() / ".config" / "hypr"
    if (d / "hyprland.lua").exists() or not (d / "hyprland.conf").exists():
        return "lua"
    return "conf"


def hypr_snippet(hotkeys: dict, syntax: str) -> str:
    lines = []
    if syntax == "lua":
        lines.append("-- add to ~/.config/hypr/hyprland.lua, then run: hyprctl reload")
    else:
        lines.append("# add to ~/.config/hypr/hyprland.conf, then run: hyprctl reload")
    for action in ("region", "toggle", "pause"):
        seq = hotkeys.get(action) or ""
        combo = hypr_combo(seq, syntax) if seq else None
        if not combo:
            continue
        cmd = trigger_cmd(action)
        if syntax == "lua":
            lines.append(f'hl.bind("{combo}", hl.dsp.exec_cmd("{lua_escape(cmd)}"))  -- {HOTKEY_LABELS[action]}')
        else:
            lines.append(f"bind = {combo}, exec, {cmd}  # {HOTKEY_LABELS[action]}")
    lines.append("")
    lines.append("Other compositors / desktops: bind your own shortcut to these commands:")
    for action in ("region", "toggle", "pause"):
        lines.append(f"  {HOTKEY_LABELS[action]:<24} {trigger_cmd(action)}")
    return "\n".join(lines)


#capture screen shit

class CaptureError(Exception):
    pass


class Capturer:
    """Wayland (Hyprland, sway, ...) uses grim + slurp. Everything else uses mss + a Qt selector."""

    def __init__(self):
        self.wayland = is_wayland()
        self.hypr = is_hyprland()

    @property
    def backend(self) -> str:
        return "grim + slurp (Wayland)" if self.wayland else "mss + built-in region selector"

    def problem(self, need_region: bool = False):
        if self.wayland:
            if not shutil.which("grim"):
                return "grim is not installed (Arch: sudo pacman -S grim slurp)."
            if need_region and not shutil.which("slurp"):
                return "slurp is not installed (Arch: sudo pacman -S grim slurp)."
            return None
        try:
            import mss  # noqa: F401
        except Exception:
            return "The 'mss' package is missing: pip install mss"
        return None

    def region_ok(self, region) -> bool:
        if not isinstance(region, dict):
            return False
        if region.get("type") == "geom":
            return self.wayland and bool(re.fullmatch(r"-?\d+,-?\d+ \d+x\d+", str(region.get("geom", ""))))
        if region.get("type") == "mss":
            return (not self.wayland) and all(isinstance(region.get(k), int) for k in ("left", "top", "width", "height"))
        return False

    # monitors 
    def _hypr_monitors(self) -> list:
        r = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout or "[]")
        return [m for m in data if isinstance(m, dict) and m.get("name")]

    def list_monitors(self) -> list:
        items = [("Primary / focused monitor", "auto"), ("All monitors", "all")]
        try:
            if self.wayland:
                if self.hypr and shutil.which("hyprctl"):
                    for m in self._hypr_monitors():
                        items.append((f"{m['name']} ({m.get('width', '?')}x{m.get('height', '?')})", m["name"]))
            else:
                import mss
                with mss.mss() as sct:
                    for i, m in enumerate(sct.monitors[1:], 1):
                        items.append((f"Monitor {i} ({m['width']}x{m['height']})", str(i)))
        except Exception:
            pass
        return items

    # grabbing
    #エッチなのはダメ！
    
    @staticmethod
    def _to_pil(shot):
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    @staticmethod
    def _grim(args: list):
        try:
            r = subprocess.run(["grim", *args, "-"], capture_output=True, timeout=20)
        except FileNotFoundError:
            raise CaptureError("grim is not installed (Arch: sudo pacman -S grim slurp).")
        except subprocess.TimeoutExpired:
            raise CaptureError("grim timed out.")
        if r.returncode != 0 or not r.stdout:
            msg = (r.stderr or b"").decode("utf-8", "ignore").strip()
            raise CaptureError(f"grim failed: {msg[:200] or 'no output'}")
        img = Image.open(io.BytesIO(r.stdout))
        img.load()
        return img.convert("RGB")

    def grab_full(self, key: str):
        if self.wayland:
            args = []
            out = None
            if key == "auto" and self.hypr and shutil.which("hyprctl"):
                try:
                    for m in self._hypr_monitors():
                        if m.get("focused"):
                            out = m["name"]
                except Exception:
                    out = None
            elif key not in ("auto", "all"):
                out = key
            if out:
                args += ["-o", out]
            return self._grim(args)
        import mss
        with mss.mss() as sct:
            idx = 0 if key == "all" else (1 if key == "auto" else int(key))
            if idx >= len(sct.monitors):
                idx = 1 if len(sct.monitors) > 1 else 0
            return self._to_pil(sct.grab(sct.monitors[idx]))

    def grab_region(self, region):
        if not self.region_ok(region):
            raise CaptureError("No valid region. Select a region first.")
        if region["type"] == "geom":
            return self._grim(["-g", region["geom"]])
        import mss
        box = {k: int(region[k]) for k in ("left", "top", "width", "height")}
        with mss.mss() as sct:
            return self._to_pil(sct.grab(box))

    def grab_all_monitors(self) -> list:
        import mss
        shots = []
        with mss.mss() as sct:
            for m in sct.monitors[1:]:
                shots.append((dict(m), self._to_pil(sct.grab(m))))
        return shots

    def select_region_wayland(self):
        """Blocking. Returns a region dict, or None if the user cancelled."""
        styled = ["slurp", "-d", "-b", "#00000066", "-c", "#ffffffff", "-w", "2"]
        last_err = ""
        for cmd in (styled, ["slurp"]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            except FileNotFoundError:
                raise CaptureError("slurp is not installed (Arch: sudo pacman -S grim slurp).")
            except subprocess.TimeoutExpired:
                return None
            geom = (r.stdout or "").strip()
            if r.returncode != 0 or not geom:
                err = (r.stderr or "").strip()
                if not err or "cancel" in err.lower():
                    return None
                last_err = err
                continue
            m = re.fullmatch(r"(-?\d+),(-?\d+) (\d+)x(\d+)", geom)
            if not m or int(m.group(3)) < 4 or int(m.group(4)) < 4:
                return None
            return {"type": "geom", "geom": geom}
        raise CaptureError(f"slurp failed: {last_err[:200]}")


def describe_region(r) -> str:
    if not r:
        return "No region selected"
    if r.get("type") == "geom":
        m = re.fullmatch(r"(-?\d+),(-?\d+) (\d+)x(\d+)", str(r.get("geom", "")))
        if m:
            return f"{m.group(3)}\u00d7{m.group(4)} px at ({m.group(1)}, {m.group(2)})"
        return str(r.get("geom"))
    try:
        return f"{r['width']}\u00d7{r['height']} px at ({r['left']}, {r['top']})"
    except KeyError:
        return "No region selected"


def match_screens(screens, monitors) -> list:
    """Pair Qt screens with mss monitors. Returns [(QScreen, monitor_index)]."""
    pairs, used = [], set()
    for qs in screens:
        g, dpr = qs.geometry(), qs.devicePixelRatio()
        best = None
        for mi, m in enumerate(monitors):
            if mi in used:
                continue
            size = min(abs(m["width"] - g.width() * dpr) + abs(m["height"] - g.height() * dpr),
                       abs(m["width"] - g.width()) + abs(m["height"] - g.height()))
            pos = min(abs(m["left"] - g.x() * dpr) + abs(m["top"] - g.y() * dpr),
                      abs(m["left"] - g.x()) + abs(m["top"] - g.y()))
            score = size * 10 + pos
            if best is None or score < best[0]:
                best = (score, mi)
        if best:
            used.add(best[1])
            pairs.append((qs, best[1]))
    return pairs


class RegionOverlay(QWidget):
    """Frozen-screenshot overlay for dragging out a region (X11, Windows, macOS)."""
    picked = Signal(int, QRect, QSize)
    cancelled = Signal()

    def __init__(self, shot_index: int, screen, pil_img):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.shot_index = shot_index
        self.target_screen = screen
        self.setScreen(screen)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        rgb = pil_img.convert("RGB")
        w, h = rgb.size
        buf = rgb.tobytes("raw", "RGB")
        self.pix = QPixmap.fromImage(QImage(buf, w, h, w * 3, QImage.Format.Format_RGB888))
        self.start = None
        self.cur = None

    def launch(self):
        self.setGeometry(self.target_screen.geometry())
        if IS_MAC:
            self.show()
        else:
            self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def sel_rect(self):
        if self.start is None or self.cur is None:
            return None
        return QRect(self.start, self.cur).normalized().intersected(self.rect())

    def paintEvent(self, _event):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pix)
        p.fillRect(self.rect(), QColor(0, 0, 0, 130))
        sel = self.sel_rect()
        if sel is not None and sel.width() > 0 and sel.height() > 0:
            sx = self.pix.width() / max(1, self.width())
            sy = self.pix.height() / max(1, self.height())
            p.drawPixmap(QRectF(sel), self.pix, QRectF(sel.x() * sx, sel.y() * sy, sel.width() * sx, sel.height() * sy))
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawRect(sel)
            label = f"{sel.width()} \u00d7 {sel.height()}"
            p.setPen(QColor("#ffffff"))
            p.fillRect(QRect(sel.x(), max(0, sel.y() - 24), 92, 22), QColor(0, 0, 0, 190))
            p.drawText(QRect(sel.x() + 6, max(0, sel.y() - 24), 86, 22), Qt.AlignmentFlag.AlignVCenter, label)
        else:
            hint = "Drag to select a region  \u00b7  Esc or right-click to cancel"
            box = QRect(self.width() // 2 - 200, 28, 400, 34)
            p.fillRect(box, QColor(0, 0, 0, 200))
            p.setPen(QColor("#ececee"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
        elif e.button() == Qt.MouseButton.LeftButton:
            self.start = e.position().toPoint()
            self.cur = self.start
            self.update()

    def mouseMoveEvent(self, e):
        if self.start is not None:
            self.cur = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.start is None:
            return
        self.cur = e.position().toPoint()
        r = self.sel_rect()
        self.start = None
        if r is None or r.width() < 8 or r.height() < 8:
            self.cur = None
            self.update()
            return
        self.picked.emit(self.shot_index, r, self.size())

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


class OverlayManager(QObject):
    finished = Signal(object)  # None, or (region dict, PIL image)

    def __init__(self, shots: list, screens: list):
        super().__init__()
        self.shots = shots
        self.overlays = []
        self._done = False
        for qs, si in match_screens(screens, [m for m, _ in shots]):
            ov = RegionOverlay(si, qs, shots[si][1])
            ov.picked.connect(self._picked)
            ov.cancelled.connect(lambda: self._finish(None))
            self.overlays.append(ov)

    def start(self):
        if not self.overlays:
            self._finish(None)
            return
        for ov in self.overlays:
            ov.launch()

    def _picked(self, si: int, rect: QRect, wsize: QSize):
        mon, img = self.shots[si]
        fx, fy = img.width / max(1, wsize.width()), img.height / max(1, wsize.height())
        box = (max(0, round(rect.x() * fx)), max(0, round(rect.y() * fy)),
               min(img.width, round((rect.x() + rect.width()) * fx)),
               min(img.height, round((rect.y() + rect.height()) * fy)))
        crop = img.crop(box)
        mx, my = mon["width"] / max(1, wsize.width()), mon["height"] / max(1, wsize.height())
        region = {
            "type": "mss",
            "left": int(mon["left"] + round(rect.x() * mx)),
            "top": int(mon["top"] + round(rect.y() * my)),
            "width": max(1, int(round(rect.width() * mx))),
            "height": max(1, int(round(rect.height() * my))),
        }
        self._finish((region, crop))

    def _finish(self, payload):
        if self._done:
            return
        self._done = True

        def close_all():
            for ov in self.overlays:
                ov.close()
                ov.deleteLater()
            self.overlays = []
            self.finished.emit(payload)

        QTimer.singleShot(0, close_all)


# ----------------------------------------------------------------------------
# OCR via AI APIs
# ----------------------------------------------------------------------------
class OCRError(Exception):
    def __init__(self, msg: str, skip_provider: bool = False):
        super().__init__(msg)
        self.skip_provider = skip_provider


def parse_keys(s: str) -> list:
    out = []
    for k in re.split(r"[,\s]+", s or ""):
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def build_prompt(cfg: dict) -> str:
    lang = (cfg.get("language") or "").strip()
    p = ("You are an OCR engine. Transcribe all text visible in the image exactly as it appears, in natural "
         "reading order, one line of text per output line. Do not translate, summarise, explain or add anything. "
         "Do not add romanisation or furigana unless it is printed in the image. Do not use markdown or code "
         "fences. Ignore icons and decoration.")
    if lang and not lang.lower().startswith("auto"):
        p += f" The text is mainly {lang}."
        if lang.lower().startswith("japanese"):
            p += " Vertical Japanese text is read top to bottom, right to left."
    extra = (cfg.get("extra_prompt") or "").strip()
    if extra:
        p += " " + extra
    p += " If there is no readable text, reply with exactly: [NO_TEXT]"
    return p


def clean_text(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "").strip()
    m = re.fullmatch(r"```[A-Za-z0-9_-]*\n(.*?)\n?```", t, flags=re.S)
    if m:
        t = m.group(1)
    if t.strip() == "[NO_TEXT]":
        return ""
    lines = [ln.rstrip() for ln in t.split("\n") if ln.strip() and ln.strip() != "[NO_TEXT]"]
    return "\n".join(lines)


def encode_image(img, max_side: int):
    img = img.convert("RGB")
    w, h = img.size
    if min(w, h) < 56:  # tiny crops confuse vision encoders
        s = 56 / min(w, h)
        img = img.resize((max(56, round(w * s)), max(56, round(h * s))), Image.Resampling.LANCZOS)
        w, h = img.size
    max_side = max(640, int(max_side))
    if max(w, h) > max_side:
        s = max_side / max(w, h)
        img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS)
    limit = 2_800_000  # keeps the base64 payload under common 4 MB request limits
    for _ in range(8):
        buf = io.BytesIO()
        img.save(buf, "PNG")
        if buf.tell() <= limit:
            return buf.getvalue(), "image/png"
        for q in (92, 85, 75):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q)
            if buf.tell() <= limit:
                return buf.getvalue(), "image/jpeg"
        img = img.resize((max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))), Image.Resampling.LANCZOS)
    raise OCRError("Image is too large to send.")


def _err_message(resp) -> str:
    try:
        j = resp.json()
        e = j.get("error", j) if isinstance(j, dict) else j
        msg = (e.get("message") or json.dumps(e)) if isinstance(e, dict) else str(e)
    except ValueError:
        msg = resp.text
    return " ".join(str(msg).split())[:240]


def call_provider(pdef: dict, pc: dict, key: str, data: bytes, mime: str, prompt: str) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    model = (pc.get("model") or "").strip()
    if not model:
        raise OCRError("no model set", skip_provider=True)
    try:
        if pdef["kind"] == "gemini":
            if model.startswith("models/"):
                model = model[7:]
            resp = requests.post(
                f"{pdef['base_url']}/models/{model}:generateContent",
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": b64}}]}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
                },
                timeout=(10, 90),
            )
        else:
            base = (pc.get("base_url") or "").strip().rstrip("/")
            if not base:
                raise OCRError("no base URL set", skip_provider=True)
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            resp = requests.post(
                base + "/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ]}],
                },
                timeout=(10, 90),
            )
    except requests.exceptions.RequestException as exc:
        raise OCRError(f"network error ({exc.__class__.__name__})")
    if resp.status_code >= 400:
        raise OCRError(f"HTTP {resp.status_code}: {_err_message(resp)}", skip_provider=resp.status_code == 404)
    try:
        j = resp.json()
        if pdef["kind"] == "gemini":
            cands = j.get("candidates") or []
            if not cands:
                reason = (j.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
                raise OCRError(f"blocked or empty response ({reason})")
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        content = j["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return content or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise OCRError("unexpected response from the API")


def _is_zero(v) -> bool:
    try:
        return float(v) == 0.0
    except (TypeError, ValueError):
        return False


def fetch_models(pdef: dict, pc: dict, key: str) -> list:
    try:
        if pdef["kind"] == "gemini":
            if not key:
                raise OCRError("Enter an API key first.")
            out, token = [], None
            for _ in range(10):
                params = {"pageSize": 200}
                if token:
                    params["pageToken"] = token
                r = requests.get(f"{pdef['base_url']}/models", headers={"x-goog-api-key": key},
                                 params=params, timeout=20)
                if r.status_code >= 400:
                    raise OCRError(f"HTTP {r.status_code}: {_err_message(r)}")
                j = r.json()
                for m in j.get("models", []):
                    name = str(m.get("name", "")).replace("models/", "", 1)
                    if ("generateContent" in m.get("supportedGenerationMethods", []) and name.startswith("gemini")
                            and not any(x in name for x in ("tts", "embedding", "image", "live", "audio", "robotics", "computer-use"))):
                        out.append(name)
                token = j.get("nextPageToken")
                if not token:
                    break
            return sorted(set(out), key=lambda n: (0 if "flash" in n else 1, n))
        base = (pc.get("base_url") or "").strip().rstrip("/")
        if not base:
            raise OCRError("Enter the base URL first.")
        if pdef["needs_key"] and not key and pdef["id"] != "openrouter":
            raise OCRError("Enter an API key first.")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = requests.get(base + "/models", headers=headers, timeout=20)
        if r.status_code >= 400:
            raise OCRError(f"HTTP {r.status_code}: {_err_message(r)}")
        ids = []
        for it in r.json().get("data", []):
            mid = it.get("id") if isinstance(it, dict) else None
            if not mid:
                continue
            if pdef["id"] == "openrouter":
                mods = (it.get("architecture") or {}).get("input_modalities") or []
                if "image" not in mods or not _is_zero((it.get("pricing") or {}).get("prompt")):
                    continue
            elif pdef["id"] == "groq" and any(x in mid for x in ("whisper", "tts", "orpheus", "guard", "embed")):
                continue
            elif pdef["id"] == "deepseek" and "vision" not in mid:
                continue
            ids.append(mid)
        return sorted(set(ids))
    except requests.exceptions.RequestException as exc:
        raise OCRError(f"network error ({exc.__class__.__name__})")
    except (ValueError, AttributeError):
        raise OCRError("unexpected response from the API")


def run_test(pdef: dict, pc: dict, key: str, cfg: dict) -> str:
    img = Image.new("RGB", (110, 28), "white")
    ImageDraw.Draw(img).text((8, 8), "OCR TEST 123", fill="black")
    img = img.resize((550, 140), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return clean_text(call_provider(pdef, pc, key, buf.getvalue(), "image/png", build_prompt(cfg)))


class OCRClient:
    def __init__(self, get_cfg):
        self.get_cfg = get_cfg
        self._idx = {}
        self._lock = threading.Lock()

    @staticmethod
    def eligible(pid: str, cfg: dict) -> bool:
        p, pc = PROVIDER_BY_ID[pid], cfg["providers"][pid]
        if not str(pc.get("model", "")).strip():
            return False
        if p["kind"] == "openai" and not str(pc.get("base_url", "")).strip():
            return False
        if p["needs_key"] and not parse_keys(pc.get("keys", "")):
            return False
        return True

    def chain(self, cfg: dict) -> list:
        order = []
        active = cfg["active_provider"]
        if self.eligible(active, cfg):
            order.append(active)
        if cfg.get("fallback"):
            for p in PROVIDERS:
                if p["id"] != active and cfg["providers"][p["id"]].get("fallback") and self.eligible(p["id"], cfg):
                    order.append(p["id"])
        return order

    def recognize(self, data: bytes, mime: str):
        cfg = self.get_cfg()
        chain = self.chain(cfg)
        if not chain:
            raise OCRError("No provider is ready. Add an API key in the AI Keys tab.")
        prompt = build_prompt(cfg)
        errors = []
        for pid in chain:
            pdef, pc = PROVIDER_BY_ID[pid], cfg["providers"][pid]
            keys = parse_keys(pc.get("keys", "")) or ([""] if not pdef["needs_key"] else [])
            with self._lock:
                start = self._idx.get(pid, 0) % len(keys)
            for off in range(len(keys)):
                i = (start + off) % len(keys)
                try:
                    text = call_provider(pdef, pc, keys[i], data, mime, prompt)
                except OCRError as exc:
                    errors.append(f"{pdef['name']}: {exc}")
                    if exc.skip_provider:
                        break
                    continue
                with self._lock:
                    self._idx[pid] = i + 1  # rotate keys to spread free-tier limits
                return clean_text(text), pdef["name"]
        raise OCRError(" | ".join(errors)[:600])


class TextSink:
    def __init__(self, get_cfg):
        self.get_cfg = get_cfg
        self._lock = threading.Lock()
        self._last = ""
        self._seen = set()

    def reset_session(self):
        with self._lock:
            self._seen.clear()
            self._last = ""

    def write(self, text: str, force: bool = False) -> int:
        cfg = self.get_cfg()
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return 0
        norm = " ".join(" ".join(lines).split())
        with self._lock:
            if not force and cfg["skip_duplicates"] and norm == self._last:
                return 0
            self._last = norm
            if not force and cfg["only_new_lines"]:
                fresh = []
                for ln in lines:
                    k = " ".join(ln.split())
                    if k not in self._seen:
                        self._seen.add(k)
                        fresh.append(ln)
                lines = fresh
                if not lines:
                    return 0
            path = Path(os.path.expanduser(cfg["output_file"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] " if cfg["timestamps"] else ""
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write("".join(f"{stamp}{ln}\n" for ln in lines))
            return len(lines)


def frame_signature(img):
    return img.convert("L").resize((960, 540), Image.Resampling.BILINEAR)


def frame_changed(a, b, threshold: float = 0.00005) -> bool:
    if a is None or b is None or a.size != b.size:
        return True
    diff = ImageChops.difference(a, b).point(lambda p: 255 if p > 28 else 0)
    return ImageStat.Stat(diff).mean[0] / 255.0 > threshold


# ----------------------------------------------------------------------------
# Hotkeys (X11 / Windows / macOS via pynput)
# ----------------------------------------------------------------------------
class HotkeyManager:
    def __init__(self, emit):
        self.emit = emit
        self.listener = None

    def stop(self):
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None

    def apply(self, hotkeys: dict):
        self.stop()
        if is_wayland():
            return False, ("Wayland: apps can't grab global keys. Bind the keys in your compositor "
                           "using the lines below (they call --trigger).")
        try:
            from pynput import keyboard
        except Exception:
            return False, "Global hotkeys need the 'pynput' package:  pip install pynput"
        mapping = {}
        for cmd, seq in hotkeys.items():
            if not seq:
                continue
            pk = to_pynput(seq)
            if not pk:
                return False, f"Unsupported hotkey: {seq}"
            mapping[pk] = (lambda c=cmd: self.emit(c))
        if not mapping:
            return True, "No hotkeys set."
        try:
            self.listener = keyboard.GlobalHotKeys(mapping)
            self.listener.daemon = True
            self.listener.start()
        except Exception as exc:
            return False, f"Could not register hotkeys: {exc}"
        note = " On macOS, allow Accessibility / Input Monitoring for this app if keys do nothing." if IS_MAC else ""
        return True, "Global hotkeys active." + note


# ----------------------------------------------------------------------------
# Qt plumbing
# ----------------------------------------------------------------------------
class Bus(QObject):
    log = Signal(str, str)                # level, message
    result = Signal(str, int, str, str)   # text, lines saved, provider, source
    command = Signal(str)
    select_done = Signal(object)          # (region, image, error)
    models_ready = Signal(str, object, str)
    test_done = Signal(str, bool, str)
    auto_stopped = Signal(object)
    notify = Signal(str, str)


class Recorder(threading.Thread):
    MAX_FAILS = 5

    def __init__(self, win, mode: str, region):
        super().__init__(daemon=True)
        self.win = win
        self.mode = mode
        self.region = region
        self.stop_evt = threading.Event()
        self.run_evt = threading.Event()
        self.run_evt.set()

    @property
    def paused(self) -> bool:
        return not self.run_evt.is_set()

    def pause(self):
        self.run_evt.clear()

    def resume(self):
        self.run_evt.set()

    def stop(self):
        self.stop_evt.set()
        self.run_evt.set()

    def _grab(self):
        cap = self.win.capturer
        if self.mode == "region":
            return cap.grab_region(self.region)
        return cap.grab_full(self.win.d["monitor"])

    def run(self):
        bus = self.win.bus
        fails = 0
        last_sig = None
        while not self.stop_evt.is_set():
            if not self.run_evt.wait(0.1):
                continue
            try:
                img = self._grab()
                sig = frame_signature(img)
                if self.win.d["skip_unchanged"] and not frame_changed(sig, last_sig):
                    pass
                else:
                    text, written, provider = self.win.process_image(img)
                    last_sig = sig
                    if not self.stop_evt.is_set():
                        bus.result.emit(text, written, provider, "recording")
                fails = 0
            except Exception as exc:  # keep the loop alive; report and back off
                fails += 1
                bus.log.emit("err", str(exc))
                if fails >= self.MAX_FAILS:
                    bus.log.emit("err", f"Stopped after {self.MAX_FAILS} failures in a row. "
                                        "Check your API key, quota and capture tools.")
                    self.stop_evt.set()
                    bus.auto_stopped.emit(self)
                    return
            wait = max(0.25, int(self.win.d["interval_ms"]) / 1000.0) + min(20, 3 * fails)
            self.stop_evt.wait(wait)


# ----------------------------------------------------------------------------
# Styling
# ----------------------------------------------------------------------------
QSS = """
QWidget { background: transparent; color: #e6e6e8; font-size: 13px; }
QMainWindow, QWidget#root { background: #0e0e0f; }
QDialog, QFileDialog { background: #121214; }
QToolTip { background: #1d1d21; color: #e6e6e8; border: 1px solid #34343a; padding: 4px 6px; }
QLabel#h1 { font-size: 24px; font-weight: 700; color: #f4f4f5; }
QLabel#muted { color: #85858d; }
QLabel#cardTitle { font-size: 15px; font-weight: 600; color: #f1f1f3; }
QLabel#brand { font-size: 17px; font-weight: 700; color: #f4f4f5; }
QLabel#badge { background: #26262b; color: #c9c9ce; border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 600; }
QFrame#sidebar { background: #121214; border-right: 1px solid #222226; }
QFrame#card { background: #161619; border: 1px solid #25252a; border-radius: 12px; }
QPushButton { background: #222226; border: 1px solid #303036; border-radius: 8px; padding: 8px 16px; color: #e6e6e8; }
QPushButton:hover { background: #2b2b31; border-color: #3c3c44; }
QPushButton:pressed { background: #1b1b1f; }
QPushButton:disabled { background: #151518; color: #5c5c63; border-color: #222226; }
QPushButton#primary { background: #ececee; color: #0e0e0f; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #ffffff; }
QPushButton#primary:disabled { background: #2c2c31; color: #6b6b72; }
QPushButton#danger { background: #2a1719; border: 1px solid #4a2428; color: #ff8a8f; }
QPushButton#danger:hover { background: #351c1f; }
QPushButton#danger:disabled { background: #151518; color: #5c5c63; border-color: #222226; }
QPushButton#nav { text-align: left; padding: 10px 16px; border: none; border-radius: 8px; background: transparent; color: #9a9aa2; font-size: 14px; }
QPushButton#nav:hover { background: #1b1b1f; color: #e6e6e8; }
QPushButton#nav:checked { background: #26262b; color: #ffffff; font-weight: 600; }
QPushButton#seg { background: #101012; border: 1px solid #2e2e33; padding: 8px 20px; }
QPushButton#seg:hover { border-color: #46464e; }
QPushButton#seg:checked { background: #ececee; color: #0e0e0f; border-color: #ececee; font-weight: 600; }
QPushButton#seg:disabled { color: #5c5c63; }
QPushButton#seg:checked:disabled { background: #3a3a3f; color: #85858d; border-color: #3a3a3f; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #101012; border: 1px solid #2e2e33; border-radius: 8px; padding: 7px 10px; selection-background-color: #4a4a54; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #8a8a94; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #5c5c63; }
QPlainTextEdit { font-family: "JetBrains Mono", "DejaVu Sans Mono", "Menlo", "Consolas", monospace; font-size: 12px; }
QComboBox { padding-right: 26px; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: url("@ARROW@"); width: 10px; height: 6px; }
QComboBox QAbstractItemView { background: #17171a; border: 1px solid #34343a; selection-background-color: #2e2e34; selection-color: #ffffff; outline: 0; padding: 4px; }
QSpinBox::up-button, QSpinBox::down-button { width: 0; border: none; }
QCheckBox, QRadioButton { spacing: 10px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid #4a4a52; background: #101012; }
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 9px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #8a8a94; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #ececee; border-color: #ececee; }
QCheckBox::indicator:checked { image: url("@CHECK@"); }
QRadioButton#providerName { font-size: 15px; font-weight: 600; color: #f1f1f3; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #34343a; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #46464e; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { height: 0; }
QMenu { background: #17171a; border: 1px solid #2a2a2f; padding: 6px; }
QMenu::item { padding: 7px 20px; border-radius: 6px; }
QMenu::item:selected { background: #2a2a30; }
"""


def make_assets():
    d = config_dir() / "assets"
    d.mkdir(parents=True, exist_ok=True)
    check, arrow = d / "check.png", d / "arrow.png"
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#0e0e0f"), 2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPoint(4, 8), QPoint(7, 11), QPoint(12, 5)])
    p.end()
    pm.save(str(check))
    pm = QPixmap(12, 8)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#9a9aa2"))
    p.drawPolygon([QPoint(1, 1), QPoint(11, 1), QPoint(6, 7)])
    p.end()
    pm.save(str(arrow))
    return check.as_posix(), arrow.as_posix()


def make_app_icon() -> QIcon:
    pm = QPixmap(128, 128)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#18181b"))
    p.setPen(QPen(QColor("#3a3a40"), 4))
    p.drawRoundedRect(6, 6, 116, 116, 26, 26)
    pen = QPen(QColor("#ececee"), 9)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline([QPoint(30, 52), QPoint(30, 30), QPoint(52, 30)])
    p.drawPolyline([QPoint(76, 30), QPoint(98, 30), QPoint(98, 52)])
    p.drawPolyline([QPoint(98, 76), QPoint(98, 98), QPoint(76, 98)])
    p.drawPolyline([QPoint(52, 98), QPoint(30, 98), QPoint(30, 76)])
    p.drawLine(46, 58, 82, 58)
    p.drawLine(46, 74, 70, 74)
    p.end()
    return QIcon(pm)


def make_card(title: str, subtitle: str = ""):
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 18, 20, 18)
    lay.setSpacing(12)
    t = QLabel(title)
    t.setObjectName("cardTitle")
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("muted")
        s.setWordWrap(True)
        lay.addWidget(s)
    return frame, lay


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


def page_header(title: str, sub: str):
    h = QLabel(title)
    h.setObjectName("h1")
    return h, muted(sub)


def scroll_wrap(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(inner)
    return sa


def h_row(*items, stretch_end: bool = False) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(10)
    for it in items:
        if isinstance(it, int):
            lay.addSpacing(it)
        else:
            lay.addWidget(it)
    if stretch_end:
        lay.addStretch(1)
    return lay


class ProviderCard(QFrame):
    def __init__(self, win, pdef: dict):
        super().__init__()
        self.setObjectName("card")
        self.win, self.pdef = win, pdef
        pc = win.d["providers"][pdef["id"]]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.active = QRadioButton(pdef["name"])
        self.active.setObjectName("providerName")
        head.addWidget(self.active)
        if pdef["free"]:
            badge = QLabel(pdef["free"].upper())
            badge.setObjectName("badge")
            head.addWidget(badge)
        head.addStretch(1)
        self.fallback = QCheckBox("Use as fallback")
        self.fallback.setToolTip("If the active provider fails or is rate limited, try this one next.")
        head.addWidget(self.fallback)
        lay.addLayout(head)
        lay.addWidget(muted(pdef["note"]))

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        r = 0
        self.base = None
        if pdef["edit_base"]:
            grid.addWidget(QLabel("Base URL"), r, 0)
            self.base = QLineEdit(pc["base_url"])
            self.base.setPlaceholderText("https://host/v1")
            grid.addWidget(self.base, r, 1, 1, 2)
            r += 1
        grid.addWidget(QLabel("API key" if pdef["needs_key"] else "API key (optional)"), r, 0)
        self.key = QLineEdit(pc["keys"])
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Paste key. Several keys separated by commas are rotated.")
        grid.addWidget(self.key, r, 1)
        self.show_btn = QPushButton("Show")
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(self._toggle_show)
        grid.addWidget(self.show_btn, r, 2)
        r += 1
        grid.addWidget(QLabel("Model"), r, 0)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.addItem(pc["model"])
        self.model.setCurrentText(pc["model"])
        grid.addWidget(self.model, r, 1, 1, 2)
        lay.addLayout(grid)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        self.get_btn = None
        btns = []
        if pdef["key_url"]:
            self.get_btn = QPushButton(pdef["key_label"])
            self.get_btn.clicked.connect(lambda: win.open_url(pdef["key_url"]))
            btns.append(self.get_btn)
        self.fetch_btn = QPushButton("Fetch models")
        self.fetch_btn.clicked.connect(lambda: win.fetch_models(self))
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(lambda: win.test_provider(self))
        btns += [self.fetch_btn, self.test_btn]
        lay.addLayout(h_row(*btns, stretch_end=True))
        lay.addWidget(self.status)

        self.fallback.setChecked(bool(pc["fallback"]))
        self.key.textChanged.connect(lambda _t: win.on_provider_edit(self))
        self.model.currentTextChanged.connect(lambda _t: win.on_provider_edit(self))
        self.fallback.toggled.connect(lambda _v: win.on_provider_edit(self))
        if self.base is not None:
            self.base.textChanged.connect(lambda _t: win.on_provider_edit(self))

    def _toggle_show(self, on: bool):
        self.key.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self.show_btn.setText("Hide" if on else "Show")

    def first_key(self) -> str:
        keys = parse_keys(self.key.text())
        return keys[0] if keys else ""

    def snapshot(self) -> dict:
        return {
            "keys": self.key.text().strip(),
            "model": self.model.currentText().strip(),
            "base_url": self.base.text().strip() if self.base is not None else self.pdef["base_url"],
            "fallback": self.fallback.isChecked(),
        }

    def set_models(self, models: list):
        cur = self.model.currentText()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(models)
        self.model.setCurrentText(cur)
        self.model.blockSignals(False)

    def set_status(self, text: str, level: str = "info"):
        color = {"info": "#85858d", "ok": "#7ee2a8", "warn": "#f5b942", "err": "#ff6b70"}[level]
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
        self.status.setVisible(bool(text))


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, bus: Bus, capturer: Capturer, ocr: OCRClient, sink: TextSink, ipc_ok: bool):
        super().__init__()
        self.cfg, self.d, self.bus = cfg, cfg.data, bus
        self.capturer, self.ocr, self.sink, self.ipc_ok = capturer, ocr, sink, ipc_ok
        self.rec = None
        self.state = "idle"
        self.captures = 0
        self.lines_total = 0
        self._selecting = False
        self._select_cb = None
        self._restore_after = False
        self._overlay = None
        self._oneshot_busy = False
        self._really_quit = False
        self.cards = {}
        self.tray = None
        self.ipc = None
        self.hotkeys = HotkeyManager(lambda c: self.bus.command.emit(c))
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self.cfg.save)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_app_icon())
        self.resize(1040, 740)
        self.setMinimumSize(880, 620)
        self._build_ui()
        self._build_tray()

        bus.log.connect(self.add_log)
        bus.result.connect(self.on_result)
        bus.command.connect(self.on_command)
        bus.select_done.connect(self._on_select_done)
        bus.models_ready.connect(self._on_models_ready)
        bus.test_done.connect(self._on_test_done)
        bus.auto_stopped.connect(self._on_auto_stopped)
        bus.notify.connect(self.notify)

        self._set_state("idle")
        self._apply_mode_ui()
        self.refresh_monitors()
        self.apply_hotkeys()
        self._startup_notes()

    # ---- small helpers ----------------------------------------------------
    def save_soon(self):
        self._save_timer.start()

    def set_cfg(self, key, value):
        self.d[key] = value
        self.save_soon()

    def open_url(self, url: str):
        if not QDesktopServices.openUrl(QUrl(url)):
            webbrowser.open(url)

    def add_log(self, level: str, msg: str):
        color = {"info": "#9a9aa2", "ok": "#7ee2a8", "warn": "#f5b942", "err": "#ff6b70"}.get(level, "#9a9aa2")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendHtml(
            f'<span style="color:#5f5f67">{stamp}</span>&nbsp;&nbsp;<span style="color:{color}">{html.escape(msg)}</span>')

    def notify(self, title: str, body: str):
        if not self.d["notifications"]:
            return
        if IS_LINUX and shutil.which("notify-send"):
            try:
                subprocess.Popen(["notify-send", "-a", APP_NAME, "-t", "3500", title, body[:300]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, body[:300], QSystemTrayIcon.MessageIcon.NoIcon, 3500)

    def _startup_notes(self):
        self.add_log("info", f"Capture backend: {self.capturer.backend}")
        prob = self.capturer.problem(need_region=True)
        if prob:
            self.add_log("warn", prob)
        if not self.ocr.chain(self.d):
            self.add_log("warn", "No AI provider is ready yet. Open the AI Keys tab and add a free key.")
        if not self.ipc_ok:
            self.add_log("warn", "Could not open the command channel; --trigger hotkeys will not work.")

    # ---- UI construction ----------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 22, 14, 18)
        sl.setSpacing(6)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        sl.addWidget(brand)
        sl.addWidget(muted("Screen text to file"))
        sl.addSpacing(18)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_btns = []
        for i, name in enumerate(("Capture", "AI Keys", "Settings")):
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, idx=i: self.show_page(idx))
            self.nav_group.addButton(b)
            self.nav_btns.append(b)
            sl.addWidget(b)
        sl.addStretch(1)
        self.side_status = QLabel("")
        self.side_status.setObjectName("muted")
        sl.addWidget(self.side_status)
        sl.addWidget(muted(f"v{VERSION}"))
        outer.addWidget(side)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_capture_page())
        self.stack.addWidget(self._build_keys_page())
        self.stack.addWidget(self._build_settings_page())
        outer.addWidget(self.stack, 1)
        self.show_page(0)

    def show_page(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self.nav_btns[idx].setChecked(True)

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(30, 28, 30, 28)
        lay.setSpacing(16)
        return w, lay

    # -- capture page
    def _build_capture_page(self):
        page, lay = self._page()
        h, s = page_header("Capture", "Read the whole screen, or just a region. New text is appended to your output file.")
        lay.addWidget(h)
        lay.addWidget(s)

        card, cl = make_card("Recording", "Reads the screen again every few seconds and saves text that changed.")
        self.btn_full = QPushButton("Full screen")
        self.btn_region = QPushButton("Region")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for b in (self.btn_full, self.btn_region):
            b.setObjectName("seg")
            b.setMinimumWidth(130)
            b.setCheckable(True)
            self.mode_group.addButton(b)
        self.btn_full.clicked.connect(lambda: self._set_mode("full"))
        self.btn_region.clicked.connect(lambda: self._set_mode("region"))
        cl.addLayout(h_row(self.btn_full, self.btn_region, stretch_end=True))

        self.monitor_row = QWidget()
        mr = QHBoxLayout(self.monitor_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.setSpacing(10)
        mr.addWidget(QLabel("Monitor"))
        self.monitor_combo = QComboBox()
        self.monitor_combo.setMinimumWidth(260)
        self.monitor_combo.currentIndexChanged.connect(self._monitor_changed)
        mr.addWidget(self.monitor_combo)
        mr.addStretch(1)
        cl.addWidget(self.monitor_row)

        self.region_row = QWidget()
        rr = QHBoxLayout(self.region_row)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(10)
        self.btn_select_region = QPushButton("Select region")
        self.btn_select_region.clicked.connect(self.pick_region_clicked)
        self.region_label = QLabel("")
        rr.addWidget(self.btn_select_region)
        rr.addWidget(self.region_label)
        rr.addStretch(1)
        cl.addWidget(self.region_row)

        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.start_recording)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.stop_recording)
        cl.addLayout(h_row(self.btn_start, self.btn_pause, self.btn_stop, stretch_end=True))

        self.status_dot = QLabel("\u25cf")
        self.status_text = QLabel("Idle")
        self.stats_label = muted("")
        self.stats_label.setWordWrap(False)
        cl.addLayout(h_row(self.status_dot, self.status_text, 16, self.stats_label, stretch_end=True))
        lay.addWidget(card)

        card2, c2 = make_card("Quick scan", "Press the hotkey, drag a region, and it is read once. Do it again for each scan.")
        self.btn_oneshot = QPushButton("Scan region once")
        self.btn_oneshot.clicked.connect(self.one_shot)
        self.hotkey_hint = muted("")
        c2.addLayout(h_row(self.btn_oneshot, self.hotkey_hint, stretch_end=True))
        lay.addWidget(card2)

        card3, c3 = make_card("Activity")
        self.last_text = QPlainTextEdit()
        self.last_text.setReadOnly(True)
        self.last_text.setPlaceholderText("The last text that was read appears here.")
        self.last_text.setFixedHeight(96)
        c3.addWidget(self.last_text)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(400)
        self.log_view.setMinimumHeight(120)
        c3.addWidget(self.log_view, 1)
        btn_file = QPushButton("Open output file")
        btn_file.clicked.connect(self.open_output_file)
        btn_dir = QPushButton("Open folder")
        btn_dir.clicked.connect(self.open_output_folder)
        c3.addLayout(h_row(btn_file, btn_dir, stretch_end=True))
        lay.addWidget(card3, 1)
        return scroll_wrap(page)

    # -- AI keys page
    def _build_keys_page(self):
        page, lay = self._page()
        h, s = page_header("AI Keys", "Pick the provider that reads your screen. Free tiers are enough for personal use.")
        lay.addWidget(h)
        lay.addWidget(s)

        self.fallback_chk = QCheckBox("If the active provider fails, automatically try the other providers marked as fallback")
        self.fallback_chk.setChecked(bool(self.d["fallback"]))
        self.fallback_chk.toggled.connect(lambda v: self.set_cfg("fallback", bool(v)))
        lay.addWidget(self.fallback_chk)
        lay.addWidget(muted("Tip: paste several keys (comma separated) into one provider and they are used in turn, "
                            "which stretches free-tier rate limits. Screenshots are sent to the provider you choose."))

        self.active_group = QButtonGroup(self)
        self.active_group.setExclusive(True)
        for pdef in PROVIDERS:
            card = ProviderCard(self, pdef)
            self.cards[pdef["id"]] = card
            self.active_group.addButton(card.active)
            card.active.setChecked(self.d["active_provider"] == pdef["id"])
            card.active.toggled.connect(lambda on, pid=pdef["id"]: on and self.set_cfg("active_provider", pid))
            lay.addWidget(card)
        lay.addStretch(1)
        return scroll_wrap(page)

    # -- settings page
    def _bind_check(self, chk: QCheckBox, key: str):
        chk.setChecked(bool(self.d[key]))
        chk.toggled.connect(lambda v, k=key: self.set_cfg(k, bool(v)))

    def _bind_spin(self, spin: QSpinBox, key: str):
        spin.setValue(int(self.d[key]))
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda v, k=key: self.set_cfg(k, int(v)))

    def _build_settings_page(self):
        page, lay = self._page()
        h, s = page_header("Settings", "Everything here is saved automatically on this computer.")
        lay.addWidget(h)
        lay.addWidget(s)

        # output
        card, cl = make_card("Output file", "Text is appended to this file, one line per line of text.")
        self.out_edit = QLineEdit(self.d["output_file"])
        self.out_edit.editingFinished.connect(self._output_edited)
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self.browse_output)
        cl.addLayout(h_row(self.out_edit, btn_browse))
        self.out_edit.setMinimumWidth(320)
        c_ts = QCheckBox("Add a timestamp to every saved line")
        c_dup = QCheckBox("Skip text that is identical to the previous capture")
        c_new = QCheckBox("Only save lines not already saved in this session")
        for chk, key in ((c_ts, "timestamps"), (c_dup, "skip_duplicates"), (c_new, "only_new_lines")):
            self._bind_check(chk, key)
            cl.addWidget(chk)
        lay.addWidget(card)

        # recording
        card, cl = make_card("Recording", "How often the screen is read while recording.")
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(250, 600000)
        self.spin_interval.setSingleStep(250)
        self.spin_interval.setSuffix(" ms")
        self.spin_interval.setMinimumWidth(130)
        self._bind_spin(self.spin_interval, "interval_ms")
        cl.addLayout(h_row(QLabel("Delay between captures"), self.spin_interval, stretch_end=True))
        cl.addWidget(muted("Free tiers allow roughly 10 to 30 requests per minute. Keep this at 3000 ms or more unless you rotate several keys."))
        c_unch = QCheckBox("Skip the AI call when the screen has not changed (saves quota)")
        c_hide = QCheckBox("Hide this window while selecting a region (turn off on tiling window managers)")
        for chk, key in ((c_unch, "skip_unchanged"), (c_hide, "hide_on_select")):
            self._bind_check(chk, key)
            cl.addWidget(chk)
        self.spin_side = QSpinBox()
        self.spin_side.setRange(640, 8192)
        self.spin_side.setSingleStep(128)
        self.spin_side.setSuffix(" px")
        self.spin_side.setMinimumWidth(130)
        self._bind_spin(self.spin_side, "max_side")
        cl.addLayout(h_row(QLabel("Largest image side sent to the AI"), self.spin_side, stretch_end=True))
        lay.addWidget(card)

        # ocr
        card, cl = make_card("Text recognition", "Hints that make the AI more accurate.")
        self.lang_combo = QComboBox()
        self.lang_combo.setEditable(True)
        self.lang_combo.addItems(LANGUAGES)
        self.lang_combo.setCurrentText(self.d["language"])
        self.lang_combo.setMinimumWidth(220)
        self.lang_combo.currentTextChanged.connect(lambda t: self.set_cfg("language", t.strip()))
        cl.addLayout(h_row(QLabel("Language of the text"), self.lang_combo, stretch_end=True))
        self.extra_edit = QLineEdit(self.d["extra_prompt"])
        self.extra_edit.setPlaceholderText("Optional extra instructions, e.g. 'only read the subtitles at the bottom'")
        self.extra_edit.textChanged.connect(lambda t: self.set_cfg("extra_prompt", t.strip()))
        cl.addWidget(self.extra_edit)
        lay.addWidget(card)

        # hotkeys
        card, cl = make_card("Hotkeys", "Click a box and press the keys you want. Clear the box to disable a hotkey.")
        self.hk_edits = {}
        for action in ("region", "toggle", "pause"):
            edit = QKeySequenceEdit()
            if hasattr(edit, "setMaximumSequenceLength"):
                edit.setMaximumSequenceLength(1)
            if hasattr(edit, "setClearButtonEnabled"):
                edit.setClearButtonEnabled(True)
            edit.setKeySequence(QKeySequence.fromString(self.d["hotkeys"].get(action, ""), QKeySequence.SequenceFormat.PortableText))
            edit.setMinimumWidth(220)
            edit.editingFinished.connect(lambda a=action: self._hotkey_edited(a))
            self.hk_edits[action] = edit
            lab = QLabel(HOTKEY_LABELS[action])
            lab.setMinimumWidth(170)
            cl.addLayout(h_row(lab, edit, stretch_end=True))
        self.hk_status = QLabel("")
        self.hk_status.setWordWrap(True)
        cl.addWidget(self.hk_status)
        lay.addWidget(card)

        # compositor binds (Linux)
        self.bind_card = None
        if IS_LINUX:
            card, cl = make_card("Hyprland / compositor binds",
                                 "On Wayland the compositor owns global keys. Paste these into your config; they trigger the running app.")
            self.syntax_combo = QComboBox()
            self.syntax_combo.addItem("Hyprland Lua (0.55+)", "lua")
            self.syntax_combo.addItem("Hyprland hyprlang (hyprland.conf)", "conf")
            cur = self.d["hypr_syntax"] if self.d["hypr_syntax"] in ("lua", "conf") else detect_hypr_syntax()
            self.syntax_combo.setCurrentIndex(0 if cur == "lua" else 1)
            self.syntax_combo.currentIndexChanged.connect(self._syntax_changed)
            self.snippet = QPlainTextEdit()
            self.snippet.setReadOnly(True)
            self.snippet.setFixedHeight(190)
            self.snippet.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            btn_copy = QPushButton("Copy")
            btn_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.snippet.toPlainText()))
            cl.addLayout(h_row(self.syntax_combo, btn_copy, stretch_end=True))
            cl.addWidget(self.snippet)
            lay.addWidget(card)
            self.bind_card = card
            self.refresh_snippet()

        # general
        card, cl = make_card("General")
        self.chk_tray = QCheckBox("Keep running in the system tray when the window is closed")
        self._bind_check(self.chk_tray, "tray_on_close")
        if self.tray is None:
            self.chk_tray.setEnabled(False)
        c_notif = QCheckBox("Show a notification after a hotkey scan")
        self._bind_check(c_notif, "notifications")
        cl.addWidget(self.chk_tray)
        cl.addWidget(c_notif)
        lay.addWidget(card)
        lay.addStretch(1)
        return scroll_wrap(page)

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            if hasattr(self, "chk_tray"):
                self.chk_tray.setEnabled(False)
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        for text, fn in (("Show window", lambda: self.on_command("show")),
                         ("Scan region once", self.one_shot),
                         ("Start / stop recording", lambda: self.on_command("toggle")),
                         ("Quit", self.quit_app)):
            act = QAction(text, menu)
            act.triggered.connect(lambda _c=False, f=fn: f())
            menu.addAction(act)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.on_command("show")
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()
        if hasattr(self, "chk_tray"):
            self.chk_tray.setEnabled(True)

    # ---- settings handlers ------------------------------------------------------
    def _output_edited(self):
        self.set_cfg("output_file", self.out_edit.text().strip() or default_config()["output_file"])
        self.out_edit.setText(self.d["output_file"])

    def browse_output(self):
        start = os.path.expanduser(self.d["output_file"])
        path, _ = QFileDialog.getSaveFileName(self, "Choose the output text file", start,
                                              "Text files (*.txt);;All files (*)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.out_edit.setText(path)
            self._output_edited()

    def open_output_file(self):
        p = Path(os.path.expanduser(self.d["output_file"]))
        target = p if p.exists() else p.parent
        if not target.exists():
            self.add_log("warn", "Nothing saved yet.")
            return
        self.open_url(QUrl.fromLocalFile(str(target)).toString())

    def open_output_folder(self):
        p = Path(os.path.expanduser(self.d["output_file"])).parent
        if p.exists():
            self.open_url(QUrl.fromLocalFile(str(p)).toString())
        else:
            self.add_log("warn", "The output folder does not exist yet.")

    def _hotkey_edited(self, action: str):
        edit = self.hk_edits[action]
        seq = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        err = validate_hotkey(seq) if seq else None
        if not err and seq and seq in [v for k, v in self.d["hotkeys"].items() if k != action and v]:
            err = "already used by another action"
        if err:
            self.hk_status.setStyleSheet("color: #ff6b70;")
            self.hk_status.setText(f"Not saved: {err}.")
            edit.setKeySequence(QKeySequence.fromString(self.d["hotkeys"].get(action, ""), QKeySequence.SequenceFormat.PortableText))
            return
        self.d["hotkeys"][action] = seq
        self.save_soon()
        self.apply_hotkeys()
        self.refresh_snippet()

    def apply_hotkeys(self):
        ok, msg = self.hotkeys.apply(self.d["hotkeys"])
        self.hk_status.setStyleSheet(f"color: {'#7ee2a8' if ok else '#f5b942'};")
        self.hk_status.setText(msg)
        hk = self.d["hotkeys"].get("region") or "(not set)"
        self.hotkey_hint.setText(f"Hotkey: {hk}")

    def _syntax_changed(self):
        self.set_cfg("hypr_syntax", self.syntax_combo.currentData())
        self.refresh_snippet()

    def refresh_snippet(self):
        if self.bind_card is None:
            return
        self.snippet.setPlainText(hypr_snippet(self.d["hotkeys"], self.syntax_combo.currentData()))

    # ---- provider card callbacks ------------------------------------------------
    def on_provider_edit(self, card: ProviderCard):
        self.d["providers"][card.pdef["id"]] = {**self.d["providers"][card.pdef["id"]], **card.snapshot()}
        self.save_soon()

    def fetch_models(self, card: ProviderCard):
        pdef, snap, key = card.pdef, card.snapshot(), card.first_key()
        card.set_status("Fetching models...")

        def work():
            try:
                self.bus.models_ready.emit(pdef["id"], fetch_models(pdef, snap, key), "")
            except Exception as exc:
                self.bus.models_ready.emit(pdef["id"], None, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _on_models_ready(self, pid: str, models, err: str):
        card = self.cards[pid]
        if err:
            card.set_status(err, "err")
        elif not models:
            card.set_status("No models found. You can still type a model name.", "warn")
        else:
            card.set_models(models)
            card.set_status(f"{len(models)} models loaded. Pick one from the Model box.", "ok")

    def test_provider(self, card: ProviderCard):
        pdef, snap, key = card.pdef, card.snapshot(), card.first_key()
        if pdef["needs_key"] and not key:
            card.set_status("Enter an API key first.", "err")
            return
        card.set_status("Testing with a sample image...")
        cfg_copy = dict(self.d)

        def work():
            try:
                text = run_test(pdef, snap, key, cfg_copy)
                ok = "TEST" in text.upper()
                self.bus.test_done.emit(pdef["id"], ok, text or "(empty reply)")
            except Exception as exc:
                self.bus.test_done.emit(pdef["id"], False, f"ERROR:{exc}")
        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, pid: str, ok: bool, text: str):
        card = self.cards[pid]
        if text.startswith("ERROR:"):
            card.set_status(text[6:], "err")
        elif ok:
            card.set_status(f"Works. The model read: {text.replace(chr(10), ' / ')}", "ok")
        else:
            card.set_status(f"The API answered but did not read the sample correctly: {text[:120]}", "warn")

    # ---- mode / monitor -----------------------------------------------------------
    def _set_mode(self, mode: str):
        self.set_cfg("mode", mode)
        self._apply_mode_ui()

    def _apply_mode_ui(self):
        full = self.d["mode"] == "full"
        self.btn_full.setChecked(full)
        self.btn_region.setChecked(not full)
        self.monitor_row.setVisible(full)
        self.region_row.setVisible(not full)
        if not self.capturer.region_ok(self.d["region"]):
            self.d["region"] = None
        self.region_label.setText(describe_region(self.d["region"]))

    def refresh_monitors(self):
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        items = self.capturer.list_monitors()
        for label, key in items:
            self.monitor_combo.addItem(label, key)
        idx = self.monitor_combo.findData(self.d["monitor"])
        if idx < 0:
            idx = 0
            self.d["monitor"] = "auto"
        self.monitor_combo.setCurrentIndex(idx)
        self.monitor_combo.blockSignals(False)

    def _monitor_changed(self, _i):
        key = self.monitor_combo.currentData()
        if key is not None:
            self.set_cfg("monitor", key)

    # ---- state ----------------------------------------------------------------------
    def _set_state(self, state: str):
        self.state = state
        idle = state == "idle"
        self.btn_start.setEnabled(idle)
        self.btn_pause.setEnabled(not idle)
        self.btn_stop.setEnabled(not idle)
        self.btn_pause.setText("Resume" if state == "paused" else "Pause")
        for w in (self.btn_full, self.btn_region, self.monitor_combo, self.btn_select_region):
            w.setEnabled(idle)
        color, text = {"idle": ("#6b6b73", "Idle"), "recording": ("#ff5a5f", "Recording"),
                       "paused": ("#f5b942", "Paused")}[state]
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.status_text.setText(text)
        self.side_status.setText(f"\u25cf {text}")
        self.side_status.setStyleSheet(f"color: {color};")
        self.stats_label.setText(f"{self.captures} scans \u00b7 {self.lines_total} lines saved this run")

    # ---- recording control ----------------------------------------------------------
    def _ready(self, need_region: bool) -> bool:
        prob = self.capturer.problem(need_region=need_region)
        if prob:
            self.add_log("err", prob)
            return False
        if not self.ocr.chain(self.d):
            self.add_log("err", "No AI provider is ready. Add a key in the AI Keys tab.")
            self.show_page(1)
            return False
        return True

    def start_recording(self):
        if self.rec is not None or self._selecting:
            return
        region_mode = self.d["mode"] == "region"
        if not self._ready(need_region=region_mode and not self.capturer.region_ok(self.d["region"])):
            return
        if region_mode:
            if self.capturer.region_ok(self.d["region"]):
                self._begin("region", self.d["region"])
            else:
                self.select_region(self._region_then_start)
        else:
            self._begin("full", None)

    def _region_then_start(self, region, _img):
        if region is None:
            return
        self._store_region(region)
        self._begin("region", region)

    def _begin(self, mode: str, region):
        self.sink.reset_session()
        self.rec = Recorder(self, mode, region)
        self.rec.start()
        self._set_state("recording")
        what = "full screen" if mode == "full" else f"region {describe_region(region)}"
        self.add_log("ok", f"Recording started ({what}), every {self.d['interval_ms']} ms.")

    def stop_recording(self):
        if self.rec is None:
            return
        self.rec.stop()
        self.rec = None
        self._set_state("idle")
        self.add_log("info", "Recording stopped.")

    def toggle_pause(self):
        if self.rec is None:
            return
        if self.rec.paused:
            self.rec.resume()
            self._set_state("recording")
            self.add_log("info", "Resumed.")
        else:
            self.rec.pause()
            self._set_state("paused")
            self.add_log("info", "Paused.")

    def _on_auto_stopped(self, rec):
        if rec is self.rec:
            self.rec = None
            self._set_state("idle")
            self.notify(APP_NAME, "Recording stopped after repeated errors.")

    # ---- region selection -----------------------------------------------------------
    def pick_region_clicked(self):
        prob = self.capturer.problem(need_region=True)
        if prob:
            self.add_log("err", prob)
            return
        self.select_region(lambda region, _img: self._store_region(region) if region else None)

    def _store_region(self, region):
        self.d["region"] = region
        self.save_soon()
        self.region_label.setText(describe_region(region))
        self.add_log("ok", f"Region set: {describe_region(region)}")

    def select_region(self, callback):
        """Calls callback(region, image) on the GUI thread; both None if cancelled."""
        if self._selecting:
            return
        self._selecting = True
        self._select_cb = callback
        hide = bool(self.d["hide_on_select"]) and self.isVisible()
        self._restore_after = hide
        if hide:
            self.hide()
        delay = 0.3 if hide else 0.0
        if self.capturer.wayland:
            threading.Thread(target=self._select_worker_wayland, args=(delay,), daemon=True).start()
        else:
            QTimer.singleShot(int(max(delay, 0.03) * 1000), self._start_overlay)

    def _select_worker_wayland(self, delay: float):
        time.sleep(delay)
        try:
            region = self.capturer.select_region_wayland()
            img = self.capturer.grab_region(region) if region else None
            self.bus.select_done.emit((region, img, None))
        except Exception as exc:
            self.bus.select_done.emit((None, None, str(exc)))

    def _start_overlay(self):
        try:
            shots = self.capturer.grab_all_monitors()
            self._overlay = OverlayManager(shots, QGuiApplication.screens())
            self._overlay.finished.connect(self._on_overlay_finished)
            self._overlay.start()
        except Exception as exc:
            self._on_select_done((None, None, f"Region selector failed: {exc}"))

    def _on_overlay_finished(self, payload):
        self._overlay = None
        if payload is None:
            self._on_select_done((None, None, None))
        else:
            self._on_select_done((payload[0], payload[1], None))

    def _on_select_done(self, payload):
        region, img, err = payload
        self._selecting = False
        if self._restore_after:
            self._restore_after = False
            self.showNormal()
            self.raise_()
            self.activateWindow()
        cb, self._select_cb = self._select_cb, None
        if err:
            self.add_log("err", err)
        elif region is None:
            self.add_log("info", "Selection cancelled.")
        if cb:
            cb(None if err else region, None if err else img)

    # ---- one-shot scan -----------------------------------------------------------------
    def one_shot(self):
        if self._oneshot_busy or self._selecting:
            return
        if not self._ready(need_region=True):
            return
        self._oneshot_busy = True
        self.select_region(self._oneshot_selected)

    def _oneshot_selected(self, region, img):
        if region is None or img is None:
            self._oneshot_busy = False
            return
        self.add_log("info", "Reading region...")
        threading.Thread(target=self._oneshot_worker, args=(img,), daemon=True).start()

    def _oneshot_worker(self, img):
        try:
            text, written, provider = self.process_image(img, force=True)
            self.bus.result.emit(text, written, provider, "scan")
        except Exception as exc:
            self.bus.log.emit("err", str(exc))
            self.bus.notify.emit(APP_NAME, f"Scan failed: {str(exc)[:120]}")
        finally:
            self._oneshot_busy = False

    # ---- shared pipeline -----------------------------------------------------------------
    def process_image(self, img, force: bool = False):
        data, mime = encode_image(img, self.d["max_side"])
        text, provider = self.ocr.recognize(data, mime)
        written = self.sink.write(text, force=force) if text else 0
        return text, written, provider

    def on_result(self, text: str, lines: int, provider: str, source: str):
        self.captures += 1
        self.lines_total += lines
        if text.strip():
            self.last_text.setPlainText(text)
            if lines:
                self.add_log("ok", f"Saved {lines} line{'s' if lines != 1 else ''} ({provider})")
            else:
                self.add_log("info", f"Same text as before, not saved ({provider})")
        else:
            self.add_log("info", f"No text found ({provider})")
        self.stats_label.setText(f"{self.captures} scans \u00b7 {self.lines_total} lines saved this run")
        if source == "scan":
            self.notify("Scanned" if text.strip() else "No text found", text.strip()[:200] or "Nothing readable in that region.")

    # ---- commands (hotkeys, tray, --trigger) -------------------------------------------------
    def on_command(self, cmd: str):
        if cmd == "show":
            self.showNormal()
            self.raise_()
            self.activateWindow()
        elif cmd == "region":
            self.one_shot()
        elif cmd == "toggle":
            if self.rec is not None:
                self.stop_recording()
                self.notify(APP_NAME, "Recording stopped")
            else:
                self.start_recording()
                if self.rec is not None:
                    self.notify(APP_NAME, "Recording started")
        elif cmd == "start":
            self.start_recording()
        elif cmd == "stop":
            self.stop_recording()
        elif cmd == "pause":
            self.toggle_pause()

    # ---- lifecycle -------------------------------------------------------------------------------
    def quit_app(self):
        self._really_quit = True
        self.close()

    def closeEvent(self, event):
        if (self.d["tray_on_close"] and self.tray is not None and self.tray.isVisible()
                and not self._really_quit):
            event.ignore()
            self.hide()
            return
        if self.rec is not None:
            self.rec.stop()
        self.hotkeys.stop()
        if self.ipc is not None:
            self.ipc.close()
        self._save_timer.stop()
        self.cfg.save()
        event.accept()
        QApplication.quit()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main() -> int:
    if send_command("show") is not None:
        print(f"{APP_NAME} is already running; brought its window forward.")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # overlays and tray must not end the app
    QGuiApplication.setDesktopFileName(APP_ID)  # Wayland app_id, handy for window rules
    app.setStyle("Fusion")
    try:
        check, arrow = make_assets()
        app.setStyleSheet(QSS.replace("@CHECK@", check).replace("@ARROW@", arrow))
    except Exception:
        app.setStyleSheet(QSS.replace("image: url(\"@CHECK@\");", "").replace("image: url(\"@ARROW@\");", ""))

    cfg = Config()
    bus = Bus()
    get_cfg = lambda: cfg.data  # noqa: E731
    ocr, sink, capturer = OCRClient(get_cfg), TextSink(get_cfg), Capturer()

    ipc = IPCServer(lambda cmd: bus.command.emit(cmd))
    ipc_ok = ipc.bind()
    win = MainWindow(cfg, bus, capturer, ocr, sink, ipc_ok)
    if ipc_ok:
        win.ipc = ipc
        ipc.start()
    win.show()
    code = app.exec()
    cfg.save()
    return code


if __name__ == "__main__":
    sys.exit(main())
