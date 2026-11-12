"""Capture on Wayland compositors without grim/slurp support (GNOME, KDE): protocol probe, backend choice,
portal screenshot, desktop splitting and portal regions. Everything runs against fakes: no display, no D-Bus,
no real compositor or portal. See COMPATIBILITY.md for what has been verified on a real system.

Run with:  python -m unittest discover -s tests -v
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OCR_SCANNER_KEYRING", "off")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import ocr_core as core  # noqa: E402
import ocr_portal as portal  # noqa: E402
import ocr_screenshot as shot  # noqa: E402
import ocr_wayland as wl  # noqa: E402

LINUX_ONLY = unittest.skipUnless(sys.platform.startswith("linux"), "Wayland detection is Linux only")


def _str(s: str) -> bytes:
    raw = s.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def fake_compositor(interfaces, error=False):
    """A socketpair end that answers wl_display.get_registry + sync like a compositor with `interfaces`."""
    client, server = socket.socketpair()
    client.settimeout(3)

    def serve():
        try:
            data = b""
            while len(data) < 24:
                data += server.recv(24 - len(data))
            out = b""
            if error:
                body = struct.pack("<I", 1) + struct.pack("<I", 0) + _str("boom")
                out = struct.pack("<II", 1, ((8 + len(body)) << 16) | 0) + body
            else:
                for i, name in enumerate(interfaces, 1):
                    body = struct.pack("<I", i) + _str(name) + struct.pack("<I", 1)
                    out += struct.pack("<II", 2, ((8 + len(body)) << 16) | 0) + body
                out += struct.pack("<II", 3, (8 << 16) | 0)  # wl_callback.done
            server.sendall(out)
        finally:
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    return client


class ProtocolProbe(unittest.TestCase):
    def probe(self, *args, **kw):
        sock = fake_compositor(*args, **kw)
        self.addCleanup(sock.close)
        return wl.list_globals(sock=sock)

    def test_reads_the_advertised_interfaces(self):
        names = self.probe(["wl_compositor", "zwlr_layer_shell_v1"])
        self.assertEqual(names, {"wl_compositor", "zwlr_layer_shell_v1"})

    def test_gnome_like_compositor_supports_neither_tool(self):
        caps = wl.capabilities(globals_={"wl_compositor", "xdg_wm_base", "wl_shm"})
        self.assertFalse(caps["grim"])
        self.assertFalse(caps["slurp"])

    def test_wlroots_like_compositor_supports_both(self):
        caps = wl.capabilities(globals_={"zwlr_screencopy_manager_v1", "zwlr_layer_shell_v1"})
        self.assertTrue(caps["grim"] and caps["slurp"])

    def test_newer_screen_copy_protocol_counts_for_grim(self):
        caps = wl.capabilities(globals_={"ext_image_copy_capture_manager_v1", "zwlr_layer_shell_v1"})
        self.assertTrue(caps["grim"])

    def test_protocol_error_gives_no_answer(self):
        self.assertIsNone(self.probe([], error=True))

    def test_no_socket_gives_no_answer(self):
        self.assertIsNone(wl.list_globals(env={}))
        self.assertIsNone(wl.list_globals(env={"WAYLAND_DISPLAY": "wayland-9", "XDG_RUNTIME_DIR": "/nonexistent"}))
        self.assertIsNone(wl.capabilities(env={}))


WAYLAND_ENV = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}


def capturer(env, caps=None):
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch("ocr_wayland.capabilities", return_value=caps):
        return core.Capturer()


@LINUX_ONLY
class BackendChoice(unittest.TestCase):
    GNOME = {"grim": False, "slurp": False, "globals": set()}
    WLROOTS = {"grim": True, "slurp": True, "globals": set()}

    def test_gnome_uses_the_portal(self):
        c = capturer({**WAYLAND_ENV, "XDG_CURRENT_DESKTOP": "GNOME"}, self.GNOME)
        self.assertTrue(c.portal)
        self.assertFalse(c.uses_slurp)
        self.assertIn("gnome", c.portal_reason)
        self.assertIn("portal", c.backend)

    def test_gnome_prefers_portal_even_if_wlroots_globals_are_present(self):
        caps = {"grim": True, "slurp": True, "globals": set()}
        c = capturer({**WAYLAND_ENV, "XDG_CURRENT_DESKTOP": "GNOME"}, caps)
        self.assertTrue(c.portal)
        self.assertFalse(c.uses_slurp)

    def test_wlroots_keeps_grim_and_slurp(self):
        c = capturer({**WAYLAND_ENV, "XDG_CURRENT_DESKTOP": "sway"}, self.WLROOTS)
        self.assertFalse(c.portal)
        self.assertTrue(c.uses_slurp)
        self.assertEqual(c.backend, "grim + slurp (Wayland)")

    def test_hyprland_is_never_probed_or_switched(self):
        c = capturer({**WAYLAND_ENV, "HYPRLAND_INSTANCE_SIGNATURE": "x"}, self.GNOME)
        self.assertFalse(c.portal)

    def test_unanswered_probe_falls_back_to_the_desktop_name(self):
        self.assertTrue(capturer({**WAYLAND_ENV, "XDG_CURRENT_DESKTOP": "KDE"}, None).portal)
        self.assertFalse(capturer({**WAYLAND_ENV, "XDG_CURRENT_DESKTOP": "sway"}, None).portal)

    def test_x11_never_uses_the_portal(self):
        c = capturer({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "GNOME"}, self.GNOME)
        self.assertFalse(c.portal)
        self.assertFalse(c.uses_slurp)

    def test_override_wins(self):
        self.assertTrue(capturer({**WAYLAND_ENV, "OCR_CAPTURE": "portal"}, self.WLROOTS).portal)
        self.assertFalse(capturer({**WAYLAND_ENV, "OCR_CAPTURE": "grim", "XDG_CURRENT_DESKTOP": "GNOME"},
                                  self.GNOME).portal)

    def test_portal_backend_does_not_need_grim_or_slurp(self):
        c = capturer({**WAYLAND_ENV, "XDG_CURRENT_DESKTOP": "GNOME"}, self.GNOME)
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertIsNone(c.problem(need_region=True))
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertIn("jeepney", c.problem())

    def test_monitor_list_is_one_desktop_entry(self):
        c = capturer({**WAYLAND_ENV, "XDG_CURRENT_DESKTOP": "GNOME"}, self.GNOME)
        self.assertEqual(c.list_monitors(), [("Whole desktop (all monitors)", "auto")])


class SplitDesktop(unittest.TestCase):
    def test_two_screens_side_by_side(self):
        img = Image.new("RGB", (3840, 1080))
        parts = core.split_desktop_image(img, [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)])
        self.assertEqual([m for m, _ in parts],
                         [{"left": 0, "top": 0, "width": 1920, "height": 1080},
                          {"left": 1920, "top": 0, "width": 1920, "height": 1080}])
        self.assertEqual(parts[1][1].size, (1920, 1080))

    def test_scaled_desktop_and_negative_origin(self):
        img = Image.new("RGB", (2 * 3000, 2 * 1000))  # 2x scale over a 3000x1000 logical desktop
        parts = core.split_desktop_image(img, [(-1000, 0, 1000, 1000), (0, 0, 2000, 1000)])
        self.assertEqual(parts[0][0], {"left": 0, "top": 0, "width": 2000, "height": 2000})
        self.assertEqual(parts[1][0], {"left": 2000, "top": 0, "width": 4000, "height": 2000})

    def test_empty_screen_list_is_an_error(self):
        with self.assertRaises(core.CaptureError):
            core.split_desktop_image(Image.new("RGB", (10, 10)), [])


class PortalRegions(unittest.TestCase):
    def setUp(self):
        self.region = {"type": "portal", "left": 100, "top": 50, "width": 40, "height": 20}

    def make(self, portal_on=True):
        c = core.Capturer.__new__(core.Capturer)
        c.wayland, c.hypr, c.portal, c.portal_reason = True, False, portal_on, ""
        c._slurp, c._slurp_lock = None, threading.Lock()
        return c

    def test_validation(self):
        self.assertIsNone(core.region_problem(self.region, True))
        self.assertIn("Wayland", core.region_problem(self.region, False))
        self.assertIn("zero", core.region_problem({**self.region, "width": 0}, True))
        self.assertIn("malformed", core.region_problem({**self.region, "left": -1}, True))
        self.assertIn("malformed", core.region_problem({**self.region, "top": "5"}, True))

    def test_regions_from_the_other_backend_are_refused(self):
        self.assertIsNone(self.make(True).region_error(self.region))
        self.assertIn("grim", self.make(True).region_error({"type": "geom", "geom": "1,2 30x40"}))
        self.assertIn("portal", self.make(False).region_error(self.region))

    def test_grab_region_crops_a_fresh_desktop_screenshot(self):
        c = self.make()
        desktop = Image.new("RGB", (400, 200), "white")
        desktop.paste((255, 0, 0), (100, 50, 140, 70))
        with mock.patch.object(c, "grab_desktop", return_value=desktop):
            img = c.grab_region(self.region)
        self.assertEqual(img.size, (40, 20))
        self.assertEqual(img.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(img.getpixel((39, 19)), (255, 0, 0))

    def test_region_outside_the_screenshot_asks_to_select_again(self):
        c = self.make()
        with mock.patch.object(c, "grab_desktop", return_value=Image.new("RGB", (120, 60))):
            with self.assertRaises(core.CaptureError) as cm:
                c.grab_region(self.region)
        self.assertIn("Select the region again", str(cm.exception))

    def test_full_screen_mode_takes_the_whole_desktop(self):
        c = self.make()
        with mock.patch.object(c, "grab_desktop", return_value="IMG"):
            self.assertEqual(c.grab_full("auto"), "IMG")
            self.assertEqual(c.grab_full("all"), "IMG")


class FakeBus:
    """Stand-in for JeepneyBus that answers Screenshot with a Request/Response signal."""

    def __init__(self, response=None, errors=None, image_path=None):
        self.calls, self.signals, self.closed = [], [], False
        self.response, self.errors = response, errors or {}
        self.image_path = image_path

    def subscribe(self, iface, member):
        pass

    def call(self, path, iface, member, signature="", body=(), timeout=10.0, dest=portal.PORTAL_BUS):
        self.calls.append((iface, member, body))
        if member in self.errors:
            raise self.errors[member]
        if member == "Screenshot":
            handle = "/org/freedesktop/portal/desktop/request/1_42/ocr"
            if self.response is not None:
                code, results = self.response
                if self.image_path and code == 0:
                    results = {"uri": ("s", "file://" + self.image_path)}
                self.signals.append((handle, portal.REQUEST_IFACE, "Response", (code, results)))
            return (handle,)
        return ()

    def next_signal(self, timeout):
        return self.signals.pop(0) if self.signals else None

    def close(self):
        self.closed = True


class PortalScreenshot(unittest.TestCase):
    def setUp(self):
        shot._portal_permission_granted = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "Screenshot from 2026.png")  # a space, as real file names have
        Image.new("RGB", (64, 32), (10, 20, 30)).save(self.path)

    def take(self, **kw):
        bus = FakeBus(**kw)
        return bus, shot.take_screenshot(lambda: bus, timeout=1.0)

    def test_success_returns_the_image_and_removes_the_file(self):
        bus, img = self.take(response=(0, {}), image_path=self.path)
        self.assertEqual(img.size, (64, 32))
        self.assertEqual(img.getpixel((0, 0)), (10, 20, 30))
        self.assertFalse(os.path.exists(self.path), "the portal's file must not pile up during recording")
        self.assertTrue(bus.closed)
        names = [c[1] for c in bus.calls]
        self.assertEqual(names, ["Register", "Screenshot"])
        options = bus.calls[1][2][1]
        self.assertEqual(options["interactive"], ("b", True))

    def test_permission_is_requested_only_once(self):
        bus1 = FakeBus(response=(0, {}), image_path=self.path)
        shot.take_screenshot(lambda: bus1, timeout=1.0)
        Image.new("RGB", (64, 32), (10, 20, 30)).save(self.path)
        bus2 = FakeBus(response=(0, {}), image_path=self.path)
        shot.take_screenshot(lambda: bus2, timeout=1.0)
        self.assertEqual(bus2.calls[1][2][1]["interactive"], ("b", False))

    def test_percent_encoded_uri_is_decoded(self):
        bus = FakeBus(response=(0, {}), image_path=self.path.replace(" ", "%20"))
        img = shot.take_screenshot(lambda: bus, timeout=1.0)
        self.assertEqual(img.size, (64, 32))
        self.assertEqual(shot._path_from_uri("file:///tmp/a%20b.png"), "/tmp/a b.png")
        with self.assertRaises(core.CaptureError):
            shot._path_from_uri("https://example.com/a.png")

    def test_cancelled_and_refused(self):
        with self.assertRaises(core.CaptureError) as cm:
            self.take(response=(1, {}))
        self.assertIn("cancelled", str(cm.exception))
        with self.assertRaises(core.CaptureError) as cm:
            self.take(response=(2, {}))
        self.assertIn("refused", str(cm.exception))

    def test_no_answer_times_out_with_a_hint(self):
        with self.assertRaises(core.CaptureError) as cm:
            self.take(response=None)
        self.assertIn("permission dialog", str(cm.exception))

    def test_missing_portal_is_explained(self):
        err = portal.BusError("x", "org.freedesktop.DBus.Error.ServiceUnknown")
        with self.assertRaises(core.CaptureError) as cm:
            self.take(errors={"Screenshot": err})
        self.assertIn("xdg-desktop-portal", str(cm.exception))

    def test_registration_failure_is_not_fatal(self):
        err = portal.BusError("already registered", "org.freedesktop.DBus.Error.Failed")
        _, img = self.take(response=(0, {}), image_path=self.path, errors={"Register": err})
        self.assertEqual(img.size, (64, 32))

    def test_no_session_bus(self):
        def broken():
            raise portal.BusError("cannot connect to the D-Bus session bus")
        with self.assertRaises(core.CaptureError):
            shot.take_screenshot(broken)

    def test_answer_without_a_file(self):
        with self.assertRaises(core.CaptureError):
            self.take(response=(0, {}))


if __name__ == "__main__":
    unittest.main()
