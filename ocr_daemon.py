from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from ocr_core import (
    APP_ID, APP_NAME, IS_LINUX, IS_WINDOWS, VALID_COMMANDS, VERSION, CaptureError, Capturer, Config, OCRClient,
    OCRError, OutputError, Recorder, ScanService, SessionOutput, TextSink, HotkeyManager, config_dir,
    describe_region, detect_session, log, setup_logging,
)


##############################################################################################################
# 1. IPC
##############################################################################################################

# the commands that existed before the daemon split (VALID_COMMANDS) plus the ones the daemon adds
EXTRA_COMMANDS = ("resume", "status", "events", "ping", "scan_region", "reload_config", "select_ack", "shutdown")
ALL_COMMANDS = tuple(VALID_COMMANDS) + EXTRA_COMMANDS
# what `--trigger` accepts: the old set, plus the ones that make sense from a shell
TRIGGER_COMMANDS = tuple(VALID_COMMANDS) + ("resume", "status", "shutdown")

MAX_LINE = 64 * 1024
SERVER_TIMEOUT = 5.0
MAX_CONNECTIONS = 16

DAEMON_SCRIPT = str(Path(__file__).resolve().with_name("ocr_daemon.py"))
GUI_SCRIPT = str(Path(__file__).resolve().with_name("ocr_scanner.py"))


