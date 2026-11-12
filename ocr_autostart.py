from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ocr_core import APP_ID, APP_NAME, IS_LINUX, IS_WINDOWS
from ocr_ipc import DAEMON_SCRIPT, GUI_SCRIPT, start_daemon

UNIT_NAME = f"{APP_ID}.service"
GUI_UNIT_NAME = f"{APP_ID}-gui.service"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "OCRScanner"
# the graphical session's variables the daemon needs for grim/slurp, mss and pynput
SESSION_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                "HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_DESKTOP",
                "DESKTOP_SESSION", "SWAYSOCK")


# systemd (Linux)

def unit_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def gui_unit_path() -> Path:
    return unit_dir() / GUI_UNIT_NAME


def _systemd_quote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{s}"'


def render_unit(python: str, script: str) -> str:
    return (
        "[Unit]\n"
        f"Description={APP_NAME} background daemon\n"
        "After=graphical-session.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_systemd_quote(python)} {_systemd_quote(script)} run\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical-session.target hyprland-session.target\n"
    )


def render_gui_unit(python: str, script: str, daemon_unit: str = UNIT_NAME) -> str:
    return (
        "[Unit]\n"
        f"Description={APP_NAME} tray GUI\n"
        f"Requires={daemon_unit}\n"
        f"After={daemon_unit} graphical-session.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_systemd_quote(python)} {_systemd_quote(script)} --hidden\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical-session.target hyprland-session.target\n"
    )


def _run(cmd: list):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return 127, "systemctl was not found. This needs a systemd based Linux."
    except subprocess.SubprocessError as exc:
        return 1, str(exc)
    return r.returncode, (r.stdout or r.stderr or "").strip()


def systemd_enable(run=_run, python: str | None = None, script: str | None = None):
    """Install + enable + start the user unit. Returns (ok, message)."""
    path = unit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        py = python or sys.executable
        daemon_script = script or DAEMON_SCRIPT
        path.write_text(render_unit(py, daemon_script), encoding="utf-8")
        gui_unit_path().write_text(render_gui_unit(py, GUI_SCRIPT), encoding="utf-8")
    except OSError as exc:
        return False, f"Could not write {path}: {exc.strerror or exc}"
    rc, out = run(["systemctl", "--user", "daemon-reload"])
    if rc != 0:
        return False, f"systemctl --user daemon-reload failed: {out}"
    present = [v for v in SESSION_VARS if os.environ.get(v)]
    if present:  # user services do not inherit the session environment by default
        run(["systemctl", "--user", "import-environment", *present])
    for unit in (UNIT_NAME, GUI_UNIT_NAME):
        run(["systemctl", "--user", "disable", unit])
    for unit in (UNIT_NAME, GUI_UNIT_NAME):
        rc, out = run(["systemctl", "--user", "enable", "--now", unit])
        if rc != 0:
            return False, f"systemctl --user enable failed for {unit}: {out}"
        rc, out = run(["systemctl", "--user", "restart", unit])
        if rc != 0:
            return False, f"systemctl --user restart failed for {unit}: {out}"
    return True, f"Enabled and started {UNIT_NAME}. The daemon and tray GUI now start at every login."


def systemd_disable(run=_run):
    failures = []
    for unit in (GUI_UNIT_NAME, UNIT_NAME):
        rc, out = run(["systemctl", "--user", "disable", "--now", unit])
        if rc != 0 and "not loaded" not in out.lower() and "does not exist" not in out.lower():
            failures.append(f"{unit}: {out}")
    for path in (gui_unit_path(), unit_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return False, f"Could not remove {path}: {exc.strerror or exc}"
    run(["systemctl", "--user", "daemon-reload"])
    if failures:
        return False, f"systemctl --user disable failed: {'; '.join(failures)}"
    return True, f"Disabled {UNIT_NAME} and stopped it."


def systemd_status(run=_run) -> str:
    if not unit_path().exists():
        return "autostart: not enabled (no user unit installed)"
    _, enabled = run(["systemctl", "--user", "is-enabled", UNIT_NAME])
    _, active = run(["systemctl", "--user", "is-active", UNIT_NAME])
    _, gui_active = run(["systemctl", "--user", "is-active", GUI_UNIT_NAME])
    return (f"autostart: units installed, {enabled or 'unknown'}; daemon is {active or 'unknown'}; "
            f"tray GUI is {gui_active or 'unknown'}")


def systemd_control(action: str, run=_run):
    rc, out = run(["systemctl", "--user", action, UNIT_NAME])
    return rc == 0, out or f"systemctl --user {action}: done"


# Windows startup entry

def windows_command(python: str | None = None, script: str | None = None) -> str:
    exe = python or sys.executable
    pyw = Path(exe).with_name("pythonw.exe")
    if pyw.exists():  # pythonw has no console window
        exe = str(pyw)
    return f'"{exe}" "{script or DAEMON_SCRIPT}" run'


def _winreg():
    import winreg
    return winreg


def windows_enable(reg=None, start=start_daemon):
    reg = reg or _winreg()
    try:
        with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, windows_command())
    except OSError as exc:
        return False, f"Could not write the startup entry: {exc.strerror or exc}"
    ok, msg = start()
    return True, "Autostart enabled. " + msg if ok else f"Autostart enabled, but the daemon could not be started now: {msg}"


def windows_disable(reg=None):
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass  # nothing to remove
    except OSError as exc:
        return False, f"Could not remove the startup entry: {exc.strerror or exc}"
    return True, "Autostart disabled. A running daemon keeps running; stop it with: python ocr_daemon.py stop"


def windows_status(reg=None) -> str:
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = reg.QueryValueEx(key, RUN_VALUE)
        return f"autostart: enabled ({value})"
    except OSError:
        return "autostart: not enabled"


# command line entry points

def cli_autostart(what: str) -> int:
    if IS_WINDOWS:
        if what == "status":
            print(windows_status())
            return 0
        ok, msg = windows_enable() if what == "enable" else windows_disable()
    elif IS_LINUX:
        if what == "status":
            print(systemd_status())
            return 0
        if not shutil.which("systemctl"):
            print("systemctl was not found. Autostart needs a systemd based Linux.")
            return 1
        ok, msg = systemd_enable() if what == "enable" else systemd_disable()
    else:
        print("Autostart is supported on Linux and Windows only.")
        return 1
    print(msg)
    return 0 if ok else 1


def cli_service(what: str) -> int:
    if not IS_LINUX:
        print("The systemd service commands are Linux only. On Windows use: python ocr_daemon.py start|stop|status")
        return 1
    if what == "status":
        print(systemd_status())
        rc, out = _run(["systemctl", "--user", "status", "--no-pager", UNIT_NAME])
        print(out)
        return 0 if rc == 0 else 3
    ok, msg = systemd_control(what)
    print(msg)
    return 0 if ok else 1
