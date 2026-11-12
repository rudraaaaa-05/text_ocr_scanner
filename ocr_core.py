from __future__ import annotations

import base64
import html
import importlib.util
import io
import json
import logging
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
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

from ocr_secrets import SecretStoreError, default_store

APP_NAME = "OCR Scanner"
APP_ID = "ocr-scanner"
VERSION = "0.4"
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
# compositor bindings keep calling `ocr_scanner.py --trigger ...` (same as before the daemon split)
SCRIPT_PATH = str(Path(__file__).resolve().with_name("ocr_scanner.py"))
# region  = pick a NEW region and scan it once
# current = scan what is already configured (stored region, or the configured screen) without a selector
VALID_COMMANDS = ("region", "current", "toggle", "start", "stop", "pause", "show")
MIN_SELECTION_PX = 8  # smaller drags are treated as accidental clicks and never reach OCR
log = logging.getLogger("ocr_scanner")


#platform detection helpers
#arch linuxのHyprLandで作ったから他の環境で動くかはわからない、
#きっと大丈夫だと思う

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


# Session detection. Used for hotkey strategy and the setup help; capture has its own checks (is_wayland above).

_DESKTOP_NAMES = (  # (substring of XDG_CURRENT_DESKTOP / DESKTOP_SESSION, canonical name)
    ("hyprland", "hyprland"), ("gnome", "gnome"), ("ubuntu", "gnome"), ("kde", "kde"), ("plasma", "kde"),
    ("cinnamon", "cinnamon"), ("mate", "mate"), ("xfce", "xfce"), ("lxqt", "lxqt"), ("budgie", "budgie"),
    ("cosmic", "cosmic"), ("pantheon", "pantheon"), ("deepin", "deepin"), ("sway", "sway"), ("niri", "niri"),
    ("river", "river"), ("wayfire", "wayfire"), ("labwc", "labwc"), ("i3", "i3"),
)


def detect_desktop(env=None) -> str:
    """Canonical desktop / compositor name ('hyprland', 'gnome', 'kde', ...) or 'unknown'. Linux only."""
    env = os.environ if env is None else env
    if not IS_LINUX:
        return "unknown"
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    names = [n.strip().lower() for n in re.split(r"[:;]", env.get("XDG_CURRENT_DESKTOP", "")) if n.strip()]
    names.append(env.get("DESKTOP_SESSION", "").strip().lower())
    for name in names:
        for needle, canon in _DESKTOP_NAMES:
            if needle in name:
                return canon
    if env.get("SWAYSOCK"):
        return "sway"
    return "unknown"


def detect_session_type(env=None) -> str:
    """'wayland', 'x11', 'windows', 'macos' or 'unknown' (a tty or a bare ssh login)."""
    env = os.environ if env is None else env
    if IS_WINDOWS:
        return "windows"
    if IS_MAC:
        return "macos"
    stype = env.get("XDG_SESSION_TYPE", "").lower()
    if stype == "wayland" or env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if stype == "x11" or env.get("DISPLAY"):
        return "x11"
    return "unknown"


