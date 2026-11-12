"""Daemon, IPC, autostart and GUI-link tests. No PySide6, display or network needed.

Run with:  python -m unittest discover -s tests -v
The end-to-end tests start the real daemon as a subprocess inside a throw-away config/runtime directory.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OCR_SCANNER_KEYRING", "off")  # tests must never touch the real credential store

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import ocr_autostart as autostart  # noqa: E402
import ocr_client  # noqa: E402
import ocr_core as core  # noqa: E402
import ocr_daemon  # noqa: E402
import ocr_ipc as ipc  # noqa: E402
from PIL import Image  # noqa: E402

IS_WIN = sys.platform.startswith("win")


def wait_for(cond, timeout=8.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class IsolatedEnv(unittest.TestCase):
    """Every test gets its own HOME / config / runtime dir so nothing touches the real user's files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / "run").mkdir(mode=0o700)
        env = {"HOME": str(base / "home"), "XDG_CONFIG_HOME": str(base / "cfg"), "XDG_RUNTIME_DIR": str(base / "run"),
               "APPDATA": str(base / "appdata")}
        (base / "home").mkdir()
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "HYPRLAND_INSTANCE_SIGNATURE"):
            os.environ.pop(k, None)
        self.env = dict(os.environ)
        self.base = base


# the daemon must not need Qt