class IPCError(Exception):
    """Raised by command handlers; becomes a structured error reply."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# endpoint

def ipc_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    folder = Path(runtime) if runtime and os.path.isdir(runtime) else config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / f"{APP_ID}.sock")


def endpoint_file() -> Path:
    return config_dir() / "ipc-endpoint.json"


def _read_endpoint():
    try:
        data = json.loads(endpoint_file().read_text(encoding="utf-8"))
        return int(data["port"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _connect(timeout: float):
    """(socket, token). Raises OSError when nothing is listening."""
    if IS_WINDOWS:
        ep = _read_endpoint()
        if ep is None:
            raise ConnectionRefusedError("no endpoint file")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", ep[0]))
        except OSError:
            s.close()
            raise
        return s, ep[1]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(ipc_path())
    except OSError:
        s.close()
        raise
    return s, ""


def _read_line(conn: socket.socket, limit: int = MAX_LINE) -> bytes:
    """Reads up to the first newline. Raises ValueError when the line is too long."""
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise ValueError("request too large")
    return buf.split(b"\n", 1)[0]


# client

def request(cmd: str, args: dict | None = None, timeout: float = 3.0):
    """Send one command to the daemon. Returns the parsed reply dict, or None if it is not running."""
    try:
        s, token = _connect(timeout)
    except OSError:
        return None
    payload = {"cmd": cmd, "args": args or {}}
    if token:
        payload["token"] = token
    try:
        with s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            line = _read_line(s, MAX_LINE * 16)
    except (OSError, ValueError):
        return None
    try:
        reply = json.loads(line.decode("utf-8", "ignore"))
    except ValueError:
        return {"ok": False, "error": {"code": "bad_reply", "message": "The daemon sent an unreadable reply."}}
    if not isinstance(reply, dict):
        return {"ok": False, "error": {"code": "bad_reply", "message": "The daemon sent an unreadable reply."}}
    return reply


def send_command(cmd: str, timeout: float = 1.5):
    """Backwards compatible helper: the reply as text ("ok" / "error: ..."), or None if not running."""
    reply = request(cmd, timeout=timeout)
    if reply is None:
        return None
    if reply.get("ok"):
        return "ok"
    err = reply.get("error") or {}
    return f"error: {err.get('message', 'failed')}"


def daemon_running(timeout: float = 1.0) -> bool:
    reply = request("ping", timeout=timeout)
    return bool(reply and reply.get("ok"))


# server

class IPCServer(threading.Thread):
    """Accepts local connections and hands validated commands to `handler(cmd, args) -> data | None`.

    The handler may raise IPCError for expected failures; any other exception is logged and reported as
    code "internal" so one bad request can never kill the daemon.
    """

    def __init__(self, handler):
        super().__init__(daemon=True, name="ipc-server")
        self.handler = handler
        self.sock = None
        self.path = None
        self.token = ""
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def bind(self) -> bool:
        try:
            if IS_WINDOWS:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", 0))
                self.token = secrets.token_hex(24)
                ep = endpoint_file()
                ep.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(ep), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"port": s.getsockname()[1], "token": self.token, "pid": os.getpid()}, fh)
                self.path = str(ep)
            else:
                self.path = ipc_path()
                if os.path.exists(self.path):
                    if daemon_running():
                        return False  # a live instance owns it
                    os.unlink(self.path)
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                old = os.umask(0o177)  # the socket is created 0600 from the start
                try:
                    s.bind(self.path)
                finally:
                    os.umask(old)
                os.chmod(self.path, 0o600)
            s.listen(8)
            self.sock = s
            return True
        except OSError as exc:
            log.error("Could not open the IPC endpoint: %s", exc)
            return False

    def run(self):
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                conn.close()  # too many simultaneous connections: refuse instead of piling up threads
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True, name="ipc-conn").start()

    # a peer from another user is never served (Linux). Windows relies on the token.
    @staticmethod
    def _peer_ok(conn: socket.socket) -> bool:
        if IS_WINDOWS or not hasattr(socket, "SO_PEERCRED"):
            return True
        try:
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
        except (OSError, struct.error):
            return False
        return uid == os.getuid()

    def _serve(self, conn: socket.socket):
        try:
            with conn:
                conn.settimeout(SERVER_TIMEOUT)
                if not self._peer_ok(conn):
                    return
                try:
                    line = _read_line(conn)
                except ValueError:
                    self._reply(conn, False, error=("request_too_large", "The request is too large."), legacy=False)
                    return
                except OSError:
                    return  # timeout or reset: nothing sensible to answer
                self._dispatch(conn, line)
        finally:
            self._slots.release()

    def _dispatch(self, conn, line: bytes):
        text = line.decode("utf-8", "ignore").strip()
        legacy = not text.startswith("{")
        if legacy:
            parts = text.split()
            cmd, args, token = (parts[0] if parts else ""), {}, None
            if IS_WINDOWS:  # a bare text command has no token, so it cannot be authenticated
                self._reply(conn, False, error=("unauthorized", "A token is required."), legacy=True)
                return
        else:
            try:
                msg = json.loads(text)
            except ValueError:
                self._reply(conn, False, error=("bad_request", "The request is not valid JSON."), legacy=False)
                return
            if not isinstance(msg, dict):
                self._reply(conn, False, error=("bad_request", "The request must be a JSON object."), legacy=False)
                return
            cmd, args, token = msg.get("cmd"), msg.get("args", {}), msg.get("token")
            if not isinstance(args, dict):
                self._reply(conn, False, error=("bad_request", "'args' must be an object."), legacy=False)
                return
        if IS_WINDOWS and not (isinstance(token, str) and hmac.compare_digest(token, self.token)):
            self._reply(conn, False, error=("unauthorized", "Missing or wrong token."), legacy=legacy)
            return
        if not isinstance(cmd, str) or cmd not in ALL_COMMANDS:
            self._reply(conn, False, error=("unknown_command", f"Unknown command: {str(cmd)[:40]!r}."), legacy=legacy)
            return
        try:
            data = self.handler(cmd, args)
        except IPCError as exc:
            self._reply(conn, False, error=(exc.code, exc.message), legacy=legacy)
            return
        except Exception as exc:  # boundary of the request thread: report, keep the daemon alive
            log.exception("IPC command %r failed", cmd)
            self._reply(conn, False, error=("internal", f"{exc.__class__.__name__}: {exc}"), legacy=legacy)
            return
        self._reply(conn, True, data=data, legacy=legacy)

    @staticmethod
    def _reply(conn, ok: bool, data=None, error=None, legacy: bool = False):
        if legacy:
            out = "ok\n" if ok else f"error: {error[1]}\n"
        else:
            body = {"ok": True, "data": data} if ok else {"ok": False, "error": {"code": error[0], "message": error[1]}}
            out = json.dumps(body, ensure_ascii=False) + "\n"
        try:
            conn.sendall(out.encode("utf-8"))
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


# single instance

class InstanceLock:
    """An OS file lock held for the life of the process (released automatically if it crashes)."""

    def __init__(self, name: str):
        self.path = config_dir() / f"{name}.lock"
        self._fh = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except OSError as exc:
            log.error("Cannot open the lock file %s: %s", self.path, exc)
            return False
        try:
            if IS_WINDOWS:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


# starting processes

def spawn_detached(argv: list):
    """Start a process that outlives the caller (no console, no controlling terminal)."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if IS_WINDOWS:
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0x8000000))
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def daemon_argv(*extra: str) -> list:
    return [sys.executable, DAEMON_SCRIPT, *extra]