def distro_info(path: str = "/etc/os-release") -> dict:
    """{'id': 'ubuntu', 'like': ['debian'], 'name': 'Ubuntu 24.04 LTS'} from os-release; {} if unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip().strip('"').strip("'")
    return {"id": fields.get("ID", "").lower(), "like": fields.get("ID_LIKE", "").lower().split(),
            "name": fields.get("PRETTY_NAME") or fields.get("NAME", "")}


def detect_session(env=None) -> dict:
    """One dict describing where we run, for logs, `status` output and hotkey decisions."""
    os_name = "windows" if IS_WINDOWS else "macos" if IS_MAC else "linux" if IS_LINUX else sys.platform
    info = {"os": os_name, "session_type": detect_session_type(env), "desktop": detect_desktop(env)}
    if IS_LINUX:
        d = distro_info()
        info["distro"], info["distro_name"] = d.get("id", ""), d.get("name", "")
    return info


def config_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "OCRScanner"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "OCRScanner"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / APP_ID


#third party imports (no Qt here: the daemon must run without PySide6)
try:
    import requests
    from PIL import Image, ImageChops, ImageDraw, ImageStat
except ImportError as _exc:  # pragma: no cover
    sys.stderr.write(
        f"Missing dependency: {getattr(_exc, 'name', None) or _exc}\n"
        "Install everything with:  pip install -r requirements.txt   (or just use run.sh / run.bat)\n"
    )
    sys.exit(1)


PROVIDERS = [
    dict(id="local", name="Local OCR (offline)", kind="local", free="Local", needs_key=False,
         base_url="", edit_base=False, key_url="", key_label="",
         model="small", fallback=True,
         note="Runs on this computer: no key, no upload, no quota. A classic text detector plus recogniser, "
              "so it reads what is printed and drops what it is unsure of instead of making text up."),
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

HOTKEY_ORDER = ("region", "current", "toggle", "pause")
HOTKEY_DEFAULTS = {"region": "Ctrl+Alt+O", "current": "Ctrl+Alt+S", "toggle": "Ctrl+Alt+R", "pause": "Ctrl+Alt+P"}
HOTKEY_LABELS = {
    "region": "Select a new region and scan it once",
    "current": "Scan the stored region / configured screen",
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
        "active_provider": "local",
        "fallback": True,
        "local_min_conf": 70,
        "local_max_side": 1280,
        "providers": {
            p["id"]: {"keys": "", "model": p["model"], "base_url": p["base_url"], "fallback": p["fallback"]}
            for p in PROVIDERS
        },
        "tray_on_close": False,
        "notifications": True,
        "hypr_syntax": "auto",
        # Wayland hotkeys: "auto" (portal, except on Hyprland which keeps compositor binds), "portal", "manual"
        "wayland_hotkeys": "auto",
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
    """config.json plus API keys.

    API keys live in the OS credential store (Windows Credential Manager / Linux Secret Service), not in
    the JSON file. `data["providers"][id]["keys"]` is still where the rest of the app reads them: load()
    fills it from the store and save() writes it back there, so no other code had to change.

    Only the GUI creates `Config(writer=True)`. A writer migrates plain-text keys it finds in config.json:
    store, read back, compare, and only then remove the plain-text copy. The daemon never writes the file;
    until the GUI has migrated, it simply keeps using the keys it finds in the JSON.
    If the credential store is unavailable, keys stay in config.json (mode 600) as before and
    `secret_status` says so. `secret_status` never contains a key.
    """

    def __init__(self, writer: bool = False, secrets=None):
        self.path = config_dir() / "config.json"
        self.data = default_config()
        self._lock = threading.Lock()
        self.writer = writer
        self.secrets = secrets if secrets is not None else default_store()
        self.secret_status = ""     # "" = fine, otherwise a readable warning (no secrets in it)
        self.migrated = []          # provider ids whose plain-text key was moved this load
        self._synced = {}           # provider id -> key text last confirmed to be in the credential store
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
        if self.data["wayland_hotkeys"] not in ("auto", "portal", "manual"):
            self.data["wayland_hotkeys"] = "auto"
        self._load_secrets()

    # API keys

    def _load_secrets(self):
        self.secret_status, self.migrated = "", []
        self._synced = {}
        why = self.secrets.problem()
        providers = self.data["providers"]
        if why:
            if any(str(pc.get("keys", "")).strip() for pc in providers.values()):
                self.secret_status = (f"Secure key storage is unavailable ({why}). "
                                      f"API keys stay in {self.path.name} (not encrypted).")
            return
        problems, moved = [], []
        for pid, pc in providers.items():
            in_file = str(pc.get("keys", ""))
            try:
                stored = self.secrets.get(pid)
            except SecretStoreError as exc:
                problems.append(str(exc))
                continue
            if in_file.strip():
                if not self.writer:
                    continue  # the daemon uses the file's copy until the GUI migrates it
                if stored != in_file:
                    try:
                        self.secrets.set_verified(pid, in_file)
                    except SecretStoreError as exc:
                        problems.append(str(exc))
                        continue
                self._synced[pid] = in_file
                moved.append(pid)
            else:
                pc["keys"] = stored or ""
                if stored:
                    self._synced[pid] = stored
        if moved:  # every moved key was read back and compared above; only now is the plain text removed
            out = json.loads(json.dumps(self.data))
            for pid in moved:
                out["providers"][pid]["keys"] = ""
            err = self._write_file(out)
            if err:
                problems.append(f"The keys were copied to the credential store, but the plain-text copy could not be removed. {err}")
            else:
                self.migrated = moved
                log.info("Moved API keys for %s from %s to the credential store", ", ".join(moved), self.path.name)
        if problems:
            self.secret_status = " ".join(problems)

    def _store_secrets(self, out: dict):
        """Write the keys of self.data to the credential store and blank them in `out` (the JSON payload).
        A key that could not be stored stays in `out`, so it is never lost."""
        why = self.secrets.problem()
        if why:
            if any(str(pc.get("keys", "")).strip() for pc in self.data["providers"].values()):
                self.secret_status = (f"Secure key storage is unavailable ({why}). "
                                      f"API keys are saved in {self.path.name} (not encrypted).")
            return
        problems = []
        for pid, pc in self.data["providers"].items():
            value = str(pc.get("keys", ""))
            if value.strip():
                if self._synced.get(pid) != value:
                    try:
                        self.secrets.set_verified(pid, value)
                    except SecretStoreError as exc:
                        problems.append(str(exc))
                        continue
                    self._synced[pid] = value
                out["providers"][pid]["keys"] = ""
            elif pid in self._synced:
                try:
                    self.secrets.delete(pid)
                except SecretStoreError as exc:
                    problems.append(str(exc))
                    continue
                self._synced.pop(pid, None)
        self.secret_status = " ".join(problems)

    # file

    def save(self):
        """Write the config. Returns None on success, otherwise a readable error (never silent)."""
        with self._lock:
            out = json.loads(json.dumps(self.data))
            self._store_secrets(out)
            return self._write_file(out)

    def _write_file(self, payload_data: dict):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            payload = json.dumps(payload_data, indent=2, ensure_ascii=False)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
            if not IS_WINDOWS:
                os.chmod(self.path, 0o600)  # may still hold keys when the credential store is unavailable
        except OSError as exc:
            log.error("Could not save the config to %s: %s", self.path, exc)
            return f"Could not save settings to {self.path}: {exc.strerror or exc}"
        return None


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
    for action in HOTKEY_ORDER:
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
    for action in HOTKEY_ORDER:
        lines.append(f"  {HOTKEY_LABELS[action]:<46} {trigger_cmd(action)}")
    return "\n".join(lines)


def _bind_combo(seq: str, fmt: str):
    """Shortcut text for desktops other than Hyprland. fmt: 'gtk' (<Ctrl><Alt>o), 'kde' (Ctrl+Alt+O), 'i3' (Ctrl+Mod1+o)."""
    sp = split_seq(seq)
    if not sp:
        return None
    mods, key = sp
    k = key.lower() if len(key) == 1 else HYPR_KEYS.get(key, key)
    if fmt == "gtk":
        mm = {"Ctrl": "<Ctrl>", "Alt": "<Alt>", "Shift": "<Shift>", "Meta": "<Super>"}
        return "".join(mm[m] for m in mods) + k
    if fmt == "i3":
        mm = {"Ctrl": "Ctrl", "Alt": "Mod1", "Shift": "Shift", "Meta": "Mod4"}
        return "+".join([mm[m] for m in mods] + [k])
    return "+".join(mods + [key.upper() if len(key) == 1 else key])


def manual_bind_help(hotkeys: dict, desktop: str, hypr_syntax: str = "conf") -> str:
    """What to do by hand when the desktop portal can't register hotkeys. Hyprland keeps its exact snippet."""
    if desktop == "hyprland":
        return hypr_snippet(hotkeys, hypr_syntax)
    rows = [(a, hotkeys.get(a) or "") for a in HOTKEY_ORDER]
    lines = []
    if desktop == "gnome":
        lines += ["GNOME: Settings > Keyboard > View and Customize Shortcuts > Custom Shortcuts > Add.",
                  "Create one entry per line below (Name, Command, then press the shortcut):"]
        lines += [f"  {HOTKEY_LABELS[a]}: {_bind_combo(seq, 'gtk') or '(no key set)'}" for a, seq in rows]
    elif desktop == "kde":
        lines += ["KDE Plasma: System Settings > Keyboard > Shortcuts > Add New > Command or Script.",
                  "Create one entry per line below (command, then the shortcut):"]
        lines += [f"  {HOTKEY_LABELS[a]}: {_bind_combo(seq, 'kde') or '(no key set)'}" for a, seq in rows]
    elif desktop in ("sway", "i3"):
        lines.append(f"# add to your {desktop} config, then reload it")
        for a, seq in rows:
            combo = _bind_combo(seq, "i3")
            if combo:
                lines.append(f"bindsym {combo} exec {trigger_cmd(a)}  # {HOTKEY_LABELS[a]}")
    else:
        lines.append("Add a custom keyboard shortcut in your desktop's settings for each command below.")
    lines.append("")
    lines.append("Commands (they talk to the running app):")
    for a, _seq in rows:
        lines.append(f"  {HOTKEY_LABELS[a]:<46} {trigger_cmd(a)}")
    return "\n".join(lines)


