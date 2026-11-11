"""OCR Scanner background daemon. Runs without PySide6 and without a display server connection of its own.

It owns screen capture, OCR, recording, output files, global hotkeys, the configuration it reads, and the
local IPC endpoint. The GUI (ocr_scanner.py) and `--trigger` are only clients of it.

    python ocr_daemon.py run          run in the foreground (what systemd / the Windows startup entry use)
    python ocr_daemon.py start        start it detached in the background (no-op if already running)
    python ocr_daemon.py stop         ask it to shut down
    python ocr_daemon.py status       show its state (exit code 1 if it is not running)
    python ocr_daemon.py autostart enable|disable|status
    python ocr_daemon.py service start|stop|restart|status     (Linux, systemd user service)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque

from ocr_core import (
    APP_NAME, IS_LINUX, IS_WINDOWS, VERSION, CaptureError, Capturer, Config, OCRClient, OCRError, OutputError,
    Recorder, ScanService, SessionOutput, TextSink, HotkeyManager, describe_region, log, setup_logging,
)
from ocr_ipc import (
    GUI_SCRIPT, IPCError, IPCServer, InstanceLock, daemon_running, request, spawn_detached, start_daemon,
)

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
        log.info("%s daemon %s started (pid %s, %s)", APP_NAME, VERSION, os.getpid(), self.capturer.backend)
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
        ok, msg, level = self.hotkeys.apply(self.d["hotkeys"])
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

    def cmd_show(self, _a):
        if self.gui_connected():
            self.events.push("show")
        else:
            self._spawn_gui()
        return None

    def _spawn_gui(self):
        if time.monotonic() - self._gui_spawned < SELECT_TTL_SPAWN:
            return  # one was just started and is still coming up
        self._gui_spawned = time.monotonic()
        try:
            spawn_detached([sys.executable, GUI_SCRIPT, "--hidden"])
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
        if self.capturer.wayland:  # slurp needs no GUI
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


def _status_text(data: dict) -> str:
    hk = data.get("hotkeys", {})
    lines = [
        f"{APP_NAME} daemon {data.get('version')}  pid {data.get('pid')}  up {data.get('uptime')} s",
        f"state:      {data.get('state')}  ({data.get('mode')} mode)",
        f"scans:      {data.get('captures')}  lines saved: {data.get('lines')}",
        f"output:     {data.get('session_file') or '(none)'}",
        f"hotkeys:    {hk.get('message')}",
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
    import ocr_autostart
    if action == "autostart":
        return ocr_autostart.cli_autostart(ns.what)
    return ocr_autostart.cli_service(ns.what)


if __name__ == "__main__":
    sys.exit(main())