def start_daemon(wait: float = 10.0, launch_gui: bool = False):
    """Make sure a daemon is running. Returns (ok, message). Never starts a second one: the daemon itself
    refuses to run twice, so a race between two callers is harmless.

    ``launch_gui`` is used by the command-line launcher. The GUI itself leaves this
    disabled so opening the window never creates another GUI process.
    """
    if daemon_running():
        if launch_gui:
            reply = request("show", {"visible": True})
            if reply is None or not reply.get("ok"):
                return False, "The daemon is running, but the GUI could not be started."
            return True, "The daemon is already running; GUI requested."
        return True, "The daemon is already running."
    try:
        spawn_detached(daemon_argv("run"))
    except OSError as exc:
        return False, f"Could not start the {APP_NAME} daemon: {exc.strerror or exc}"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if daemon_running():
            if not launch_gui:
                return True, "The daemon was started."
            reply = request("show")
            if reply is None or not reply.get("ok"):
                return False, "The daemon started, but the GUI could not be started."
            return True, "The daemon and tray GUI were started."
        time.sleep(0.2)
    return False, f"The {APP_NAME} daemon did not answer within {wait:.0f} s. See the daemon log in {config_dir()}."


# command line

def cli_trigger(argv: list) -> int:
    """`--trigger <command>`: same exit codes as before (0 ok, 1 not running / failed, 2 bad usage)."""
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in TRIGGER_COMMANDS:
        sys.stderr.write(f"usage: ocr_scanner.py --trigger {{{'|'.join(TRIGGER_COMMANDS)}}}\n")
        return 2
    reply = request(cmd)
    if reply is None:
        sys.stderr.write(f"The {APP_NAME} daemon is not running. Start it first:  python ocr_daemon.py start\n")
        return 1
    if not reply.get("ok"):
        sys.stderr.write(f"{(reply.get('error') or {}).get('message', 'The command failed.')}\n")
        return 1
    if cmd == "status":
        print(json.dumps(reply.get("data"), indent=2, ensure_ascii=False))
    return 0


##############################################################################################################
# 2. GUI link (polls the daemon for events)
##############################################################################################################

POLL_INTERVAL = 0.5


class DaemonLink(threading.Thread):

    def __init__(self, on_update, on_connection):
        super().__init__(daemon=True, name="daemon-link")
        self.on_update, self.on_connection = on_update, on_connection
        self.stop_evt = threading.Event()
        self.last = 0
        self.connected = None   # unknown until the first poll
        self._first = True

    def stop(self):
        self.stop_evt.set()

    def run(self):
        while not self.stop_evt.is_set():
            reply = request("events", {"since": self.last}, timeout=2.0)
            ok = bool(reply and reply.get("ok"))
            if ok != self.connected:
                self.connected = ok
                if not ok:
                    self._first = True
                self._safe(self.on_connection, ok)
            if ok:
                data = reply["data"]
                if data["last"] < self.last:  # the daemon restarted: its counters started over
                    self.last = 0
                    self._first = True
                    continue
                self.last = data["last"]
                replay, self._first = self._first, False
                self._safe(self.on_update, data, replay)
            self.stop_evt.wait(POLL_INTERVAL)

    @staticmethod
    def _safe(fn, *args):
        try:
            fn(*args)
        except Exception:  # a GUI callback must never kill the poller thread
            log.exception("Daemon link callback failed")


##############################################################################################################
# 3. autostart (systemd user units / Windows Run key)
##############################################################################################################

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


##############################################################################################################
# 4. the daemon
##############################################################################################################

EVENT_BUFFER = 500
GUI_SEEN_WINDOW = 4.0        # the GUI polls every ~0.5 s; silent for longer than this means it is gone
SELECT_TTL = 20.0            # how long a region-selection request waits for a GUI to pick it up
SELECT_TTL_SPAWN = 45.0      # ...when a GUI process had to be started for it
RESULT_TEXT_CAP = 20000
WATCHDOG_INTERVAL = 5.0