#session output files
#every launch gets its own file, named from the configured output path + a timestamp.

class OutputError(Exception):
    pass


SESSION_STAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"


class SessionOutput:
    """One output file per application launch.

    The file name is derived from the configured output path (``ocr_output.txt`` becomes
    ``ocr_output_2026-01-31_14-05-09.txt``) and is reserved with exclusive creation, so an existing
    file, from an earlier launch or anything else, is never opened for writing. The path is fixed for
    the whole launch; changing the setting only affects the next launch.
    """

    def __init__(self, configured_path: str, clock=datetime.now):
        self.base = Path(os.path.expanduser(str(configured_path or "")))
        self._configured = str(configured_path or "")
        self.started = clock()
        self._path = None
        self._lock = threading.Lock()

    @property
    def path(self):
        """The active session file, or None if it has not been created yet."""
        return self._path

    def _parts(self):
        is_dir = self.base.is_dir() or self._configured.endswith(("/", "\\"))
        if is_dir or not self._configured.strip():
            folder = self.base if self._configured.strip() else Path.home()
            return folder, "ocr_output", ".txt"
        return self.base.parent, (self.base.stem or "ocr_output"), self.base.suffix

    def ensure(self) -> Path:
        with self._lock:
            if self._path is not None:
                return self._path
            folder, stem, suffix = self._parts()
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise OutputError(f"Cannot create the output folder {folder}: {exc.strerror or exc}") from exc
            stamp = self.started.strftime(SESSION_STAMP_FORMAT)
            for n in range(1, 1000):
                extra = "" if n == 1 else f"_{n}"
                candidate = folder / f"{stem}_{stamp}{extra}{suffix}"
                try:
                    with open(candidate, "x", encoding="utf-8"):
                        pass
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise OutputError(f"Cannot create the output file {candidate}: {exc.strerror or exc}") from exc
                self._path = candidate
                log.info("Session output file: %s", candidate)
                return candidate
            raise OutputError(f"Could not find a free session file name in {folder}.")


#capture screen shit

class CaptureError(Exception):
    pass


