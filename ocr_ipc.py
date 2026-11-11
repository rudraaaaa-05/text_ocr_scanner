"""Local IPC between the daemon, the GUI and the ``--trigger`` command line. Stdlib only, no Qt.

Transport
    Linux:   a Unix domain socket in $XDG_RUNTIME_DIR (a per-user 0700 directory), chmod 0600. Every
             connection's peer uid is also checked (SO_PEERCRED).
    Windows: a TCP socket bound to 127.0.0.1 on an ephemeral port. The port and a random token are written
             to ``ipc-endpoint.json`` in the per-user config folder, and every request must carry the
             token. Nothing listens on a public interface.

Protocol (one request and one reply per connection, one line each, UTF-8, max 64 KiB)
    request  {"cmd": "status", "args": {...}, "token": "..."}
    reply    {"ok": true, "data": {...}}   or   {"ok": false, "error": {"code": "...", "message": "..."}}
    The old plain-text form ("region\\n") is still accepted on Unix and answered with "ok\\n" or
    "error: <message>\\n", so existing scripts and compositor bindings keep working.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from ocr_core import APP_ID, APP_NAME, IS_WINDOWS, VALID_COMMANDS, config_dir, log

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
            reply = request("show")
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
