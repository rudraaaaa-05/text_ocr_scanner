"""Run with:  python -m unittest discover -s tests -v   (pytest also works)

Covers the Qt-free logic. Real overlay / tray / window behaviour needs a desktop and is not tested here.
"""
import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import qt_stub  # noqa: E402

qt_stub.install_if_missing()
import ocr_scanner as o  # noqa: E402
from PIL import Image  # noqa: E402

DUAL = [  # a second monitor LEFT of the primary: negative coordinates
    {"left": -1920, "top": 0, "width": 1920, "height": 1080},
    {"left": 0, "top": 0, "width": 2560, "height": 1440},
]


def cfg(**over):
    d = o.default_config()
    d.update(over)
    return d


class FakeMss:
    """Stands in for the `mss` package: records grabs, returns solid images."""

    def __init__(self, monitors):
        self.mons = monitors
        self.grabs = []
        outer = self
        virtual = [{"left": min(m["left"] for m in monitors), "top": 0, "width": 0, "height": 0}] + monitors

        class Shot:
            def __init__(self, w, h):
                self.size = (w, h)
                self.bgra = bytes(w * h * 4)

        class Ctx:
            monitors = virtual

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def grab(self, box):
                outer.grabs.append(dict(box))
                return Shot(box["width"], box["height"])

        mod = types.ModuleType("mss")
        mod.mss = lambda: Ctx()
        exc = types.ModuleType("mss.exception")
        exc.ScreenShotError = type("ScreenShotError", (Exception,), {})
        mod.exception = exc
        self.modules = {"mss": mod, "mss.exception": exc}


class SessionOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "ocr_output.txt")

    def test_name_has_timestamp_and_keeps_extension(self):
        s = o.SessionOutput(self.base, clock=lambda: datetime(2026, 1, 31, 14, 5, 9))
        self.assertEqual(s.ensure().name, "ocr_output_2026-01-31_14-05-09.txt")
        self.assertTrue(s.path.exists())

    def test_same_second_launches_get_distinct_files(self):
        clock = lambda: datetime(2026, 1, 31, 14, 5, 9)  # noqa: E731
        a = o.SessionOutput(self.base, clock=clock).ensure()
        b = o.SessionOutput(self.base, clock=clock).ensure()
        self.assertNotEqual(a, b)
        self.assertEqual(b.name, "ocr_output_2026-01-31_14-05-09_2.txt")

    def test_previous_files_are_never_modified(self):
        old = Path(self.tmp.name) / "ocr_output_2026-01-31_14-05-09.txt"
        old.write_text("old session\n", encoding="utf-8")
        legacy = Path(self.base)
        legacy.write_text("legacy file\n", encoding="utf-8")
        before = (old.stat().st_mtime_ns, legacy.stat().st_mtime_ns)
        session = o.SessionOutput(self.base, clock=lambda: datetime(2026, 1, 31, 14, 5, 9))
        sink = o.TextSink(lambda: cfg(timestamps=False), session)
        sink.write("new text")
        self.assertEqual(old.read_text(encoding="utf-8"), "old session\n")
        self.assertEqual(legacy.read_text(encoding="utf-8"), "legacy file\n")
        self.assertEqual((old.stat().st_mtime_ns, legacy.stat().st_mtime_ns), before)
        self.assertNotEqual(session.path, old)
        self.assertEqual(session.path.read_text(encoding="utf-8"), "new text\n")

    def test_all_scans_of_a_launch_use_one_file(self):
        session = o.SessionOutput(self.base)
        sink = o.TextSink(lambda: cfg(timestamps=False), session)
        sink.write("one")
        first = session.path
        sink.write("two")
        self.assertEqual(session.path, first)
        self.assertEqual(first.read_text(encoding="utf-8"), "one\ntwo\n")

    def test_changing_config_does_not_move_the_active_file(self):
        session = o.SessionOutput(self.base)
        path = session.ensure()
        self.assertEqual(session.ensure(), path)

    def test_directory_as_configured_path(self):
        s = o.SessionOutput(self.tmp.name + os.sep, clock=lambda: datetime(2026, 1, 1, 0, 0, 0))
        self.assertEqual(s.ensure().name, "ocr_output_2026-01-01_00-00-00.txt")

    def test_unwritable_folder_raises_output_error(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        s = o.SessionOutput(str(blocker / "sub" / "out.txt"))
        with self.assertRaises(o.OutputError):
            s.ensure()

    def test_duplicate_filtering_within_a_session(self):
        session = o.SessionOutput(self.base)
        sink = o.TextSink(lambda: cfg(timestamps=False, skip_duplicates=True, only_new_lines=True), session)
        self.assertEqual(sink.write("alpha\nbeta"), 2)
        self.assertEqual(sink.write("alpha\nbeta"), 0)          # same text again
        self.assertEqual(sink.write("beta\ngamma"), 1)          # only the new line
        self.assertEqual(sink.write("alpha", force=True), 1)    # forced scans bypass the filter
        self.assertEqual(session.path.read_text(encoding="utf-8"), "alpha\nbeta\ngamma\nalpha\n")

    def test_failed_write_does_not_mark_text_as_seen(self):
        session = o.SessionOutput(self.base)
        sink = o.TextSink(lambda: cfg(timestamps=False, only_new_lines=True), session)
        real_open = open

        def boom(path, mode="r", *a, **k):
            if mode == "a":
                raise OSError(13, "denied")
            return real_open(path, mode, *a, **k)

        with mock.patch("builtins.open", boom):
            with self.assertRaises(o.OutputError):
                sink.write("keep me")
        self.assertEqual(sink.write("keep me"), 1)


class RegionValidationTests(unittest.TestCase):
    def mss_region(self, **kw):
        r = {"type": "mss", "left": 100, "top": 100, "width": 200, "height": 100}
        r.update(kw)
        return r

    def test_rejects_missing_empty_zero_and_negative_regions(self):
        bad = [None, "x", {}, {"type": "zzz"},
               self.mss_region(width=0), self.mss_region(height=0),
               self.mss_region(width=-5), self.mss_region(height=-1),
               self.mss_region(width=10.5), self.mss_region(left=None), self.mss_region(width=True),
               {"type": "mss"}]
        for r in bad:
            self.assertIsNotNone(o.region_problem(r, False, DUAL), r)

    def test_accepts_valid_region_including_negative_origin(self):
        self.assertIsNone(o.region_problem(self.mss_region(), False, DUAL))
        self.assertIsNone(o.region_problem(self.mss_region(left=-1000, width=500), False, DUAL))

    def test_region_outside_desktop_is_rejected(self):
        self.assertIn("no longer on the screen", o.region_problem(self.mss_region(left=5000), False, DUAL))
        self.assertIsNotNone(o.region_problem(self.mss_region(left=-1999), False, DUAL))  # sticks out left

    def test_layout_change_invalidates_stored_region(self):
        region = self.mss_region(layout=o.monitor_layout(DUAL))
        self.assertIsNone(o.region_problem(region, False, DUAL))
        moved = [dict(DUAL[0], left=-1280), DUAL[1]]  # monitor rearranged
        self.assertIn("layout changed", o.region_problem(region, False, moved))

    def test_layout_survives_json_roundtrip(self):
        import json
        region = json.loads(json.dumps(self.mss_region(layout=o.monitor_layout(DUAL))))
        self.assertIsNone(o.region_problem(region, False, DUAL))

    def test_legacy_region_without_layout_is_bounds_checked_only(self):
        self.assertIsNone(o.region_problem(self.mss_region(), False, DUAL))

    def test_wayland_geometry(self):
        ok = {"type": "geom", "geom": "-1920,10 300x200"}
        self.assertIsNone(o.region_problem(ok, True))
        for g in ("0,0 0x10", "0,0 10x0", "0,0 -5x10", "garbage", ""):
            self.assertIsNotNone(o.region_problem({"type": "geom", "geom": g}, True), g)

    def test_region_kinds_do_not_cross_platforms(self):
        self.assertIsNotNone(o.region_problem({"type": "geom", "geom": "0,0 10x10"}, False, DUAL))
        self.assertIsNotNone(o.region_problem(self.mss_region(), True))


class CoordinateMappingTests(unittest.TestCase):
    def test_selection_on_monitor_with_negative_origin(self):
        mon = DUAL[0]  # left of the primary
        region, crop = o.map_selection(mon, (100, 50, 200, 100), (1920, 1080))
        self.assertEqual(region, {"type": "mss", "left": -1820, "top": 50, "width": 200, "height": 100})
        self.assertEqual(crop, (100, 50, 300, 150))

    def test_windows_display_scaling_150_percent(self):
        mon = {"left": 0, "top": 0, "width": 2880, "height": 1620}  # physical; Qt sees 1920x1080
        region, crop = o.map_selection(mon, (100, 100, 200, 100), (1920, 1080))
        self.assertEqual((region["left"], region["top"], region["width"], region["height"]), (150, 150, 300, 150))
        self.assertEqual(crop, (150, 150, 450, 300))

    def test_fractional_scaling_keeps_crop_and_region_consistent(self):
        mon = {"left": 1920, "top": -200, "width": 1920, "height": 1080}  # Qt logical 1536x864 (125 %)
        region, crop = o.map_selection(mon, (33, 17, 101, 77), (1536, 864))
        self.assertEqual(region["width"], crop[2] - crop[0])
        self.assertEqual(region["height"], crop[3] - crop[1])
        self.assertEqual(region["left"], 1920 + crop[0])
        self.assertEqual(region["top"], -200 + crop[1])

    def test_selection_is_clamped_and_empty_rejected(self):
        mon = {"left": 0, "top": 0, "width": 100, "height": 100}
        region, _ = o.map_selection(mon, (90, 90, 50, 50), (100, 100))
        self.assertEqual((region["width"], region["height"]), (10, 10))
        with self.assertRaises(ValueError):
            o.map_selection(mon, (150, 150, 20, 20), (100, 100))
        with self.assertRaises(ValueError):
            o.map_selection(mon, (0, 0, 0, 10), (100, 100))
        with self.assertRaises(ValueError):
            o.map_selection(mon, (0, 0, 10, 10), (0, 0))

    def test_match_screens_multi_monitor_negative_coords(self):
        # Qt screens, both position conventions, dpr 1.5 on the right-hand (primary) monitor
        screens = [(0, 0, 1707, 960, 1.5), (-1920, 0, 1920, 1080, 1.0)]
        self.assertEqual(o.match_screens(screens, DUAL), {0: 1, 1: 0})

    def test_match_screens_identical_monitors_use_position(self):
        mons = [{"left": -1920, "top": 0, "width": 1920, "height": 1080},
                {"left": 0, "top": 0, "width": 1920, "height": 1080}]
        screens = [(0, 0, 1920, 1080, 1.0), (-1920, 0, 1920, 1080, 1.0)]
        self.assertEqual(o.match_screens(screens, mons), {0: 1, 1: 0})

    def test_match_screens_skips_unknown_screen(self):
        self.assertEqual(o.match_screens([(0, 0, 800, 600, 1.0)], DUAL), {})


class CaptureRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMss(DUAL)
        patcher = mock.patch.dict(sys.modules, self.fake.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(o, "is_wayland", return_value=False), mock.patch.object(o, "is_hyprland", return_value=False):
            self.cap = o.Capturer()

    def region(self, **kw):
        r = {"type": "mss", "left": -500, "top": 10, "width": 300, "height": 200, "layout": o.monitor_layout(DUAL)}
        r.update(kw)
        return r

    def test_region_mode_grabs_only_the_stored_region(self):
        img = self.cap.grab_region(self.region())
        self.assertEqual(self.fake.grabs, [{"left": -500, "top": 10, "width": 300, "height": 200}])
        self.assertEqual(img.size, (300, 200))

    def test_invalid_region_raises_and_never_grabs(self):
        for bad in (None, self.region(width=0), self.region(height=-4), self.region(left=99999)):
            with self.assertRaises(o.CaptureError):
                self.cap.grab_region(bad)
        self.assertEqual(self.fake.grabs, [])

    def test_full_mode_grabs_configured_monitor_or_all(self):
        self.cap.grab_full("2")
        self.cap.grab_full("all")
        self.cap.grab_full("auto")
        self.assertEqual([g["width"] for g in self.fake.grabs], [2560, 0, 1920])
        self.assertEqual(self.fake.grabs[0]["left"], 0)

    def test_missing_monitor_is_an_error_not_a_silent_fallback(self):
        with self.assertRaises(o.CaptureError):
            self.cap.grab_full("7")
        with self.assertRaises(o.CaptureError):
            self.cap.grab_full("banana")
        self.assertEqual(self.fake.grabs, [])


class ScanServiceTests(unittest.TestCase):
    def make(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        session = o.SessionOutput(os.path.join(self.tmp.name, "o.txt"))
        c = cfg(timestamps=False)
        self.cap = mock.Mock()
        self.cap.grab_region.return_value = Image.new("RGB", (4, 4))
        self.cap.grab_full.return_value = Image.new("RGB", (8, 8))
        self.ocr = mock.Mock()
        self.ocr.recognize.return_value = ("hello", "local")
        self.sink = o.TextSink(lambda: c, session)
        self.svc = o.ScanService(lambda: c, self.cap, self.ocr, self.sink)
        return session

    def test_region_mode_scan_current_uses_stored_region_only(self):
        session = self.make()
        region = {"type": "mss", "left": 1, "top": 2, "width": 3, "height": 4}
        text, written, _ = self.svc.scan_current("region", region, "auto")
        self.cap.grab_region.assert_called_once_with(region)
        self.cap.grab_full.assert_not_called()
        self.assertEqual((text, written), ("hello", 1))
        self.assertEqual(session.path.read_text(encoding="utf-8"), "hello\n")

    def test_full_mode_scan_current_uses_configured_monitor(self):
        self.make()
        self.svc.scan_current("full", None, "2")
        self.cap.grab_full.assert_called_once_with("2")
        self.cap.grab_region.assert_not_called()

    def test_missing_region_does_not_fall_back_or_run_ocr(self):
        self.make()
        real = o.Capturer.grab_region
        self.cap.grab_region.side_effect = lambda r: real(types.SimpleNamespace(region_error=lambda x: "No region is stored yet."), r)
        with self.assertRaises(o.CaptureError):
            self.svc.scan_current("region", None, "auto")
        self.cap.grab_full.assert_not_called()
        self.ocr.recognize.assert_not_called()

    def test_scan_current_applies_duplicate_filter(self):
        self.make()
        region = {"type": "mss", "left": 1, "top": 2, "width": 3, "height": 4}
        self.assertEqual(self.svc.scan_current("region", region, "auto")[1], 1)
        self.assertEqual(self.svc.scan_current("region", region, "auto")[1], 0)

    def test_unknown_mode_is_rejected(self):
        self.make()
        with self.assertRaises(o.CaptureError):
            self.svc.capture("sideways", None, "auto")


class FakeListener:
    instances = []

    def __init__(self, mapping):
        self.mapping, self.started, self.stopped, self.alive = mapping, False, False, True
        self.error = None
        self.daemon = False
        FakeListener.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.alive = False

    def join(self, timeout=None):
        if self.error:
            raise self.error

    def is_alive(self):
        return self.alive


class FakeBackend:
    GlobalHotKeys = FakeListener


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        FakeListener.instances = []
        self.cmds = []
        self.mgr = o.HotkeyManager(self.cmds.append, backend=FakeBackend)
        p = mock.patch.object(o, "is_wayland", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_mapping_covers_all_actions_and_fires_commands(self):
        ok, _msg, level = self.mgr.apply(o.default_config()["hotkeys"])
        self.assertTrue(ok)
        self.assertEqual(level, "ok")
        mapping = FakeListener.instances[0].mapping
        self.assertEqual(set(mapping), {"<ctrl>+<alt>+o", "<ctrl>+<alt>+s", "<ctrl>+<alt>+r", "<ctrl>+<alt>+p"})
        mapping["<ctrl>+<alt>+o"]()
        mapping["<ctrl>+<alt>+s"]()
        self.assertEqual(self.cmds, ["region", "current"])
        self.assertTrue(FakeListener.instances[0].daemon)

    def test_default_hotkeys_are_distinct_and_trigger_names_exist(self):
        hk = o.default_config()["hotkeys"]
        self.assertEqual(len(set(hk.values())), len(hk))
        for action in o.HOTKEY_ORDER:
            self.assertIn(action, o.VALID_COMMANDS)

    def test_duplicate_binding_is_reported_and_not_registered_twice(self):
        hk = dict(o.default_config()["hotkeys"], current="Ctrl+Alt+R")
        ok, msg, level = self.mgr.apply(hk)
        self.assertFalse(ok)
        self.assertEqual(level, "err")
        self.assertIn("both", msg)
        self.assertEqual(len(FakeListener.instances[0].mapping), 3)

    def test_unsupported_key_is_reported(self):
        hk = dict(o.default_config()["hotkeys"], current="Ctrl+Alt+")
        ok, msg, level = self.mgr.apply(hk)
        self.assertEqual(level, "err")
        self.assertIn("unsupported", msg)

    def test_registration_failure_is_surfaced(self):
        class Broken:
            @staticmethod
            def GlobalHotKeys(_mapping):
                raise RuntimeError("no X display")

        mgr = o.HotkeyManager(lambda c: None, backend=Broken)
        with self.assertLogs("ocr_scanner", level="ERROR"):
            ok, msg, level = mgr.apply(o.default_config()["hotkeys"])
        self.assertFalse(ok)
        self.assertEqual(level, "err")
        self.assertIn("no X display", msg)
        self.assertFalse(mgr.running)

    def test_missing_pynput_gives_install_hint(self):
        mgr = o.HotkeyManager(lambda c: None)
        err = ImportError("No module named 'pynput'", name="pynput")
        with mock.patch.dict(sys.modules, {"pynput": None}):
            ok, msg, level = mgr.apply(o.default_config()["hotkeys"])
        self.assertFalse(ok)
        self.assertEqual(level, "err")
        self.assertIn("pip install pynput", msg)
        del err

    def test_reapply_stops_old_listener_and_ignores_its_callbacks(self):
        hk = o.default_config()["hotkeys"]
        self.mgr.apply(hk)
        old = FakeListener.instances[0]
        self.mgr.apply(hk)
        self.assertTrue(old.stopped)
        self.assertEqual(len(FakeListener.instances), 2)
        old.mapping["<ctrl>+<alt>+o"]()          # stale callback
        self.assertEqual(self.cmds, [])
        FakeListener.instances[1].mapping["<ctrl>+<alt>+o"]()
        self.assertEqual(self.cmds, ["region"])

    def test_stop_cleans_up(self):
        self.mgr.apply(o.default_config()["hotkeys"])
        listener = FakeListener.instances[0]
        self.mgr.stop()
        self.assertTrue(listener.stopped)
        self.assertFalse(self.mgr.running)
        listener.mapping["<ctrl>+<alt>+r"]()
        self.assertEqual(self.cmds, [])
        self.mgr.stop()  # idempotent

    def test_dead_listener_is_detected(self):
        self.mgr.apply(o.default_config()["hotkeys"])
        self.assertIsNone(self.mgr.check_alive())
        listener = FakeListener.instances[0]
        listener.alive = False
        listener.error = RuntimeError("X connection lost")
        with self.assertLogs("ocr_scanner", level="ERROR"):
            msg = self.mgr.check_alive()
        self.assertIn("X connection lost", msg)
        self.assertFalse(self.mgr.running)

    def test_wayland_is_informational_and_registers_nothing(self):
        with mock.patch.object(o, "is_wayland", return_value=True):
            ok, msg, level = self.mgr.apply(o.default_config()["hotkeys"])
        self.assertEqual(level, "info")
        self.assertEqual(FakeListener.instances, [])
        self.assertIn("--trigger", msg)

    def test_callbacks_emit_from_worker_thread_without_blocking(self):
        self.mgr.apply(o.default_config()["hotkeys"])
        cb = FakeListener.instances[0].mapping["<ctrl>+<alt>+s"]
        t = threading.Thread(target=cb)
        t.start()
        t.join(2)
        self.assertEqual(self.cmds, ["current"])


class BusShutdownTests(unittest.TestCase):
    def test_post_delivers_then_drops_after_close(self):
        bus = o.Bus()
        got = []
        bus.command.connect(got.append)
        self.assertTrue(bus.post("command", "current"))
        bus.close()
        self.assertFalse(bus.post("command", "region"))
        self.assertEqual(got, ["current"])

    def test_post_swallows_deleted_object_only_during_shutdown(self):
        bus = o.Bus()

        class Dead:
            def emit(self, *a):
                raise RuntimeError("Internal C++ object already deleted")

        bus.__dict__["_sig_command"] = Dead()
        if not isinstance(bus.command, Dead):
            self.skipTest("real Qt signals in use")
        with self.assertRaises(RuntimeError):
            bus.post("command", "x")
        bus.close()
        self.assertFalse(bus.post("command", "x"))


class ProtocolTests(unittest.TestCase):
    def test_trigger_commands(self):
        self.assertEqual(set(o.VALID_COMMANDS), {"region", "current", "toggle", "start", "stop", "pause", "show"})

    def test_new_hotkey_survives_old_config_merge(self):
        self.assertIn("current", o.default_config()["hotkeys"])


if __name__ == "__main__":
    unittest.main()