GEOM_RE = re.compile(r"(-?\d+),(-?\d+) (\d+)x(\d+)")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def monitor_layout(monitors) -> list:
    """Order independent fingerprint of the physical monitor layout."""
    return sorted([int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"])] for m in monitors)


def region_problem(region, wayland: bool, monitors=None):
    """None if the stored region is usable, otherwise a human readable reason.

    `monitors` is the current list of physical monitors (dicts with left/top/width/height). When given,
    the region must still lie on the desktop and the layout must match the one it was selected on.
    """
    if region is None:
        return "No region is stored yet. Select a region first."
    if not isinstance(region, dict):
        return "The stored region is not valid. Select a new region."
    kind = region.get("type")
    if kind == "geom":
        if not wayland:
            return "The stored region was selected on Wayland and cannot be used here. Select a new region."
        m = GEOM_RE.fullmatch(str(region.get("geom", "")))
        if not m:
            return "The stored region is malformed. Select a new region."
        if int(m.group(3)) <= 0 or int(m.group(4)) <= 0:
            return "The stored region is empty (zero size). Select a new region."
        return None
    if kind == "mss":
        if wayland:
            return "The stored region was not selected on Wayland and cannot be used here. Select a new region."
        vals = [region.get(k) for k in ("left", "top", "width", "height")]
        if not all(_is_int(v) for v in vals):
            return "The stored region is malformed. Select a new region."
        left, top, width, height = vals
        if width <= 0 or height <= 0:
            return f"The stored region has no area ({width}\u00d7{height}). Select a new region."
        if monitors:
            bl = min(m["left"] for m in monitors)
            bt = min(m["top"] for m in monitors)
            br = max(m["left"] + m["width"] for m in monitors)
            bb = max(m["top"] + m["height"] for m in monitors)
            if left < bl or top < bt or left + width > br or top + height > bb:
                return "The stored region is no longer on the screen (monitors changed). Select a new region."
            stored = region.get("layout")
            if stored is not None and stored != monitor_layout(monitors):
                return "The monitor layout changed since this region was selected. Select the region again."
        return None
    return "The stored region has an unknown type. Select a new region."


def _mss_module():
    """(mss module, tuple of its capture error types)."""
    try:
        import mss
    except ImportError:
        raise CaptureError("The 'mss' package is missing: pip install mss") from None
    try:
        from mss.exception import ScreenShotError
        return mss, (ScreenShotError, OSError, ValueError)
    except ImportError:
        return mss, (OSError, ValueError)


class Capturer:
    """Wayland (Hyprland, sway, ...) uses grim + slurp. Everything else uses mss + a Qt selector."""

    def __init__(self):
        self.wayland = is_wayland()
        self.hypr = is_hyprland()
        self._slurp = None
        self._slurp_lock = threading.Lock()

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
        except ImportError:
            return "The 'mss' package is missing: pip install mss"
        return None

    def physical_monitors(self) -> list:
        """Physical monitors as mss sees them (index 0, the virtual desktop, excluded)."""
        mss, errors = _mss_module()
        try:
            with mss.mss() as sct:
                return [dict(m) for m in sct.monitors[1:]]
        except errors as exc:
            raise CaptureError(f"Could not read the monitor layout: {exc}") from exc

    def region_error(self, region):
        """None if `region` can be captured right now, else the reason."""
        if self.wayland:
            return region_problem(region, True)
        try:
            monitors = self.physical_monitors()
        except CaptureError as exc:
            return str(exc)
        return region_problem(region, False, monitors)

    def region_ok(self, region) -> bool:
        return self.region_error(region) is None

    # monitors
    def _hypr_monitors(self) -> list:
        try:
            r = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5)
            data = json.loads(r.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise CaptureError(f"hyprctl failed: {exc}") from exc
        return [m for m in data if isinstance(m, dict) and m.get("name")]

    def list_monitors(self) -> list:
        items = [("Primary / focused monitor", "auto"), ("All monitors", "all")]
        if self.wayland:
            if self.hypr and shutil.which("hyprctl"):
                for m in self._hypr_monitors():
                    items.append((f"{m['name']} ({m.get('width', '?')}x{m.get('height', '?')})", m["name"]))
        else:
            for i, m in enumerate(self.physical_monitors(), 1):
                items.append((f"Monitor {i} ({m['width']}x{m['height']})", str(i)))
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
            raise CaptureError("grim is not installed (Arch: sudo pacman -S grim slurp).") from None
        except subprocess.TimeoutExpired:
            raise CaptureError("grim timed out.") from None
        if r.returncode != 0 or not r.stdout:
            msg = (r.stderr or b"").decode("utf-8", "ignore").strip()
            raise CaptureError(f"grim failed: {msg[:200] or 'no output'}")
        img = Image.open(io.BytesIO(r.stdout))
        img.load()
        return img.convert("RGB")

    def grab_full(self, key: str):
        """Full-screen mode: the configured monitor, or every monitor for "all"."""
        if self.wayland:
            args = []
            out = None
            if key == "auto" and self.hypr and shutil.which("hyprctl"):
                for m in self._hypr_monitors():
                    if m.get("focused"):
                        out = m["name"]
            elif key not in ("auto", "all"):
                out = key
            if out:
                args += ["-o", out]
            return self._grim(args)
        mss, errors = _mss_module()
        try:
            with mss.mss() as sct:
                if key == "all":
                    mon = sct.monitors[0]
                else:
                    idx = 1 if key == "auto" else int(key)
                    if not 1 <= idx < len(sct.monitors):
                        raise CaptureError(f"Monitor {key} is not connected. Choose another one on the Capture page.")
                    mon = sct.monitors[idx]
                shot = sct.grab(mon)
        except ValueError:
            raise CaptureError(f"Invalid monitor setting: {key!r}.") from None
        except errors as exc:
            raise CaptureError(f"Screen capture failed: {exc}") from exc
        return self._to_pil(shot)

    def grab_region(self, region):
        """Region mode: only the stored region. Never falls back to full screen."""
        err = self.region_error(region)
        if err:
            raise CaptureError(err)
        if region["type"] == "geom":
            return self._grim(["-g", region["geom"]])
        mss, errors = _mss_module()
        box = {k: int(region[k]) for k in ("left", "top", "width", "height")}
        try:
            with mss.mss() as sct:
                img = self._to_pil(sct.grab(box))
        except errors as exc:
            raise CaptureError(f"Screen capture failed: {exc}") from exc
        if not IS_MAC and img.size != (box["width"], box["height"]):  # macOS Retina returns 2x pixels
            raise CaptureError(f"Captured {img.size[0]}\u00d7{img.size[1]} px instead of "
                               f"{box['width']}\u00d7{box['height']} px. Select the region again.")
        return img

    def grab_all_monitors(self) -> list:
        mss, errors = _mss_module()
        shots = []
        try:
            with mss.mss() as sct:
                for m in sct.monitors[1:]:
                    shots.append((dict(m), self._to_pil(sct.grab(m))))
        except errors as exc:
            raise CaptureError(f"Screen capture failed: {exc}") from exc
        if not shots:
            raise CaptureError("No monitors were found to capture.")
        return shots

    def cancel_selection(self):
        """Stop a running slurp (used when the app quits mid selection)."""
        with self._slurp_lock:
            proc = self._slurp
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def select_region_wayland(self):
        """Blocking. Returns a region dict, or None if the user cancelled."""
        styled = ["slurp", "-d", "-b", "#00000066", "-c", "#ffffffff", "-w", "2"]
        last_err = ""
        for cmd in (styled, ["slurp"]):
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except FileNotFoundError:
                raise CaptureError("slurp is not installed (Arch: sudo pacman -S grim slurp).") from None
            with self._slurp_lock:
                self._slurp = proc
            try:
                out, err = proc.communicate(timeout=300)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return None
            finally:
                with self._slurp_lock:
                    self._slurp = None
            geom = (out or "").strip()
            if proc.returncode != 0 or not geom:
                err = (err or "").strip()
                if not err or "cancel" in err.lower() or proc.returncode in (-15, -9):
                    return None
                last_err = err
                continue
            m = GEOM_RE.fullmatch(geom)
            if not m:
                raise CaptureError(f"slurp returned an unexpected selection: {geom[:60]!r}")
            if int(m.group(3)) < MIN_SELECTION_PX or int(m.group(4)) < MIN_SELECTION_PX:
                raise CaptureError(f"The selection was too small (minimum {MIN_SELECTION_PX}\u00d7{MIN_SELECTION_PX} px).")
            return {"type": "geom", "geom": geom}
        raise CaptureError(f"slurp failed: {last_err[:200]}")


def describe_region(r) -> str:
    if not r or not isinstance(r, dict):
        return "No region selected"
    if r.get("type") == "geom":
        m = GEOM_RE.fullmatch(str(r.get("geom", "")))
        if m:
            return f"{m.group(3)}\u00d7{m.group(4)} px at ({m.group(1)}, {m.group(2)})"
        return str(r.get("geom"))
    try:
        return f"{r['width']}\u00d7{r['height']} px at ({r['left']}, {r['top']})"
    except KeyError:
        return "No region selected"




# OCR VIA AI API PROVIDERS

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


def redact(text: str, *keys: str) -> str:
    """Hide API keys in text that may be shown or logged (some servers echo the rejected key back)."""
    for k in sorted({k for k in keys if k and len(k) >= 4}, key=len, reverse=True):
        text = text.replace(k, "***")
    return text


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
        raise OCRError(redact(f"HTTP {resp.status_code}: {_err_message(resp)}", key), skip_provider=resp.status_code == 404)
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
                    raise OCRError(redact(f"HTTP {r.status_code}: {_err_message(r)}", key))
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
            raise OCRError(redact(f"HTTP {r.status_code}: {_err_message(r)}", key))
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
    img = make_test_image()
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return clean_text(call_provider(pdef, pc, key, buf.getvalue(), "image/png", build_prompt(cfg)))

# added local OCR

LOCAL_INSTALL_HINT = "pip install rapidocr onnxruntime"
# languages that are supported by the rapidocr wheel, and don't require any extra downloads
LOCAL_LANG_WORDS = (
    "japanese", "english", "chinese", "german", "french", "spanish", "portuguese", "italian",
    "vietnamese", "dutch", "polish", "turkish", "swedish", "danish", "norwegian", "finnish",
    "czech", "indonesian", "romanian", "hungarian",
)


def make_test_image():
    img = Image.new("RGB", (110, 28), "white")
    ImageDraw.Draw(img).text((8, 8), "OCR TEST 123", fill="black")
    return img.resize((550, 140), Image.Resampling.NEAREST)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3000 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF


def assemble_lines(boxes, txts, scores, min_conf: float) -> str:
    """Turn detector boxes into lines in reading order.

    Boxes under min_conf are discarded outright. That is the anti-hallucination rule: a low-confidence
    guess is worse than a gap, so it is never written.
    """
    if boxes is None or txts is None or scores is None:
        return ""
    items = []
    for box, txt, sc in zip(boxes, txts, scores):
        txt = (txt or "").strip()
        if not txt or float(sc) < min_conf:
            continue
        xs = [float(pt[0]) for pt in box]
        ys = [float(pt[1]) for pt in box]
        items.append({"x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys), "t": txt})
    if not items:
        return ""
    items.sort(key=lambda b: (b["y0"] + b["y1"]) / 2)
    rows = []
    for it in items:
        cy, h = (it["y0"] + it["y1"]) / 2, max(1.0, it["y1"] - it["y0"])
        for row in rows:
            if abs(cy - row["cy"]) <= 0.5 * min(h, row["h"]):  # same visual line
                row["items"].append(it)
                n = len(row["items"])
                row["cy"] += (cy - row["cy"]) / n
                row["h"] += (h - row["h"]) / n
                break
        else:
            rows.append({"cy": cy, "h": h, "items": [it]})
    lines = []
    for row in sorted(rows, key=lambda r: r["cy"]):
        parts = sorted(row["items"], key=lambda b: b["x0"])
        line = parts[0]["t"]
        for prev, nxt in zip(parts, parts[1:]):
            gap = nxt["x0"] - prev["x1"]
            glue = _is_cjk(prev["t"][-1]) and _is_cjk(nxt["t"][0]) and gap < 0.6 * row["h"]
            line += ("" if glue else " ") + nxt["t"]
        lines.append(line)
    return "\n".join(lines)


class LocalOCR:
    """Offline OCR. Detects text lines, reads each one and scores it; low scores are dropped.

    Tuned on measurements, not guesses:
      * detector limit_type="max": the default upscales small crops to 736 px, which was ~25x slower on
        region captures with no accuracy gain. We cap the size ourselves instead.
      * angle classifier off: screen text is upright, and it only adds a chance of flipping short strings.
      * model "small" ships inside the rapidocr wheel, so the default needs no download.
    """
    VARIANTS = ("small", "medium")

    def __init__(self):
        self._engines = {}
        self._build_lock = threading.Lock()
        self._run_lock = threading.Lock()

    @staticmethod
    def problem():
        """None when usable, otherwise a human-readable reason."""
        if importlib.util.find_spec("rapidocr") is None or importlib.util.find_spec("onnxruntime") is None:
            return f"Local OCR is not installed. Run:  {LOCAL_INSTALL_HINT}"
        return None

    @staticmethod
    def supports(language: str) -> bool:
        lang = (language or "").strip().lower()
        return not lang or lang.startswith("auto") or any(w in lang for w in LOCAL_LANG_WORDS)

    @classmethod
    def variant(cls, cfg: dict) -> str:
        v = str(cfg["providers"]["local"].get("model", "small")).lower()
        return v if v in cls.VARIANTS else "small"

    def _engine(self, variant: str):
        eng = self._engines.get(variant)
        if eng is not None:
            return eng
        with self._build_lock:
            eng = self._engines.get(variant)
            if eng is not None:
                return eng
            err = self.problem()
            if err:
                raise OCRError(err, skip_provider=True)
            try:
                from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
            except Exception as exc:
                raise OCRError(f"Local OCR could not be imported: {exc}", skip_provider=True)
            mtype = ModelType.MEDIUM if variant == "medium" else ModelType.SMALL
            user_models = config_dir() / "models"
            # small is bundled in the wheel (root=None); anything else downloads into our own folder
            roots = [None, user_models] if variant == "small" else [user_models]
            last = None
            for root in roots:
                params = {
                    "Global.log_level": "error",
                    "Global.max_side_len": 8192,  # we resize first, so the engine must not shrink it again
                    "Global.use_cls": False,
                    "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": mtype, "Det.limit_type": "max",
                    "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.lang_type": LangRec.JAPAN, "Rec.model_type": mtype,
                }
                if root is not None:
                    root.mkdir(parents=True, exist_ok=True)
                    params["Global.model_root_dir"] = str(root)
                try:
                    eng = RapidOCR(params=params)
                    break
                except Exception as exc:
                    last = exc
            else:
                hint = " Switch to the Small model, or check your internet connection for the one-time download." \
                    if variant == "medium" else ""
                raise OCRError(f"Local OCR could not load the {variant} model ({str(last)[:140]}).{hint}",
                               skip_provider=True)
            self._engines[variant] = eng
            return eng

    def warmup(self, cfg: dict) -> int:
        t0 = time.perf_counter()
        eng = self._engine(self.variant(cfg))
        with self._run_lock:
            eng(make_test_image(), use_cls=False)
        return int((time.perf_counter() - t0) * 1000)

    def recognize(self, img, cfg: dict) -> str:
        lang = cfg.get("language", "")
        if not self.supports(lang):
            raise OCRError(f"Local OCR does not cover {lang}. Use Japanese, Chinese, English or a Latin-script "
                           "language, or switch to a cloud provider.", skip_provider=True)
        cap = min(4096, max(640, int(cfg.get("local_max_side", 1280))))
        conf = min(0.99, max(0.2, int(cfg.get("local_min_conf", 70)) / 100.0))
        rgb = img.convert("RGB")
        w, h = rgb.size
        if max(w, h) > cap:
            s = cap / max(w, h)
            rgb = rgb.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS)
        eng = self._engine(self.variant(cfg))
        try:
            with self._run_lock:
                res = eng(rgb, use_cls=False, text_score=conf)
        except Exception as exc:
            raise OCRError(f"local OCR failed ({exc.__class__.__name__}: {str(exc)[:120]})")
        return assemble_lines(getattr(res, "boxes", None), getattr(res, "txts", None),
                              getattr(res, "scores", None), conf)

    def selftest(self, cfg: dict):
        self.warmup(cfg)
        t0 = time.perf_counter()
        text = self.recognize(make_test_image(), {**cfg, "language": "English"})
        return text, int((time.perf_counter() - t0) * 1000)


class OCRClient:
    def __init__(self, get_cfg):
        self.get_cfg = get_cfg
        self._idx = {}
        self._lock = threading.Lock()
        self.local = LocalOCR()

    def eligible(self, pid: str, cfg: dict) -> bool:
        p, pc = PROVIDER_BY_ID[pid], cfg["providers"][pid]
        if p["kind"] == "local":
            return self.local.problem() is None
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

    def recognize(self, img, max_side: int):
        cfg = self.get_cfg()
        chain = self.chain(cfg)
        if not chain:
            raise OCRError(self.local.problem() or "No provider is ready. Add an API key in the AI Keys tab.")
        prompt = build_prompt(cfg)
        errors = []
        encoded = None  # PNG/JPEG encoding is only paid for when a cloud provider is actually used
        for pid in chain:
            pdef, pc = PROVIDER_BY_ID[pid], cfg["providers"][pid]
            if pdef["kind"] == "local":
                try:
                    return self.local.recognize(img, cfg), pdef["name"]
                except OCRError as exc:
                    errors.append(f"{pdef['name']}: {exc}")
                    continue
            if encoded is None:
                try:
                    encoded = encode_image(img, max_side)
                except OCRError as exc:
                    errors.append(str(exc))
                    encoded = False
            if encoded is False:
                continue
            data, mime = encoded
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
    """Appends text to the session output file. Duplicate filtering lasts for the whole launch."""

    def __init__(self, get_cfg, session: SessionOutput):
        self.get_cfg = get_cfg
        self.session = session
        self._lock = threading.Lock()
        self._last = ""
        self._seen = set()

    def write(self, text: str, force: bool = False) -> int:
        cfg = self.get_cfg()
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return 0
        norm = " ".join(" ".join(lines).split())
        with self._lock:
            if not force and cfg["skip_duplicates"] and norm == self._last:
                return 0
            fresh = lines
            seen_add = []
            if not force and cfg["only_new_lines"]:
                fresh = []
                for ln in lines:
                    k = " ".join(ln.split())
                    if k not in self._seen and k not in seen_add:
                        seen_add.append(k)
                        fresh.append(ln)
                if not fresh:
                    self._last = norm
                    return 0
            path = self.session.ensure()
            stamp = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] " if cfg["timestamps"] else ""
            try:
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write("".join(f"{stamp}{ln}\n" for ln in fresh))
            except OSError as exc:
                raise OutputError(f"Could not write to {path}: {exc.strerror or exc}") from exc
            # state only advances once the text is really on disk
            self._last = norm
            self._seen.update(seen_add)
            return len(fresh)


def frame_signature(img):
    return img.convert("L").resize((960, 540), Image.Resampling.BILINEAR)


def frame_changed(a, b, threshold: float = 0.00005) -> bool:
    if a is None or b is None or a.size != b.size:
        return True
    diff = ImageChops.difference(a, b).point(lambda p: 255 if p > 28 else 0)
    return ImageStat.Stat(diff).mean[0] / 255.0 > threshold


class ScanService:
    """Qt-free scan pipeline shared by recording, region scans and scan-current.

    capture() routes by mode and never falls back from region to full screen: a bad region is an error.
    """

    def __init__(self, get_cfg, capturer: Capturer, ocr: OCRClient, sink: TextSink):
        self.get_cfg = get_cfg
        self.capturer = capturer
        self.ocr = ocr
        self.sink = sink

    def capture(self, mode: str, region, monitor: str):
        if mode == "region":
            return self.capturer.grab_region(region)
        if mode == "full":
            return self.capturer.grab_full(monitor)
        raise CaptureError(f"Unknown capture mode: {mode!r}")

    def process_image(self, img, force: bool = False):
        text, provider = self.ocr.recognize(img, self.get_cfg()["max_side"])
        written = self.sink.write(text, force=force) if text else 0
        return text, written, provider

    def scan_current(self, mode: str, region, monitor: str):
        """Capture what is configured right now and run it through OCR + output (duplicate filter on)."""
        img = self.capture(mode, region, monitor)
        return self.process_image(img, force=False)


# hotkeys

class HotkeyManager:
    """Global hotkeys through pynput (X11, Windows, macOS). Callbacks only call `emit`, which must hand
    the command to the GUI thread (a Qt signal); they never touch widgets themselves."""

    PORTAL_WAIT = 4.0  # seconds apply() waits for the portal before reporting "waiting for the dialog"

    def __init__(self, emit, backend=None, portal_bus=None):
        self.emit = emit
        self._backend = backend  # tests inject a fake with GlobalHotKeys(mapping)
        self._portal_bus = portal_bus  # tests inject a fake D-Bus connection factory
        self._listener = None
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._listener is not None

    def stop(self):
        with self._lock:
            listener, self._listener = self._listener, None
            self._generation += 1  # callbacks of the old listener become no-ops
        if listener is None:
            return
        try:
            listener.stop()
            if threading.current_thread() is not listener:
                listener.join(1.0)
        except Exception:  # cleanup boundary: never let a backend error break shutdown, but record it
            log.exception("Could not stop the hotkey listener cleanly")

    def _make_callback(self, action: str, generation: int):
        def callback():
            if self._generation == generation:
                self.emit(action)
        return callback

    def apply(self, hotkeys: dict, wayland_mode: str = "auto"):
        """Register hotkeys. Returns (ok, message, level) with level in ok/info/warn/err.
        X11 / Windows / macOS: pynput. Wayland: the desktop portal where it exists, else manual binds."""
        self.stop()
        if is_wayland():
            return self._apply_wayland(hotkeys, wayland_mode)
        mapping, owner, problems = {}, {}, []
        with self._lock:
            generation = self._generation
        for action in HOTKEY_ORDER:
            seq = hotkeys.get(action)
            if not seq:
                continue
            label = HOTKEY_LABELS[action]
            pk = to_pynput(seq)
            if not pk:
                problems.append(f"'{label}': unsupported hotkey {seq}")
                continue
            if pk in owner:
                problems.append(f"{seq} is assigned to both '{HOTKEY_LABELS[owner[pk]]}' and '{label}'; "
                                f"'{label}' was not registered")
                continue
            owner[pk] = action
            mapping[pk] = self._make_callback(action, generation)
        if not mapping:
            if problems:
                return False, "No hotkeys registered. " + "; ".join(problems) + ".", "err"
            return True, "No hotkeys set.", "info"
        backend = self._backend
        if backend is None:
            try:
                from pynput import keyboard as backend
            except ImportError as exc:
                if getattr(exc, "name", None) in ("pynput", "pynput.keyboard"):
                    return False, "Global hotkeys need the 'pynput' package:  pip install pynput", "err"
                log.error("pynput could not be loaded: %s", exc)
                return False, f"Global hotkeys are unavailable: pynput could not start ({exc}).", "err"
        try:
            listener = backend.GlobalHotKeys(mapping)
            listener.daemon = True
            listener.start()
        except Exception as exc:  # backend raises platform specific errors; report them, with traceback in the log
            log.exception("Could not register global hotkeys")
            return False, f"Could not register hotkeys: {exc}", "err"
        with self._lock:
            self._listener = listener
        if problems:
            return False, f"Registered {len(mapping)} hotkey(s), but: " + "; ".join(problems) + ".", "err"
        note = " On macOS, allow Accessibility / Input Monitoring for this app if keys do nothing." if IS_MAC else ""
        return True, "Global hotkeys active." + note, "ok"

    def _apply_wayland(self, hotkeys: dict, mode: str):
        """Apps can't grab keys on Wayland. Hyprland keeps its compositor binds (unchanged behaviour, portal only
        on request); everywhere else the GlobalShortcuts portal is used when the running portal offers it."""
        manual = ("Wayland: apps can't grab global keys. Bind the keys in your compositor "
                  "using the lines below (they call --trigger).")
        if mode == "manual" or (mode == "auto" and detect_desktop() == "hyprland"):
            return False, manual, "info"
        import ocr_portal
        shortcuts = {}
        for action in HOTKEY_ORDER:
            seq = hotkeys.get(action)
            trig = ocr_portal.to_portal_trigger(seq) if seq else None
            if seq and not trig:
                return False, f"'{HOTKEY_LABELS[action]}': unsupported hotkey {seq}.", "err"
            if trig:
                shortcuts[action] = (HOTKEY_LABELS[action], trig)
        if not shortcuts:
            return True, "No hotkeys set.", "info"
        ok, reason = ocr_portal.probe(self._portal_bus) if self._portal_bus else ocr_portal.probe()
        if not ok:
            return False, (f"Wayland: global shortcuts through the desktop portal aren't available ({reason}). "
                           "Bind the keys manually using the lines below (they call --trigger)."), "info"
        with self._lock:
            generation = self._generation
        cb = self._make_callback
        client = ocr_portal.PortalShortcuts(shortcuts, lambda action: cb(action, generation)(),
                                            **({"bus_factory": self._portal_bus} if self._portal_bus else {}))
        client.start()
        with self._lock:
            self._listener = client
        client.ready.wait(self.PORTAL_WAIT)
        state, msg, level = client.status
        if state == "pending":
            return True, ("Waiting for you to confirm the global shortcuts in the desktop's dialog. "
                          "If none appears, use the manual binds below."), "info"
        if state == "err":
            with self._lock:
                self._listener = None
            client.stop()
            return False, f"{msg} Bind the keys manually using the lines below (they call --trigger).", level if level != "ok" else "err"
        return True, msg, level

    def portal_status(self):
        """Latest (ok, message, level) of a portal session, or None when no portal session is active."""
        listener = self._listener
        if not hasattr(listener, "status"):
            return None
        state, msg, level = listener.status
        return (state == "ok", msg, level) if state != "pending" else None

    def check_alive(self):
        """pynput reports startup failures from inside its thread. Returns an error text if the listener died."""
        listener = self._listener
        if listener is None or listener.is_alive():
            return None
        with self._lock:
            self._listener = None
        try:
            listener.join(0.1)  # re-raises the exception that killed the thread, if any
            reason = getattr(listener, "failure", None) or "the listener stopped unexpectedly"
        except Exception as exc:  # the stored listener exception can be of any type
            reason = str(exc) or exc.__class__.__name__
        log.error("Hotkey listener died: %s", reason)
        return f"Global hotkeys stopped working: {reason}"


class Recorder(threading.Thread):
    MAX_FAILS = 5

    def __init__(self, service: ScanService, bus, get_cfg, mode: str, region):
        """`bus` is anything with post(name, *args): the daemon's event hub (or a Qt Bus in tests)."""
        super().__init__(daemon=True)
        self.service = service
        self.bus = bus
        self.get_cfg = get_cfg
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

    def run(self):
        bus = self.bus
        fails = 0
        last_sig = None
        while not self.stop_evt.is_set():
            if not self.run_evt.wait(0.1):
                continue
            cfg = self.get_cfg()
            try:
                img = self.service.capture(self.mode, self.region, cfg["monitor"])
                sig = frame_signature(img)
                if cfg["skip_unchanged"] and not frame_changed(sig, last_sig):
                    pass
                else:
                    text, written, provider = self.service.process_image(img)
                    last_sig = sig
                    if not self.stop_evt.is_set():
                        bus.post("result", text, written, provider, "recording")
                fails = 0
            except (CaptureError, OCRError, OutputError) as exc:
                fails += 1
                log.warning("Recording: %s", exc)
                bus.post("log", "err", str(exc))
            except Exception as exc:  # thread boundary: keep the loop alive, report and back off
                fails += 1
                log.exception("Recording: unexpected error")
                bus.post("log", "err", f"Unexpected error: {exc.__class__.__name__}: {exc}")
            if fails >= self.MAX_FAILS:
                bus.post("log", "err", f"Stopped after {self.MAX_FAILS} failures in a row. "
                                       "Check your API key, quota and capture tools.")
                self.stop_evt.set()
                bus.post("auto_stopped", self)
                return
            wait = max(0.25, int(cfg["interval_ms"]) / 1000.0) + min(20, 3 * fails)
            self.stop_evt.wait(wait)




def setup_logging(filename: str = "ocr-scanner.log", console: bool = True):
    """Log to the console and to a rotating file in the config dir. Safe to call twice."""
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        log.addHandler(stream)
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(config_dir() / filename, maxBytes=512_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:
        log.warning("Could not open the log file: %s", exc)