class NoQtTests(unittest.TestCase):
    def test_daemon_modules_import_without_pyside6(self):
        code = ("import sys; sys.modules['PySide6'] = None\n"
                "import ocr_core, ocr_ipc, ocr_daemon, ocr_autostart, ocr_client\n"
                "assert not any(m.startswith('PySide6') and sys.modules[m] for m in sys.modules), 'Qt was imported'\n"
                "print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    def test_core_sources_never_import_qt(self):
        import re
        for name in ("ocr_core.py", "ocr_ipc.py", "ocr_daemon.py", "ocr_autostart.py", "ocr_client.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"^\s*(import|from)\s+(PySide6|PyQt)", text, re.M), name)


# IPC

class IPCTests(IsolatedEnv):
    def start_server(self, handler=None):
        calls = []

        def default(cmd, args):
            calls.append((cmd, args))
            if cmd == "stop":
                raise ipc.IPCError("nope", "refused on purpose")
            if cmd == "start":
                raise RuntimeError("boom")
            return {"echo": cmd, "args": args}

        server = ipc.IPCServer(handler or default)
        self.assertTrue(server.bind())
        server.start()
        self.addCleanup(server.close)
        return server, calls

    def test_roundtrip_and_structured_errors(self):
        _, calls = self.start_server()
        r = ipc.request("status", {"a": 1})
        self.assertEqual(r, {"ok": True, "data": {"echo": "status", "args": {"a": 1}}})
        r = ipc.request("stop")
        self.assertEqual(r["error"], {"code": "nope", "message": "refused on purpose"})
        r = ipc.request("start")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "internal")
        self.assertTrue(ipc.request("ping")["ok"], "one failing handler must not kill the server")

    def test_validation(self):
        server, calls = self.start_server()
        for raw, code in ((b"{not json}\n", "bad_request"), (b'{"cmd": "rm -rf"}\n', "unknown_command"), (b'{"cmd": 5}\n', "unknown_command"),
                          (b'{"cmd": "ping", "args": []}\n', "bad_request")):
            with self.subTest(raw=raw):
                s, _ = ipc._connect(2)
                with s:
                    s.sendall(raw)
                    reply = json.loads(s.makefile().readline())
                self.assertEqual(reply["error"]["code"], code)
        self.assertEqual(calls, [])

    def test_oversized_request_is_rejected(self):
        self.start_server()
        s, _ = ipc._connect(3)
        with s:
            s.sendall(b"x" * (ipc.MAX_LINE + 4096))
            reply = json.loads(s.makefile().readline())
        self.assertEqual(reply["error"]["code"], "request_too_large")

    def test_silent_client_times_out_without_blocking_others(self):
        with mock.patch.object(ipc, "SERVER_TIMEOUT", 0.4):
            self.start_server()
            idle, _ = ipc._connect(3)
            self.addCleanup(idle.close)
            self.assertTrue(ipc.request("ping")["ok"])  # served while the idle one hangs around
            idle.settimeout(3)
            self.assertEqual(idle.recv(10), b"")        # the server gave up on it

    @unittest.skipIf(IS_WIN, "unix socket permissions")
    def test_socket_is_private_to_the_user(self):
        server, _ = self.start_server()
        self.assertEqual(os.stat(server.path).st_mode & 0o777, 0o600)

    def test_legacy_plain_text_commands_still_work(self):
        _, calls = self.start_server()
        for text, expect in ((b"region\n", b"ok\n"), (b"current extra words\n", b"ok\n")):
            s, _ = ipc._connect(2)
            with s:
                s.sendall(text)
                self.assertEqual(s.recv(100), expect)
        s, _ = ipc._connect(2)
        with s:
            s.sendall(b"stop\n")
            self.assertEqual(s.recv(100), b"error: refused on purpose\n")
        self.assertEqual(ipc.send_command("show"), "ok")
        self.assertEqual([c for c, _ in calls], ["region", "current", "stop", "show"])

    def test_send_command_reports_not_running(self):
        self.assertIsNone(ipc.send_command("show"))
        self.assertIsNone(ipc.request("status"))
        self.assertFalse(ipc.daemon_running())

    def test_stale_socket_is_replaced_and_live_one_is_respected(self):
        path = ipc.ipc_path()
        dead = socket.socket(socket.AF_UNIX)
        dead.bind(path)
        dead.close()  # a socket file nobody listens on, as after a crash
        server, _ = self.start_server()
        self.assertTrue(ipc.daemon_running())
        second = ipc.IPCServer(lambda c, a: None)
        self.assertFalse(second.bind(), "must not steal the endpoint from a live server")
        self.assertTrue(ipc.daemon_running())

    def test_trigger_cli_exit_codes(self):
        self.assertEqual(ipc.cli_trigger(["--trigger", "nonsense"]), 2)
        with mock.patch("sys.stderr"):
            self.assertEqual(ipc.cli_trigger(["--trigger", "region"]), 1)  # nothing running
        self.start_server()
        for cmd in ("region", "current", "toggle", "pause", "show", "resume"):
            self.assertEqual(ipc.cli_trigger(["--trigger", cmd]), 0, cmd)
        with mock.patch("sys.stderr"):
            self.assertEqual(ipc.cli_trigger(["--trigger", "stop"]), 1)  # handler refuses -> failure

    def test_generated_hotkey_commands_still_point_at_ocr_scanner_py(self):
        cmd = core.trigger_cmd("region")
        self.assertIn("ocr_scanner.py --trigger region", cmd)
        self.assertTrue((ROOT / "ocr_scanner.py").exists())

    def test_legacy_command_set_is_preserved(self):
        self.assertEqual(set(core.VALID_COMMANDS), {"region", "current", "toggle", "start", "stop", "pause", "show"})
        for cmd in core.VALID_COMMANDS:
            self.assertIn(cmd, ipc.ALL_COMMANDS)
            self.assertIn(cmd, ipc.TRIGGER_COMMANDS)


class WindowsTokenTests(IsolatedEnv):
    """The Windows transport (loopback TCP + token) exercised on any OS by flipping the platform flag."""

    def test_requests_need_the_token(self):
        with mock.patch.object(ipc, "IS_WINDOWS", True):
            server = ipc.IPCServer(lambda c, a: {"ok": 1})
            self.assertTrue(server.bind())
            server.start()
            self.addCleanup(server.close)
            ep = json.loads(ipc.endpoint_file().read_text())
            self.assertEqual(ep["token"], server.token)
            self.assertTrue(ipc.request("ping")["ok"])

            def raw(payload: bytes):
                s = socket.create_connection(("127.0.0.1", ep["port"]), timeout=3)
                with s:
                    s.sendall(payload)
                    return json.loads(s.makefile().readline())

            self.assertEqual(raw(b'{"cmd": "ping"}\n')["error"]["code"], "unauthorized")
            self.assertEqual(raw(b'{"cmd": "ping", "token": "wrong"}\n')["error"]["code"], "unauthorized")
            s = socket.create_connection(("127.0.0.1", ep["port"]), timeout=3)
            with s:
                s.sendall(b"ping\n")  # legacy text has no token, so it is refused
                self.assertTrue(s.recv(100).startswith(b"error:"))
            self.assertEqual(server.sock.getsockname()[0], "127.0.0.1", "must be loopback only")
            server.close()
            self.assertFalse(ipc.endpoint_file().exists())


class InstanceLockTests(IsolatedEnv):
    @unittest.skipIf(IS_WIN, "uses flock semantics")
    def test_second_holder_is_refused_until_release(self):
        a, b = ipc.InstanceLock("daemon"), ipc.InstanceLock("daemon")
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    @unittest.skipIf(IS_WIN, "uses flock semantics")
    def test_different_names_do_not_collide(self):
        a, b = ipc.InstanceLock("daemon"), ipc.InstanceLock("gui")
        self.assertTrue(a.acquire() and b.acquire())
        a.release()
        b.release()


# daemon logic with fakes

class FakeCapturer:
    wayland = False
    portal = False
    uses_slurp = False
    backend = "fake"

    def __init__(self):
        self.region_err = None
        self.grabs = []
        self.color = 0

    def problem(self, need_region=False):
        return None

    def region_error(self, region):
        return self.region_err or (None if region else "No region is stored yet. Select a region first.")

    def grab_region(self, region):
        self.grabs.append(("region", region))
        err = self.region_error(region)
        if err:
            raise core.CaptureError(err)
        return Image.new("RGB", (60, 20), (self.color,) * 3)

    def grab_full(self, monitor):
        self.grabs.append(("full", monitor))
        return Image.new("RGB", (60, 20), (self.color,) * 3)

    def cancel_selection(self):
        pass


class FakeOCR:
    def __init__(self):
        self.texts = ["hello world"]
        self.calls = 0
        self.local = mock.Mock(problem=lambda: None)
        self.gate = None

    def chain(self, cfg):
        return ["local"]

    def recognize(self, img, max_side):
        self.calls += 1
        if self.gate:
            self.gate.wait(5)
        return self.texts[min(self.calls - 1, len(self.texts) - 1)], "Fake"


def make_daemon(test, **cfg_over):
    cfg = core.Config()
    cfg.data["output_file"] = str(test.base / "out" / "ocr.txt")
    cfg.data["interval_ms"] = 250
    cfg.data["notifications"] = False
    cfg.data.update(cfg_over)
    cap = FakeCapturer()
    d = ocr_daemon.Daemon(cfg=cfg, capturer=cap)
    d.ocr = d.service.ocr = FakeOCR()
    test.addCleanup(d.shutdown)
    return d, cap


class DaemonCommandTests(IsolatedEnv):
    def test_start_pause_resume_stop_status(self):
        d, cap = make_daemon(self)
        self.assertEqual(d.handle("status", {})["state"], "idle")
        self.assertEqual(d.handle("start", {})["state"], "recording")
        self.assertEqual(d.handle("start", {})["state"], "recording", "a second start must not add a second loop")
        live = [t for t in threading.enumerate() if isinstance(t, core.Recorder) and not t.stop_evt.is_set()]
        self.assertEqual(len(live), 1)
        self.assertEqual(d.handle("pause", {})["state"], "paused")
        self.assertEqual(d.handle("pause", {})["state"], "recording", "pause toggles, as the hotkey always did")
        d.handle("pause", {})
        self.assertEqual(d.handle("resume", {})["state"], "recording")
        self.assertEqual(d.handle("toggle", {})["state"], "idle")
        self.assertEqual(d.handle("toggle", {})["state"], "recording")
        self.assertEqual(d.handle("stop", {})["state"], "idle")
        self.assertEqual(d.handle("stop", {})["state"], "idle")

    def test_recording_writes_output_and_counts(self):
        d, cap = make_daemon(self)
        d.session.ensure()
        d.handle("start", {})
        self.assertTrue(wait_for(lambda: d.captures >= 1))
        d.handle("stop", {})
        self.assertEqual(d.lines_total, 1)
        self.assertEqual(d.session.path.read_text(), "hello world\n")

    def test_recording_continues_with_no_gui_and_gui_can_catch_up(self):
        d, cap = make_daemon(self, skip_unchanged=False, skip_duplicates=False)
        d.handle("start", {})
        self.assertFalse(d.gui_connected())
        self.assertTrue(wait_for(lambda: d.captures >= 2, 10), "recording must not depend on a GUI")
        reply = d.handle("events", {"since": 0})  # a GUI opening later sees the history
        self.assertTrue(d.gui_connected())
        kinds = [e["type"] for e in reply["events"]]
        self.assertIn("result", kinds)
        self.assertIn("state", kinds)
        self.assertEqual(reply["state"], "recording")
        later = d.handle("events", {"since": reply["last"]})
        self.assertTrue(all(e["id"] > reply["last"] for e in later["events"]))

    def test_region_mode_never_falls_back_to_full_screen(self):
        d, cap = make_daemon(self, mode="region", region=None)
        with self.assertRaises(ipc.IPCError) as cm:
            d.handle("start", {})
        self.assertEqual(cm.exception.code, "region_invalid")
        with self.assertRaises(ipc.IPCError):
            d.handle("current", {})
        self.assertEqual(cap.grabs, [])
        self.assertEqual(d.state, "idle")

    def test_scan_current_full_mode_and_busy_guard(self):
        d, cap = make_daemon(self)
        d.session.ensure()
        gate = threading.Event()
        d.ocr.gate = gate
        self.assertEqual(d.handle("current", {}), {"started": True})
        with self.assertRaises(ipc.IPCError) as cm:
            d.handle("current", {})
        self.assertEqual(cm.exception.code, "busy")
        gate.set()
        self.assertTrue(wait_for(lambda: d.captures == 1))
        self.assertTrue(wait_for(lambda: not d._busy["current"]))
        self.assertEqual(cap.grabs, [("full", "auto")])

    def test_ocr_unavailable_is_a_structured_error(self):
        d, _ = make_daemon(self)
        d.ocr.chain = lambda cfg: []
        d.ocr.local = mock.Mock(problem=lambda: "No engine.")
        for cmd in ("start", "current", "region"):
            with self.assertRaises(ipc.IPCError) as cm:
                d.handle(cmd, {})
            self.assertEqual(cm.exception.code, "ocr_unavailable")
            self.assertEqual(cm.exception.message, "No engine.")

    def test_region_command_asks_the_gui_and_scans_the_result_once(self):
        d, cap = make_daemon(self)
        d.session.ensure()
        d._gui_seen = time.monotonic()  # a GUI is polling
        self.assertEqual(d.handle("region", {})["selector"], "gui")
        with self.assertRaises(ipc.IPCError):
            d.handle("region", {})  # busy until the first one finishes
        sel = d.handle("events", {"since": 0})["select"]
        self.assertEqual(sel["purpose"], "once")
        d.handle("select_ack", {"id": sel["id"]})
        self.assertIsNone(d.handle("events", {"since": 0})["select"], "claimed requests are not offered twice")
        region = {"type": "mss", "left": 0, "top": 0, "width": 60, "height": 20}
        d.handle("scan_region", {"region": region})
        self.assertTrue(wait_for(lambda: d.captures == 1 and not d._busy["region"]))
        self.assertEqual(cap.grabs, [("region", region)])
        self.assertIsNone(d.cfg.data["region"], "the one-shot region must not be stored")

    def test_cancelled_selection_frees_the_busy_flag(self):
        d, _ = make_daemon(self)
        d._gui_seen = time.monotonic()
        d.handle("region", {})
        d.handle("scan_region", {"region": None, "silent": True})
        self.assertFalse(d._busy["region"])
        self.assertIsNone(d.handle("events", {"since": 0})["select"])
        d.handle("region", {})  # can start again

    def test_invalid_region_from_gui_is_rejected(self):
        d, cap = make_daemon(self)
        cap.region_err = "bad region"
        d._gui_seen = time.monotonic()
        d.handle("region", {})
        with self.assertRaises(ipc.IPCError) as cm:
            d.handle("scan_region", {"region": {"type": "mss"}})
        self.assertEqual(cm.exception.code, "region_invalid")
        self.assertFalse(d._busy["region"])

    def test_show_without_gui_starts_one_and_with_gui_notifies_it(self):
        d, _ = make_daemon(self)
        with mock.patch.object(ocr_daemon, "spawn_detached") as spawn:
            d.handle("show", {})
            d.handle("show", {})  # the second must not start another while the first boots
            self.assertEqual(spawn.call_count, 1)
            self.assertIn("--hidden", spawn.call_args[0][0])
        d._gui_seen = time.monotonic()
        d.handle("show", {})
        self.assertIn("show", [e["type"] for e in d.handle("events", {"since": 0})["events"]])

    def test_reload_config_picks_up_the_file_and_never_overwrites_it(self):
        d, _ = make_daemon(self)
        path = d.cfg.path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(d.cfg.data, interval_ms=1234, providers=json.loads(json.dumps(d.cfg.data["providers"])))
        data["providers"]["gemini"]["keys"] = "SECRET-KEY"
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_text()
        with mock.patch.object(d.hotkeys, "apply", return_value=(True, "ok", "ok")):
            d.handle("reload_config", {})
        self.assertEqual(d.d["interval_ms"], 1234)
        self.assertEqual(d.d["providers"]["gemini"]["keys"], "SECRET-KEY")
        d.shutdown()
        self.assertEqual(path.read_text(), before, "the daemon never writes the settings file")

    def test_status_never_contains_credentials(self):
        d, _ = make_daemon(self)
        d.d["providers"]["gemini"]["keys"] = "SECRET-KEY"
        blob = json.dumps([d.handle("status", {}), d.handle("events", {"since": 0})])
        self.assertNotIn("SECRET-KEY", blob)

    def test_hotkey_actions_run_the_same_commands(self):
        d, _ = make_daemon(self)
        d._run_hotkey("toggle")
        self.assertEqual(d.state, "recording")
        d._run_hotkey("pause")
        self.assertEqual(d.state, "paused")
        d._run_hotkey("toggle")
        self.assertEqual(d.state, "idle")

    def test_auto_stop_after_failures_updates_state(self):
        d, cap = make_daemon(self)
        cap.region_err = None
        cap.grab_full = mock.Mock(side_effect=core.CaptureError("screen gone"))
        with mock.patch.object(core.Recorder, "MAX_FAILS", 2):
            d.handle("start", {})
            self.assertTrue(wait_for(lambda: d.state == "idle", 15))
        types = [(e["type"], e.get("level")) for e in d.handle("events", {"since": 0})["events"]]
        self.assertIn(("log", "err"), types)

    def test_shutdown_is_idempotent_and_stops_recording(self):
        d, _ = make_daemon(self)
        d.handle("start", {})
        rec = d.rec
        d.shutdown()
        d.shutdown()
        self.assertTrue(rec.stop_evt.is_set())
        self.assertEqual(d.state, "idle")


# real processes

class EndToEndTests(IsolatedEnv):
    def run_cli(self, *args, script="ocr_daemon.py", timeout=60):
        return subprocess.run([sys.executable, str(ROOT / script), *args], cwd=ROOT, env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def spawn_daemon(self):
        proc = subprocess.Popen([sys.executable, str(ROOT / "ocr_daemon.py"), "run"], cwd=ROOT, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        self.assertTrue(wait_for(ipc.daemon_running, 20), "daemon did not come up")
        return proc

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.communicate()

    def test_lifecycle_duplicate_prevention_and_stop(self):
        first = self.spawn_daemon()
        second = self.run_cli("run")
        self.assertEqual(second.returncode, 0)
        self.assertIn("already running", second.stderr)
        self.assertIsNone(first.poll(), "the first daemon must be untouched by the second")
        self.assertEqual(self.run_cli("start").returncode, 0)  # also a no-op
        st = self.run_cli("status")
        self.assertEqual(st.returncode, 0, st.stderr)
        self.assertIn("state:      idle", st.stdout)
        self.assertEqual(self.run_cli("stop").returncode, 0)
        self.assertEqual(first.wait(10), 0)
        self.assertEqual(self.run_cli("status").returncode, 1)

    def test_start_command_spawns_a_detached_daemon(self):
        r = self.run_cli("start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(ipc.daemon_running())
        self.assertEqual(self.run_cli("stop").returncode, 0)
        self.assertTrue(wait_for(lambda: not ipc.daemon_running()))

    @unittest.skipIf(IS_WIN, "POSIX signals")
    def test_sigterm_shuts_down_cleanly_and_removes_the_socket(self):
        proc = self.spawn_daemon()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(10), 0)
        self.assertFalse(os.path.exists(ipc.ipc_path()))

    @unittest.skipIf(IS_WIN, "POSIX signals")
    def test_new_daemon_starts_after_a_crash(self):
        proc = self.spawn_daemon()
        proc.kill()  # no cleanup at all: stale socket, lock held by a dead process
        proc.wait(10)
        self.assertFalse(ipc.daemon_running())
        self.spawn_daemon()
        self.assertTrue(ipc.daemon_running())

    def test_trigger_through_the_gui_entry_point_without_qt(self):
        """`ocr_scanner.py --trigger ...` is what compositor bindings call; it must not need PySide6."""
        code = ("import sys; sys.modules['PySide6'] = None; sys.argv = ['ocr_scanner.py', '--trigger', %r]\n"
                "import runpy; runpy.run_path('ocr_scanner.py', run_name='__main__')")
        self.spawn_daemon()
        for cmd, rc in (("pause", 0), ("status", 0), ("nonsense", 2)):
            r = subprocess.run([sys.executable, "-c", code % cmd], cwd=ROOT, env=self.env, capture_output=True,
                               text=True, timeout=60)
            self.assertEqual(r.returncode, rc, (cmd, r.stdout, r.stderr))
        self.assertEqual(self.run_cli("stop").returncode, 0)
        r = subprocess.run([sys.executable, "-c", code % "pause"], cwd=ROOT, env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not running", r.stderr)

    def test_gui_link_connects_reconnects_and_survives_daemon_restart(self):
        updates, states = [], []
        link = ocr_client.DaemonLink(lambda d, replay: updates.append((d, replay)), states.append)
        with mock.patch.object(ocr_client, "POLL_INTERVAL", 0.1):
            link.start()
            self.addCleanup(link.stop)
            self.assertTrue(wait_for(lambda: states == [False]), "starts disconnected when there is no daemon")
            proc = self.spawn_daemon()
            self.assertTrue(wait_for(lambda: states == [False, True]), states)
            self.assertTrue(updates[0][1], "the first batch after connecting is flagged as replay")
            self.assertTrue(any(e["type"] == "log" for e in updates[0][0]["events"]))
            # the daemon dies (as if it crashed): the link notices and keeps trying
            proc.kill()
            proc.wait(10)
            self.assertTrue(wait_for(lambda: states[-1] is False))
            self.spawn_daemon()
            self.assertTrue(wait_for(lambda: states[-1] is True))
            self.assertTrue(wait_for(lambda: any(r for _, r in updates[1:])), "history replayed after reconnecting")

    def test_closing_the_gui_link_does_not_touch_the_daemon(self):
        proc = self.spawn_daemon()
        link = ocr_client.DaemonLink(lambda *a: None, lambda ok: None)
        link.start()
        self.assertTrue(wait_for(lambda: link.connected))
        link.stop()
        link.join(5)
        self.assertIsNone(proc.poll())
        self.assertTrue(ipc.daemon_running())
        link2 = ocr_client.DaemonLink(lambda *a: None, lambda ok: None)  # "reopening the GUI"
        link2.start()
        self.addCleanup(link2.stop)
        self.assertTrue(wait_for(lambda: link2.connected))


# autostart

class SystemdTests(IsolatedEnv):
    def test_unit_text(self):
        text = autostart.render_unit("/usr/bin/python3", "/opt/my app/ocr_daemon.py")
        self.assertIn('ExecStart="/usr/bin/python3" "/opt/my app/ocr_daemon.py" run', text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("WantedBy=graphical-session.target hyprland-session.target", text)
        self.assertNotIn("User=", text)
        self.assertIn("%%", autostart.render_unit("/p%y", "/s"))

    def test_enable_installs_enables_and_imports_the_session_environment(self):
        calls = []
        run = lambda cmd: (calls.append(cmd), (0, ""))[1]  # noqa: E731
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-1"}):
            ok, msg = autostart.systemd_enable(run=run, python="/py", script="/d.py")
        self.assertTrue(ok, msg)
        self.assertTrue(autostart.unit_path().exists())
        self.assertEqual(autostart.unit_path().parent, Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user")
        self.assertIn(["systemctl", "--user", "enable", "--now", "ocr-scanner.service"], calls)
        self.assertIn(["systemctl", "--user", "import-environment", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"], calls)
        self.assertTrue(all(c[:2] == ["systemctl", "--user"] for c in calls), "never needs root")

    def test_enable_reports_systemctl_failure(self):
        ok, msg = autostart.systemd_enable(run=lambda c: (1, "Failed to connect to bus"), python="/py", script="/d")
        self.assertFalse(ok)
        self.assertIn("Failed to connect to bus", msg)

    def test_disable_removes_the_unit(self):
        run = lambda cmd: (0, "")  # noqa: E731
        autostart.systemd_enable(run=run, python="/py", script="/d.py")
        ok, _ = autostart.systemd_disable(run=run)
        self.assertTrue(ok)
        self.assertFalse(autostart.unit_path().exists())
        ok, _ = autostart.systemd_disable(run=run)  # idempotent
        self.assertTrue(ok)

    def test_status_and_control(self):
        self.assertIn("not enabled", autostart.systemd_status(run=lambda c: (0, "")))
        run = lambda cmd: (0, "active" if "is-active" in cmd else "enabled")  # noqa: E731
        autostart.systemd_enable(run=lambda c: (0, ""), python="/py", script="/d")
        self.assertIn("enabled", autostart.systemd_status(run=run))
        calls = []
        autostart.systemd_control("restart", run=lambda c: (calls.append(c), (0, ""))[1])
        self.assertEqual(calls, [["systemctl", "--user", "restart", "ocr-scanner.service"]])


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.values = {}

    class _Key:
        def __init__(self, reg):
            self.reg = reg

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def CreateKey(self, root, path):
        self.path = path
        return self._Key(self)

    def OpenKey(self, root, path, res=0, access=0):
        return self._Key(self)

    def SetValueEx(self, key, name, _r, _t, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1


class WindowsAutostartTests(IsolatedEnv):
    def test_command_prefers_pythonw_and_runs_the_daemon_in_the_foreground(self):
        d = Path(self.tmp.name) / "py"
        d.mkdir()
        (d / "python.exe").write_text("")
        (d / "pythonw.exe").write_text("")
        cmd = autostart.windows_command(python=str(d / "python.exe"), script=r"C:\x\ocr_daemon.py")
        self.assertEqual(cmd, f'"{d / "pythonw.exe"}" "C:\\x\\ocr_daemon.py" run')

    def test_enable_disable_status_use_the_per_user_run_key(self):
        reg = FakeReg()
        started = []
        ok, msg = autostart.windows_enable(reg=reg, start=lambda: (started.append(1), (True, "Started."))[1])
        self.assertTrue(ok, msg)
        self.assertEqual(reg.path, autostart.RUN_KEY)
        self.assertIn("ocr_daemon.py", reg.values[autostart.RUN_VALUE])
        self.assertEqual(started, [1])
        self.assertIn("enabled", autostart.windows_status(reg=reg))
        self.assertTrue(autostart.windows_disable(reg=reg)[0])
        self.assertIn("not enabled", autostart.windows_status(reg=reg))
        self.assertTrue(autostart.windows_disable(reg=reg)[0], "disabling twice is fine")


if __name__ == "__main__":
    unittest.main()