class EventHub:
    """Ring buffer of events the GUI polls for. Also the `bus` the Recorder posts to."""

    def __init__(self, daemon: "Daemon"):
        self.daemon = daemon
        self._lock = threading.Lock()
        self._events = deque(maxlen=EVENT_BUFFER)
        self._next = 1

    def push(self, type_: str, **data) -> int:
        with self._lock:
            ev = {"id": self._next, "type": type_, "time": time.time(), **data}
            self._next += 1
            self._events.append(ev)
            return ev["id"]

    def since(self, last: int):
        with self._lock:
            return [e for e in self._events if e["id"] > last], self._next - 1

    # Recorder / worker interface, same shape as the old Qt Bus
    def post(self, name: str, *args) -> bool:
        d = self.daemon
        if name == "log":
            self.push("log", level=args[0], message=args[1])
        elif name == "result":
            text, written, provider, source = args
            d.record_result(text, written, provider, source)
        elif name == "auto_stopped":
            d.on_auto_stopped(args[0])
        return True


class Daemon:
    def __init__(self, cfg: Config | None = None, capturer: Capturer | None = None, backend=None):
        self.cfg = cfg or Config()
        self.d = self.cfg.data
        get_cfg = lambda: self.cfg.data  # noqa: E731
        self.events = EventHub(self)
        self.session = SessionOutput(self.d["output_file"])
        self.service = ScanService(get_cfg, capturer or Capturer(), OCRClient(get_cfg), TextSink(get_cfg, self.session))
        self.capturer, self.ocr = self.service.capturer, self.service.ocr
        self.hotkeys = HotkeyManager(self._on_hotkey, backend=backend)
        self.hotkey_status = (True, "Hotkeys not applied yet.", "info")
        self.ipc = IPCServer(self.handle)
        self.lock = InstanceLock("daemon")
        self.rec = None
        self.captures = 0
        self.lines_total = 0
        self.started_at = time.time()
        self.startup_error = ""
        self.stop_evt = threading.Event()
        self._mutex = threading.RLock()
        self._busy = {"region": False, "current": False}
        self._pending_select = None     # {"id", "purpose", "claimed", "expires"}
        self._select_seq = 0
        self._gui_seen = 0.0
        self._gui_spawned = 0.0
        self._closed = False

    # lifecycle

    def start(self, launch_gui: bool = False) -> int:
        """Returns a process exit code: 0 on a clean run, 1 if the daemon could not start."""
        if not self.lock.acquire():
            sys.stderr.write(f"The {APP_NAME} daemon is already running.\n")
            return 0
        if not self.ipc.bind():
            sys.stderr.write("Could not open the IPC endpoint (is another instance running?).\n")
            self.lock.release()
            return 1
        try:
            self.session.ensure()
        except OutputError as exc:
            self.startup_error = str(exc)
            self.failure(f"Output file: {exc}")
        self.ipc.start()
        self._install_signals()
        self.apply_hotkeys()
        self._warm_local()
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
        log.info("%s daemon %s started (pid %s, %s, session %s)", APP_NAME, VERSION, os.getpid(),
                 self.capturer.backend, detect_session())
        if self.cfg.secret_status:
            self.log_event("warn", self.cfg.secret_status)
        self.events.push("log", level="info", message=f"Daemon started. Capture backend: {self.capturer.backend}")
        if self.session.path is not None:
            self.events.push("log", level="info", message=f"This session writes to {self.session.path}")
        if launch_gui:
            threading.Thread(target=self._ensure_gui, daemon=True, name="gui-start").start()
        try:
            while not self.stop_evt.wait(0.5):  # short waits so signal handlers run promptly
                pass
        finally:
            self.shutdown()
        return 0

    def request_shutdown(self):
        self.stop_evt.set()

    def shutdown(self):
        """Idempotent. Everything that could keep the process alive is stopped here."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            rec, self.rec = self.rec, None
        log.info("Daemon shutting down.")
        if rec is not None:
            rec.stop()
        self.capturer.cancel_selection()
        self.hotkeys.stop()
        self.ipc.close()
        self.lock.release()

    def _install_signals(self):
        def handler(_sig, _frame):
            self.request_shutdown()
        names = ["SIGINT", "SIGTERM"] + (["SIGBREAK"] if IS_WINDOWS else [])
        for name in names:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread (tests)
                pass

    # helpers

    def gui_connected(self) -> bool:
        return time.monotonic() - self._gui_seen < GUI_SEEN_WINDOW

    @property
    def state(self) -> str:
        rec = self.rec
        if rec is None:
            return "idle"
        return "paused" if rec.paused else "recording"

    def log_event(self, level: str, message: str):
        log.log({"err": 40, "warn": 30}.get(level, 20), "%s", message)
        self.events.push("log", level=level, message=message)

    def notify(self, title: str, body: str, force: bool = False):
        if not force and not self.d["notifications"]:
            return
        self.events.push("notify", title=title, body=body[:300])
        if self.gui_connected():
            return  # the GUI shows it
        if IS_LINUX and shutil.which("notify-send"):
            try:
                subprocess.Popen(["notify-send", "-a", APP_NAME, "-t", "3500", title, body[:300]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def failure(self, message: str):
        """An error the person must see: log, event and a notification even if notifications are off."""
        self.log_event("err", message)
        self.notify(APP_NAME, message, force=True)

    def _push_state(self):
        self.events.push("state", state=self.state)

    def record_result(self, text: str, written: int, provider: str, source: str):
        with self._mutex:
            self.captures += 1
            self.lines_total += written
        self.events.push("result", text=text[:RESULT_TEXT_CAP], lines=written, provider=provider, source=source)
        if source == "scan":
            self.notify("Scanned" if text.strip() else "No text found",
                        text.strip()[:200] or "Nothing readable in that region.")

    def on_auto_stopped(self, rec):
        with self._mutex:
            if rec is not self.rec:
                return
            self.rec = None
        self._push_state()
        self.notify(APP_NAME, "Recording stopped after repeated errors.", force=True)

    def _warm_local(self):
        if "local" not in self.ocr.chain(self.d) or self.ocr.local.problem() is not None:
            return

        def work():
            try:
                ms = self.ocr.local.warmup(self.d)
                self.log_event("ok", f"Local OCR engine ready ({ms} ms).")
            except Exception as exc:  # thread boundary: any engine failure is reported, never fatal
                log.exception("Local OCR warm-up failed")
                self.log_event("err", f"Local OCR: {exc}")
        threading.Thread(target=work, daemon=True, name="warm-local").start()

    # hotkeys

    def _on_hotkey(self, action: str):
        # runs on the pynput thread: never block it
        threading.Thread(target=self._run_hotkey, args=(action,), daemon=True, name="hotkey-action").start()

    def _run_hotkey(self, action: str):
        try:
            self.handle(action, {})
        except IPCError as exc:
            self.failure(exc.message)
        except Exception as exc:  # thread boundary
            log.exception("Hotkey action %r failed", action)
            self.failure(f"Hotkey action failed: {exc.__class__.__name__}: {exc}")

    def apply_hotkeys(self):
        ok, msg, level = self.hotkeys.apply(self.d["hotkeys"], self.d.get("wayland_hotkeys", "auto"))
        self.hotkey_status = (ok, msg, level)
        if level == "err":
            self.failure(f"Hotkeys: {msg}")
        elif level == "info" and not ok:
            self.log_event("info", msg)
        if self.hotkeys.running:
            threading.Timer(0.8, self._verify_hotkeys).start()  # pynput reports start-up failures from its own thread

    def _verify_hotkeys(self):
        if self._closed:
            return
        err = self.hotkeys.check_alive()
        if err:
            self.hotkey_status = (False, err, "err")
            self.failure(err)
            return
        update = self.hotkeys.portal_status()  # a portal dialog may be answered long after apply() returned
        if update is not None and update != self.hotkey_status:
            self.hotkey_status = update
            self.log_event("ok" if update[0] else "warn", update[1])

    def _watchdog(self):
        while not self.stop_evt.wait(WATCHDOG_INTERVAL):
            self._verify_hotkeys()
            with self._mutex:
                pend = self._pending_select
                if pend and time.monotonic() > pend["expires"]:
                    self._pending_select = None
                    self._busy["region"] = False
                    expired = True
                else:
                    expired = False
            if expired:
                self.failure("Region selection timed out: no window picked it up. Open the OCR Scanner window and try again.")

    # command dispatch

    def handle(self, cmd: str, args: dict):
        fn = getattr(self, "cmd_" + cmd, None)
        if fn is None:
            raise IPCError("unknown_command", f"Unknown command: {cmd!r}.")
        return fn(args)

    def cmd_ping(self, _a):
        return {"pid": os.getpid(), "version": VERSION}

    def cmd_status(self, _a):
        ok, msg, level = self.hotkey_status
        return {
            "pid": os.getpid(), "version": VERSION, "state": self.state, "mode": self.d["mode"],
            "captures": self.captures, "lines": self.lines_total,
            "session_file": str(self.session.path) if self.session.path else None,
            "session_error": self.startup_error or None,
            "hotkeys": {"ok": ok, "message": msg, "level": level, "running": self.hotkeys.running},
            "session": detect_session(),
            "credential_store": {"secure": self.cfg.secrets.available, "warning": self.cfg.secret_status},
            "backend": self.capturer.backend, "gui_connected": self.gui_connected(),
            "uptime": int(time.time() - self.started_at),
        }

    def cmd_events(self, args):
        self._gui_seen = time.monotonic()
        last = args.get("since", 0)
        if not isinstance(last, int) or isinstance(last, bool) or last < 0:
            raise IPCError("bad_argument", "'since' must be a non-negative integer.")
        events, newest = self.events.since(last)
        sel = None
        with self._mutex:
            p = self._pending_select
            if p and not p["claimed"] and time.monotonic() <= p["expires"]:
                sel = {"id": p["id"], "purpose": p["purpose"]}
        ok, msg, level = self.hotkey_status
        return {"events": events, "last": newest, "select": sel, "state": self.state,
                "captures": self.captures, "lines": self.lines_total,
                "session_file": str(self.session.path) if self.session.path else None,
                "session_error": self.startup_error or None,
                "hotkeys": {"ok": ok, "message": msg, "level": level, "running": self.hotkeys.running}}

    def cmd_show(self, args):
        if self.gui_connected():
            self.events.push("show")
        else:
            self._spawn_gui(hidden=not bool(args.get("visible")))
        return None

    def _spawn_gui(self, hidden: bool = True):
        if time.monotonic() - self._gui_spawned < SELECT_TTL_SPAWN:
            return  # one was just started and is still coming up
        self._gui_spawned = time.monotonic()
        try:
            argv = [sys.executable, GUI_SCRIPT]
            if hidden:
                argv.append("--hidden")
            spawn_detached(argv)
        except OSError as exc:
            raise IPCError("gui_unavailable", f"Could not start the GUI: {exc.strerror or exc}") from exc

    def _ensure_gui(self):
        """Retry the tray client during login while the graphical session settles."""
        for _ in range(12):
            if self.stop_evt.wait(1.0):
                return
            if self.gui_connected():
                return
            self._gui_spawned = 0.0
            try:
                self._spawn_gui()
            except IPCError as exc:
                self.log_event("warn", exc.message)

    def cmd_shutdown(self, _a):
        threading.Timer(0.2, self.request_shutdown).start()  # reply first, then exit
        return None

    def cmd_reload_config(self, _a):
        self.cfg.load()
        self.d = self.cfg.data
        self.apply_hotkeys()
        self._warm_local()  # no-op unless the local engine is in use and not loaded yet
        return {"hotkeys": self.hotkey_status[1]}

    # recording

    def _check_ready(self, need_region: bool):
        prob = self.capturer.problem(need_region=need_region)
        if prob:
            raise IPCError("capture_unavailable", prob)
        if not self.ocr.chain(self.d):
            raise IPCError("ocr_unavailable",
                           self.ocr.local.problem() or "No OCR provider is ready. Add a key in the AI Keys tab.")

    def cmd_start(self, _a):
        with self._mutex:
            if self.rec is not None:
                return {"state": self.state}
            mode = self.d["mode"]
            region = None
            if mode == "region":
                err = self.capturer.region_error(self.d["region"])
                if err:
                    raise IPCError("region_invalid", err)
                region = dict(self.d["region"])
            self._check_ready(need_region=False)
            self.rec = Recorder(self.service, self.events, lambda: self.d, mode, region)
            self.rec.start()
        what = "full screen" if mode == "full" else f"region {describe_region(region)}"
        self.log_event("ok", f"Recording started ({what}), every {self.d['interval_ms']} ms.")
        self._push_state()
        return {"state": self.state}

    def cmd_stop(self, _a):
        with self._mutex:
            rec, self.rec = self.rec, None
        if rec is not None:
            rec.stop()
            self.log_event("info", "Recording stopped.")
            self._push_state()
        return {"state": self.state}

    def cmd_pause(self, _a):
        """Toggle, as the pause hotkey always did."""
        rec = self.rec
        if rec is None:
            return {"state": self.state}
        return self.cmd_resume({}) if rec.paused else self._pause(rec)

    def _pause(self, rec):
        rec.pause()
        self.log_event("info", "Paused.")
        self._push_state()
        return {"state": self.state}

    def cmd_resume(self, _a):
        rec = self.rec
        if rec is not None and rec.paused:
            rec.resume()
            self.log_event("info", "Resumed.")
            self._push_state()
        return {"state": self.state}

    def cmd_toggle(self, _a):
        if self.rec is not None:
            self.cmd_stop({})
            self.notify(APP_NAME, "Recording stopped")
        else:
            self.cmd_start({})
            self.notify(APP_NAME, "Recording started")
        return {"state": self.state}

    # one-shot scans

    def _claim(self, kind: str):
        with self._mutex:
            if self._busy[kind]:
                raise IPCError("busy", "A scan is already running." if kind == "current"
                               else "A region scan is already in progress.")
            self._busy[kind] = True

    def _release(self, kind: str):
        with self._mutex:
            self._busy[kind] = False

    def _run_job(self, kind: str, fn):
        try:
            text, written, provider = fn()
            self.events.post("result", text, written, provider, "scan")
        except (CaptureError, OCRError, OutputError) as exc:
            log.warning("Scan (%s) failed: %s", kind, exc)
            self.failure(f"Scan failed: {exc}")
        except Exception as exc:  # thread boundary
            log.exception("Scan (%s) failed unexpectedly", kind)
            self.failure(f"Scan failed unexpectedly: {exc.__class__.__name__}: {exc}")
        finally:
            self._release(kind)

    def cmd_current(self, _a):
        mode = self.d["mode"]
        region = dict(self.d["region"]) if isinstance(self.d["region"], dict) else self.d["region"]
        monitor = self.d["monitor"]
        if mode == "region":
            err = self.capturer.region_error(region)
            if err:  # no OCR, and never a fallback to full screen
                raise IPCError("region_invalid", f"Scan current: {err}")
        self._check_ready(need_region=False)
        self._claim("current")
        self.log_event("info", "Scanning the stored region..." if mode == "region" else "Scanning the screen...")
        threading.Thread(target=self._run_job, daemon=True, name="scan-current",
                         args=("current", lambda: self.service.scan_current(mode, region, monitor))).start()
        return {"started": True}

    def cmd_region(self, _a):
        """Pick a NEW region and scan it once (the region is not stored)."""
        self._check_ready(need_region=True)
        self._claim("region")
        if self.capturer.uses_slurp:  # slurp needs no GUI; the portal backend uses the GUI's Qt selector
            threading.Thread(target=self._wayland_region, daemon=True, name="region-wayland").start()
            return {"started": True, "selector": "slurp"}
        try:
            spawned = not self.gui_connected()
            if spawned:
                self._spawn_gui()
        except IPCError:
            self._release("region")
            raise
        with self._mutex:
            self._select_seq += 1
            self._pending_select = {"id": self._select_seq, "purpose": "once", "claimed": False,
                                    "expires": time.monotonic() + (SELECT_TTL_SPAWN if spawned else SELECT_TTL)}
        return {"started": True, "selector": "gui"}

    def _wayland_region(self):
        try:
            region = self.capturer.select_region_wayland()
            if region is None:
                self.log_event("info", "Selection cancelled.")
                self._release("region")
                return
        except (CaptureError, OSError) as exc:
            self.failure(f"Scan failed: {exc}")
            self._release("region")
            return
        self.log_event("info", "Reading region...")
        self._run_job("region", lambda: self._scan_region(region))

    def _scan_region(self, region):
        img = self.capturer.grab_region(region)
        return self.service.process_image(img, force=True)

    def cmd_select_ack(self, args):
        with self._mutex:
            p = self._pending_select
            if p and p["id"] == args.get("id"):
                p["claimed"] = True
        return None

    def cmd_scan_region(self, args):
        """Called by the GUI after the user picked a region (region=null means cancelled / failed)."""
        region, error = args.get("region"), args.get("error")
        with self._mutex:
            self._pending_select = None
        if region is None:
            if error:
                self.failure(f"Region selection: {error}")
            elif not args.get("silent"):  # the GUI has already told the user it was cancelled
                self.log_event("info", "Selection cancelled.")
            self._release("region")
            return None
        err = self.capturer.region_error(region)
        if err:
            self._release("region")
            raise IPCError("region_invalid", err)
        with self._mutex:
            self._busy["region"] = True
        self.log_event("info", "Reading region...")
        threading.Thread(target=self._run_job, daemon=True, name="scan-region",
                         args=("region", lambda: self._scan_region(region))).start()
        return {"started": True}


def _session_text(sess) -> str:
    if not isinstance(sess, dict):
        return "unknown"
    parts = [sess.get("distro_name") or sess.get("os"), sess.get("desktop"), sess.get("session_type")]
    return ", ".join(str(p) for p in parts if p and p != "unknown") or "unknown"


def _keys_text(store) -> str:
    if not isinstance(store, dict):
        return "unknown"
    if store.get("secure"):
        return "system credential store" + (f" (warning: {store['warning']})" if store.get("warning") else "")
    return "config file, not encrypted" + (f" ({store['warning']})" if store.get("warning") else "")


def _status_text(data: dict) -> str:
    hk = data.get("hotkeys", {})
    lines = [
        f"{APP_NAME} daemon {data.get('version')}  pid {data.get('pid')}  up {data.get('uptime')} s",
        f"state:      {data.get('state')}  ({data.get('mode')} mode)",
        f"scans:      {data.get('captures')}  lines saved: {data.get('lines')}",
        f"output:     {data.get('session_file') or '(none)'}",
        f"hotkeys:    {hk.get('message')}",
        f"session:    {_session_text(data.get('session'))}",
        f"API keys:   {_keys_text(data.get('credential_store'))}",
        f"GUI:        {'connected' if data.get('gui_connected') else 'not connected'}",
        f"backend:    {data.get('backend')}",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ocr_daemon.py", description=f"{APP_NAME} background daemon")
    sub = ap.add_subparsers(dest="action")
    ap.set_defaults(gui=False)
    run_parser = sub.add_parser("run", help="run in the foreground")
    run_parser.add_argument("--gui", action="store_true", help="also launch the tray GUI")
    sub.add_parser("start", help="start detached in the background")
    sub.add_parser("stop", help="ask the running daemon to exit")
    sub.add_parser("status", help="show the daemon's state")
    au = sub.add_parser("autostart", help="start the daemon at login")
    au.add_argument("what", choices=["enable", "disable", "status"])
    sv = sub.add_parser("service", help="manage the systemd user service (Linux)")
    sv.add_argument("what", choices=["start", "stop", "restart", "status"])
    ns = ap.parse_args(argv)
    action = ns.action or "run"

    if action == "run":
        setup_logging("ocr-daemon.log")
        return Daemon().start(launch_gui=ns.gui)
    if action == "start":
        ok, msg = start_daemon(launch_gui=True)
        print(msg)
        return 0 if ok else 1
    if action == "stop":
        reply = request("shutdown")
        if reply is None:
            print("The daemon is not running.")
            return 0
        if not reply.get("ok"):
            print((reply.get("error") or {}).get("message", "Failed."))
            return 1
        for _ in range(50):  # wait for it to go away
            if not daemon_running(0.3):
                print("The daemon stopped.")
                return 0
            time.sleep(0.1)
        print("The daemon was told to stop but is still answering.")
        return 1
    if action == "status":
        reply = request("status")
        if reply is None:
            print("The daemon is not running.")
            return 1
        if not reply.get("ok"):
            print((reply.get("error") or {}).get("message", "Failed."))
            return 1
        print(_status_text(reply["data"]))
        return 0
    if action == "autostart":
        return cli_autostart(ns.what)
    return cli_service(ns.what)


if __name__ == "__main__":
    sys.exit(main())
