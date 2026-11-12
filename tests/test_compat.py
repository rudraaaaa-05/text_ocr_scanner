"""Cross-platform compatibility tests: session detection, credential storage and migration, portal hotkeys,
manual bind help, autostart targets. No display, D-Bus, keyring or network needed.

What these do NOT prove: that the real `keyring` backends, `jeepney`, any real desktop portal, GNOME, KDE
or Windows behave as the fakes below do. See COMPATIBILITY.md for what was verified on real systems.

Run with:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OCR_SCANNER_KEYRING", "off")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ocr_autostart as autostart  # noqa: E402
import ocr_core as core  # noqa: E402
import ocr_daemon  # noqa: E402
import ocr_portal as portal  # noqa: E402
import ocr_secrets as secrets_mod  # noqa: E402
from ocr_secrets import SecretStore, SecretStoreError  # noqa: E402

LINUX_ONLY = unittest.skipUnless(sys.platform.startswith("linux"), "Linux session detection")
KEY = "AIzaSyFAKE-test-key-1234567890"
KEY2 = "sk-another-FAKE-key-abcdef"


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / "run").mkdir(mode=0o700)
        (base / "home").mkdir()
        p = mock.patch.dict(os.environ, {"HOME": str(base / "home"), "XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_RUNTIME_DIR": str(base / "run"), "APPDATA": str(base / "appdata")})
        p.start()
        self.addCleanup(p.stop)
        self.base = base


# session detection

@LINUX_ONLY
class DetectionTests(unittest.TestCase):
    def test_desktops(self):
        cases = [
            ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "gnome"),
            ({"XDG_CURRENT_DESKTOP": "GNOME"}, "gnome"),
            ({"XDG_CURRENT_DESKTOP": "KDE"}, "kde"),
            ({"DESKTOP_SESSION": "plasma"}, "kde"),
            ({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, "cinnamon"),
            ({"XDG_CURRENT_DESKTOP": "XFCE"}, "xfce"),
            ({"XDG_CURRENT_DESKTOP": "sway"}, "sway"),
            ({"SWAYSOCK": "/run/user/1000/sway.sock"}, "sway"),
            ({"HYPRLAND_INSTANCE_SIGNATURE": "abc"}, "hyprland"),
            ({"XDG_CURRENT_DESKTOP": "Hyprland"}, "hyprland"),
            ({}, "unknown"),
            ({"XDG_CURRENT_DESKTOP": "SomethingNew"}, "unknown"),
        ]
        for env, want in cases:
            self.assertEqual(core.detect_desktop(env), want, env)

    def test_hyprland_signature_wins_over_other_names(self):
        self.assertEqual(core.detect_desktop({"HYPRLAND_INSTANCE_SIGNATURE": "x", "XDG_CURRENT_DESKTOP": "GNOME"}), "hyprland")

    def test_session_types(self):
        self.assertEqual(core.detect_session_type({"XDG_SESSION_TYPE": "wayland"}), "wayland")
        self.assertEqual(core.detect_session_type({"WAYLAND_DISPLAY": "wayland-0"}), "wayland")
        self.assertEqual(core.detect_session_type({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}), "x11")
        self.assertEqual(core.detect_session_type({"DISPLAY": ":0"}), "x11")
        self.assertEqual(core.detect_session_type({}), "unknown")

    def test_session_type_agrees_with_capture_routing_helper(self):
        for env in ({"XDG_SESSION_TYPE": "wayland"}, {"WAYLAND_DISPLAY": "w-1"}, {"XDG_SESSION_TYPE": "x11"}, {}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(core.detect_session_type() == "wayland", core.is_wayland(), env)

    def test_distro_info(self):
        samples = {
            "arch": ('NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\n', "arch", []),
            "debian": ('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\n', "debian", []),
            "ubuntu": ('PRETTY_NAME="Ubuntu 24.04 LTS"\nID=ubuntu\nID_LIKE=debian\n', "ubuntu", ["debian"]),
            "mint": ('PRETTY_NAME="Linux Mint 22"\nID=linuxmint\nID_LIKE="ubuntu debian"\n', "linuxmint", ["ubuntu", "debian"]),
        }
        with tempfile.TemporaryDirectory() as d:
            for name, (text, want_id, want_like) in samples.items():
                path = Path(d) / name
                path.write_text(text)
                info = core.distro_info(str(path))
                self.assertEqual((info["id"], info["like"]), (want_id, want_like), name)
                self.assertTrue(info["name"])
        self.assertEqual(core.distro_info("/nonexistent/os-release"), {})

    def test_detect_session_has_expected_keys(self):
        info = core.detect_session({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"})
        self.assertEqual((info["os"], info["session_type"], info["desktop"]), ("linux", "wayland", "kde"))


# credential store

class FakeKeyring:
    def __init__(self):
        self.items = {}
        self.sets = 0

    def get_password(self, service, user):
        return self.items.get((service, user))

    def set_password(self, service, user, value):
        self.sets += 1
        self.items[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.items:
            raise type("PasswordDeleteError", (Exception,), {})("not found")
        del self.items[(service, user)]


class LossyKeyring(FakeKeyring):
    """Accepts writes but returns something else when read back."""

    def get_password(self, service, user):
        v = super().get_password(service, user)
        return None if v is None else v[:-1]


class FailingKeyring(FakeKeyring):
    def set_password(self, service, user, value):
        raise RuntimeError(f"collection is locked while storing {value}")  # a backend that echoes the secret


class DownStore(SecretStore):
    def __init__(self, why="no Secret Service is running"):
        super().__init__()
        self._checked, self._problem = True, why


def write_config(base, keys):
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "ocr-scanner" / "config.json"
    cfg = core.default_config()
    for pid, key in keys.items():
        cfg["providers"][pid]["keys"] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg))
    return path


class SecretStoreTests(Isolated):
    def test_disabled_by_environment(self):
        with mock.patch.dict(os.environ, {secrets_mod.DISABLE_ENV: "off"}):
            self.assertIn(secrets_mod.DISABLE_ENV, SecretStore().problem())

    def test_a_keyring_that_appears_later_is_picked_up(self):
        store = SecretStore()
        with mock.patch.dict(os.environ, {secrets_mod.DISABLE_ENV: "off"}):
            self.assertTrue(store.problem())
        backend_cls = type("Backend", (), {"__module__": "keyring.backends.SecretService"})
        fake = types.SimpleNamespace(get_keyring=lambda: backend_cls())
        with mock.patch.dict(os.environ, {secrets_mod.DISABLE_ENV: "on"}), mock.patch.dict(sys.modules, {"keyring": fake}):
            self.assertIsNone(store.problem())
            self.assertTrue(store.available)

    def test_missing_keyring_package_says_how_to_install(self):
        with mock.patch.dict(os.environ, {secrets_mod.DISABLE_ENV: "on"}), mock.patch.dict(sys.modules, {"keyring": None}):
            self.assertIn("pip install keyring", SecretStore().problem())

    def _fake_keyring_module(self, module_name):
        backend_cls = type("Backend", (), {"__module__": module_name})
        return types.SimpleNamespace(get_keyring=lambda: backend_cls())

    def test_insecure_or_null_backends_are_refused(self):
        for module in ("keyring.backends.fail", "keyring.backends.null", "keyrings.alt.file"):
            with mock.patch.dict(os.environ, {secrets_mod.DISABLE_ENV: "on"}), \
                    mock.patch.dict(sys.modules, {"keyring": self._fake_keyring_module(module)}):
                self.assertIn("no secure credential store", SecretStore().problem(), module)

    def test_real_looking_backend_is_accepted(self):
        for module in ("keyring.backends.SecretService", "keyring.backends.Windows", "keyring.backends.kwallet"):
            with mock.patch.dict(os.environ, {secrets_mod.DISABLE_ENV: "on"}), \
                    mock.patch.dict(sys.modules, {"keyring": self._fake_keyring_module(module)}):
                self.assertIsNone(SecretStore().problem(), module)

    def test_errors_never_contain_the_secret(self):
        store = SecretStore(backend=FailingKeyring())
        with self.assertRaises(SecretStoreError) as cm:
            store.set("gemini", KEY)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertIn("RuntimeError", str(cm.exception))

    def test_windows_credential_size_limit_gives_a_clear_error_instead_of_a_crash(self):
        store = SecretStore(backend=FakeKeyring())
        with mock.patch.object(secrets_mod.sys, "platform", "win32"):
            store.set("gemini", "k" * 1280)
            with self.assertRaises(SecretStoreError) as cm:
                store.set("gemini", "k" * 1281)
        self.assertIn("too long", str(cm.exception))
        self.assertNotIn("k" * 20, str(cm.exception))

    def test_delete_of_missing_entry_is_not_an_error(self):
        SecretStore(backend=FakeKeyring()).delete("gemini")

    def test_set_verified_detects_a_store_that_does_not_keep_the_value(self):
        with self.assertRaises(SecretStoreError):
            SecretStore(backend=LossyKeyring()).set_verified("gemini", KEY)

    def test_unavailable_store_raises_readable_error(self):
        with self.assertRaises(SecretStoreError) as cm:
            DownStore().get("gemini")
        self.assertIn("no Secret Service is running", str(cm.exception))


class ConfigKeyTests(Isolated):
    def make(self, store, writer=True, keys=None):
        path = write_config(self.base, keys if keys is not None else {"gemini": KEY, "groq": KEY2})
        return core.Config(writer=writer, secrets=SecretStore(backend=store) if not isinstance(store, SecretStore) else store), path

    def file_text(self, path):
        return path.read_text(encoding="utf-8")

    def test_migration_moves_keys_verifies_then_removes_plaintext(self):
        kr = FakeKeyring()
        cfg, path = self.make(kr)
        self.assertEqual(sorted(cfg.migrated), ["gemini", "groq"])
        self.assertEqual(kr.items[("ocr-scanner", "api-keys:gemini")], KEY)
        self.assertEqual(kr.items[("ocr-scanner", "api-keys:groq")], KEY2)
        text = self.file_text(path)
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY2, text)
        self.assertEqual(cfg.data["providers"]["gemini"]["keys"], KEY, "the app still reads keys from memory")
        self.assertEqual(cfg.secret_status, "")

    def test_next_start_reads_keys_back_from_the_store(self):
        kr = FakeKeyring()
        self.make(kr)
        fresh = core.Config(writer=False, secrets=SecretStore(backend=kr))
        self.assertEqual(fresh.data["providers"]["gemini"]["keys"], KEY)
        self.assertEqual(fresh.data["providers"]["groq"]["keys"], KEY2)

    def test_plaintext_is_kept_when_the_stored_value_cannot_be_read_back(self):
        cfg, path = self.make(LossyKeyring())
        self.assertEqual(cfg.migrated, [])
        self.assertIn(KEY, self.file_text(path), "never remove the only copy of a key")
        self.assertEqual(cfg.data["providers"]["gemini"]["keys"], KEY)
        self.assertIn("read back", cfg.secret_status)
        self.assertNotIn(KEY, cfg.secret_status)

    def test_plaintext_is_kept_when_storing_fails_and_error_hides_the_key(self):
        cfg, path = self.make(FailingKeyring())
        self.assertIn(KEY, self.file_text(path))
        self.assertNotIn(KEY, cfg.secret_status)
        self.assertNotIn(KEY2, cfg.secret_status)
        self.assertTrue(cfg.secret_status)

    def test_unavailable_store_degrades_to_the_old_behaviour(self):
        cfg, path = self.make(DownStore())
        self.assertIn(KEY, self.file_text(path))
        self.assertEqual(cfg.data["providers"]["gemini"]["keys"], KEY)
        self.assertIn("unavailable", cfg.secret_status)
        self.assertNotIn(KEY, cfg.secret_status)
        self.assertIsNone(cfg.save())
        self.assertIn(KEY, self.file_text(path), "saving without a store must not lose keys")
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_store_with_no_keys_is_not_a_warning(self):
        cfg, _ = self.make(DownStore(), keys={})
        self.assertEqual(cfg.secret_status, "")

    def test_daemon_side_config_never_writes_file_or_store(self):
        kr = FakeKeyring()
        path = write_config(self.base, {"gemini": KEY})
        before = path.read_text()
        cfg = core.Config(writer=False, secrets=SecretStore(backend=kr))
        self.assertEqual(path.read_text(), before)
        self.assertEqual(kr.sets, 0)
        self.assertEqual(cfg.data["providers"]["gemini"]["keys"], KEY, "daemon keeps working before the GUI migrates")

    def test_save_stores_new_keys_and_keeps_them_out_of_the_file(self):
        kr = FakeKeyring()
        cfg, path = self.make(kr, keys={})
        cfg.data["providers"]["gemini"]["keys"] = KEY
        self.assertIsNone(cfg.save())
        self.assertEqual(kr.items[("ocr-scanner", "api-keys:gemini")], KEY)
        self.assertNotIn(KEY, self.file_text(path))

    def test_unchanged_keys_are_not_rewritten_on_every_save(self):
        kr = FakeKeyring()
        cfg, _ = self.make(kr)
        sets = kr.sets
        for _ in range(3):
            cfg.save()
        self.assertEqual(kr.sets, sets)

    def test_clearing_a_key_deletes_it_from_the_store(self):
        kr = FakeKeyring()
        cfg, _ = self.make(kr)
        cfg.data["providers"]["groq"]["keys"] = ""
        cfg.save()
        self.assertNotIn(("ocr-scanner", "api-keys:groq"), kr.items)
        self.assertIn(("ocr-scanner", "api-keys:gemini"), kr.items)

    def test_failed_save_to_store_keeps_the_key_in_the_file(self):
        kr = FakeKeyring()
        cfg, path = self.make(kr, keys={})
        kr.set_password = mock.Mock(side_effect=RuntimeError("locked"))
        cfg.data["providers"]["gemini"]["keys"] = KEY
        self.assertIsNone(cfg.save())
        self.assertIn(KEY, self.file_text(path))
        self.assertTrue(cfg.secret_status)

    def test_store_going_away_later_does_not_wipe_anything(self):
        kr = FakeKeyring()
        self.make(kr)
        cfg = core.Config(writer=True, secrets=DownStore())
        self.assertEqual(cfg.data["providers"]["gemini"]["keys"], "")
        cfg.save()
        self.assertEqual(kr.items[("ocr-scanner", "api-keys:gemini")], KEY)

    def test_reload_picks_up_keys_changed_by_the_gui(self):
        kr = FakeKeyring()
        gui, _ = self.make(kr, keys={})
        daemon_cfg = core.Config(writer=False, secrets=SecretStore(backend=kr))
        gui.data["providers"]["gemini"]["keys"] = KEY
        gui.save()
        daemon_cfg.load()
        self.assertEqual(daemon_cfg.data["providers"]["gemini"]["keys"], KEY)

    def test_redact_hides_echoed_keys(self):
        self.assertEqual(core.redact(f"Incorrect API key provided: {KEY}.", KEY), "Incorrect API key provided: ***.")
        self.assertEqual(core.redact("nothing here", ""), "nothing here")

    def test_http_error_text_does_not_echo_the_key(self):
        resp = mock.Mock(status_code=401)
        resp.json.return_value = {"error": {"message": f"bad key {KEY}"}}
        with mock.patch.object(core.requests, "post", return_value=resp):
            with self.assertRaises(core.OCRError) as cm:
                core.call_provider(core.PROVIDER_BY_ID["gemini"], {"model": "m"}, KEY, b"x", "image/png", "p")
        self.assertNotIn(KEY, str(cm.exception))


# portal hotkeys

class FakeBus:
    """Scripted stand-in for JeepneyBus. `responses` maps a portal method to (code, results)."""
    instances = []

    def __init__(self, responses=None, props=None, fail_connect=None):
        if fail_connect:
            raise portal.BusError(fail_connect)
        self.unique_name = ":1.42"
        self.responses = responses or {}
        self.props = props
        self.calls, self.signals, self.subscribed = [], [], []
        self.closed = False
        self.lock = threading.Lock()
        FakeBus.instances.append(self)

    def call(self, path, iface, member, signature="", body=(), timeout=10.0, dest=portal.PORTAL_BUS):
        self.calls.append((path, iface, member, body))
        if member == "Get":
            if isinstance(self.props, portal.BusError):
                raise self.props
            return (("u", 1),)
        if member in ("Register", "Close"):
            return ()
        handle = f"/org/freedesktop/portal/desktop/request/1_42/{member}"
        if member in self.responses:
            code, results = self.responses[member]
            self.push((handle, portal.REQUEST_IFACE, "Response", (code, results)))
        return (handle,)

    def push(self, sig):
        with self.lock:
            self.signals.append(sig)

    def subscribe(self, iface, member):
        self.subscribed.append((iface, member))

    def next_signal(self, timeout):
        end = time.monotonic() + min(timeout, 0.05)
        while time.monotonic() < end:
            with self.lock:
                if self.signals:
                    return self.signals.pop(0)
            time.sleep(0.005)
        return None

    def close(self):
        self.closed = True


SESSION = "/org/freedesktop/portal/desktop/session/1_42/ocr1"
OK_RESPONSES = {
    "CreateSession": (0, {"session_handle": ("s", SESSION)}),
    "BindShortcuts": (0, {"shortcuts": ("a(sa{sv})", [
        ("region", {"trigger_description": ("s", "Ctrl+Alt+O")}),
        ("current", {"trigger_description": ("s", "Ctrl+Alt+S")}),
        ("toggle", {"trigger_description": ("s", "Ctrl+Alt+R")}),
        ("pause", {"trigger_description": ("s", "Ctrl+Alt+P")}),
    ])}),
}


def bus_factory(**kw):
    def factory():
        return FakeBus(**kw)
    return factory


class TriggerTests(unittest.TestCase):
    def test_conversion(self):
        self.assertEqual(portal.to_portal_trigger("Ctrl+Alt+O"), "CTRL+ALT+o")
        self.assertEqual(portal.to_portal_trigger("Ctrl+Shift+F5"), "CTRL+SHIFT+F5")
        self.assertEqual(portal.to_portal_trigger("Meta+Space"), "LOGO+space")
        self.assertIsNone(portal.to_portal_trigger("Ctrl+Alt+"))
        self.assertIsNone(portal.to_portal_trigger(""))


class PortalProbeTests(unittest.TestCase):
    def test_supported(self):
        ok, reason = portal.probe(bus_factory())
        self.assertTrue(ok, reason)

    def test_interface_missing_is_reported_as_unsupported(self):
        err = portal.BusError("x", "org.freedesktop.DBus.Error.UnknownProperty")
        ok, reason = portal.probe(bus_factory(props=err))
        self.assertFalse(ok)
        self.assertIn("GlobalShortcuts", reason)

    def test_portal_not_running(self):
        err = portal.BusError("x", "org.freedesktop.DBus.Error.ServiceUnknown")
        self.assertFalse(portal.probe(bus_factory(props=err))[0])

    def test_no_session_bus(self):
        ok, reason = portal.probe(bus_factory(fail_connect="cannot connect to the D-Bus session bus"))
        self.assertFalse(ok)
        self.assertIn("session bus", reason)

    def test_missing_jeepney_is_a_readable_reason(self):
        with mock.patch.dict(sys.modules, {"jeepney": None, "jeepney.io": None, "jeepney.io.blocking": None}):
            ok, reason = portal.probe()
        self.assertFalse(ok)
        self.assertIn("jeepney", reason)


@LINUX_ONLY
class WaylandHotkeyTests(unittest.TestCase):
    def setUp(self):
        FakeBus.instances = []
        self.cmds = []
        self.hk = core.default_config()["hotkeys"]
        for p in (mock.patch.object(core, "is_wayland", return_value=True),):
            p.start()
            self.addCleanup(p.stop)

    def mgr(self, **kw):
        m = core.HotkeyManager(self.cmds.append, portal_bus=bus_factory(**kw) if kw else bus_factory(responses=OK_RESPONSES))
        m.PORTAL_WAIT = 2.0
        self.addCleanup(m.stop)
        return m

    def desktop(self, name):
        p = mock.patch.object(core, "detect_desktop", return_value=name)
        p.start()
        self.addCleanup(p.stop)

    def test_hyprland_keeps_compositor_binds_and_never_touches_the_portal(self):
        self.desktop("hyprland")
        ok, msg, level = self.mgr().apply(self.hk)
        self.assertEqual((ok, level), (False, "info"))
        self.assertIn("Bind the keys in your compositor", msg)
        self.assertEqual(FakeBus.instances, [])

    def test_manual_mode_skips_the_portal_everywhere(self):
        self.desktop("gnome")
        ok, msg, level = self.mgr().apply(self.hk, "manual")
        self.assertFalse(ok)
        self.assertEqual(FakeBus.instances, [])

    def test_gnome_registers_through_the_portal_and_activation_runs_the_command(self):
        self.desktop("gnome")
        m = self.mgr(responses=OK_RESPONSES)
        ok, msg, level = m.apply(self.hk)
        self.assertTrue(ok, msg)
        self.assertEqual(level, "ok")
        self.assertTrue(m.running)
        bus = FakeBus.instances[-1]
        bind = [c for c in bus.calls if c[2] == "BindShortcuts"][0]
        self.assertEqual(bind[3][0], SESSION)
        ids = {e[0]: e[1]["preferred_trigger"][1] for e in bind[3][1]}
        self.assertEqual(ids["region"], "CTRL+ALT+o")
        self.assertEqual(set(ids), set(core.HOTKEY_ORDER))
        bus.push((SESSION, portal.SHORTCUTS_IFACE, "Activated", (SESSION, "current", 1, {})))
        bus.push((SESSION, portal.SHORTCUTS_IFACE, "Activated", ("/other/session", "region", 2, {})))
        bus.push((SESSION, portal.SHORTCUTS_IFACE, "Activated", (SESSION, "unknown-id", 3, {})))
        deadline = time.monotonic() + 2
        while not self.cmds and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        self.assertEqual(self.cmds, ["current"], "only our session's known shortcut may fire")
        m.stop()
        self.assertTrue(bus.closed)
        self.assertTrue(any(c[2] == "Close" for c in bus.calls), "the portal session must be closed")
        self.assertFalse(m.running)

    def test_kde_uses_the_same_portal_path(self):
        self.desktop("kde")
        ok, msg, _ = self.mgr(responses=OK_RESPONSES).apply(self.hk)
        self.assertTrue(ok, msg)

    def test_unsupported_portal_falls_back_to_manual_binds_with_the_reason(self):
        self.desktop("gnome")
        err = portal.BusError("x", "org.freedesktop.DBus.Error.UnknownProperty")
        m = self.mgr(props=err)
        ok, msg, level = m.apply(self.hk)
        self.assertEqual((ok, level), (False, "info"))
        self.assertIn("--trigger", msg)
        self.assertIn("GlobalShortcuts", msg)
        self.assertFalse(m.running)
        self.assertFalse(any(c[2] == "CreateSession" for b in FakeBus.instances for c in b.calls))

    def test_no_jeepney_falls_back_to_manual_binds(self):
        self.desktop("gnome")
        m = core.HotkeyManager(self.cmds.append)
        with mock.patch.dict(sys.modules, {"jeepney": None, "jeepney.io": None, "jeepney.io.blocking": None}):
            ok, msg, level = m.apply(self.hk)
        self.assertFalse(ok)
        self.assertIn("jeepney", msg)
        self.assertIn("--trigger", msg)

    def test_user_cancelling_the_dialog_falls_back_with_a_warning(self):
        self.desktop("kde")
        resp = dict(OK_RESPONSES, BindShortcuts=(1, {}))
        m = self.mgr(responses=resp)
        ok, msg, level = m.apply(self.hk)
        self.assertEqual((ok, level), (False, "warn"))
        self.assertIn("cancelled", msg)
        self.assertFalse(m.running)

    def test_session_refused(self):
        self.desktop("gnome")
        m = self.mgr(responses={"CreateSession": (2, {})})
        ok, msg, level = m.apply(self.hk)
        self.assertFalse(ok)
        self.assertEqual(level, "err")

    def test_pending_dialog_is_reported_then_resolved_later(self):
        self.desktop("kde")
        m = self.mgr(responses={"CreateSession": OK_RESPONSES["CreateSession"]})
        m.PORTAL_WAIT = 0.3
        ok, msg, level = m.apply(self.hk)
        self.assertEqual((ok, level), (True, "info"))
        self.assertIn("Waiting", msg)
        self.assertTrue(m.running)
        self.assertIsNone(m.portal_status())
        bus = FakeBus.instances[-1]
        handle = "/org/freedesktop/portal/desktop/request/1_42/BindShortcuts"
        bus.push((handle, portal.REQUEST_IFACE, "Response", (0, OK_RESPONSES["BindShortcuts"][1])))
        deadline = time.monotonic() + 3
        while m.portal_status() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        status = m.portal_status()
        self.assertIsNotNone(status)
        self.assertTrue(status[0])

    def test_keys_without_assigned_triggers_warn(self):
        self.desktop("gnome")
        resp = dict(OK_RESPONSES, BindShortcuts=(0, {"shortcuts": ("a(sa{sv})", [("region", {}), ("current", {})])}))
        ok, msg, level = self.mgr(responses=resp).apply(self.hk)
        self.assertTrue(ok)
        self.assertEqual(level, "warn")
        self.assertIn("assign", msg)

    def test_dead_portal_thread_is_reported_by_check_alive(self):
        self.desktop("gnome")
        m = self.mgr(responses=OK_RESPONSES)
        m.apply(self.hk)
        client = m._listener
        client.failure = "the portal went away"
        client._stop_evt.set()
        client.join(2)
        self.assertEqual(m.check_alive(), "Global hotkeys stopped working: the portal went away")

    def test_apply_twice_replaces_the_old_session(self):
        self.desktop("gnome")
        m = self.mgr(responses=OK_RESPONSES)
        m.apply(self.hk)
        first = FakeBus.instances[-1]
        m.apply(self.hk)
        self.assertTrue(first.closed)
        self.assertEqual(sum(1 for b in FakeBus.instances if not b.closed and b.calls), 1)

    def test_unsupported_hotkey_text_is_reported(self):
        self.desktop("gnome")
        ok, msg, level = self.mgr().apply(dict(self.hk, current="Ctrl+Alt+"))
        self.assertEqual(level, "err")
        self.assertIn("unsupported", msg)


class X11PathUnchangedTests(unittest.TestCase):
    def test_x11_still_uses_pynput_and_ignores_the_portal(self):
        class L:
            def __init__(self, mapping):
                self.mapping = mapping
                self.daemon = False
            def start(self): pass
            def stop(self): pass
            def join(self, t=None): pass
            def is_alive(self): return True
        backend = types.SimpleNamespace(GlobalHotKeys=L)
        m = core.HotkeyManager(lambda a: None, backend=backend, portal_bus=lambda: self.fail("portal used on X11"))
        with mock.patch.object(core, "is_wayland", return_value=False):
            ok, _msg, level = m.apply(core.default_config()["hotkeys"])
        self.assertEqual((ok, level), (True, "ok"))


# manual bind help

class ManualBindTests(unittest.TestCase):
    def setUp(self):
        self.hk = core.default_config()["hotkeys"]

    def test_hyprland_output_is_exactly_the_existing_snippet(self):
        for syntax in ("lua", "conf"):
            self.assertEqual(core.manual_bind_help(self.hk, "hyprland", syntax), core.hypr_snippet(self.hk, syntax))

    def test_every_desktop_lists_the_trigger_commands(self):
        for desktop in ("gnome", "kde", "sway", "i3", "xfce", "unknown"):
            text = core.manual_bind_help(self.hk, desktop)
            for action in core.HOTKEY_ORDER:
                self.assertIn(core.trigger_cmd(action), text, desktop)

    def test_desktop_specific_wording(self):
        self.assertIn("Custom Shortcuts", core.manual_bind_help(self.hk, "gnome"))
        self.assertIn("<Ctrl><Alt>o", core.manual_bind_help(self.hk, "gnome"))
        self.assertIn("System Settings", core.manual_bind_help(self.hk, "kde"))
        self.assertIn("Ctrl+ALT+O".replace("ALT", "Alt"), core.manual_bind_help(self.hk, "kde"))
        self.assertIn("bindsym Ctrl+Mod1+o exec", core.manual_bind_help(self.hk, "sway"))

    def test_unset_hotkeys_are_skipped_not_broken(self):
        text = core.manual_bind_help(dict(self.hk, current=""), "gnome")
        self.assertIn("(no key set)", text)


# autostart + daemon surface

class AutostartTests(unittest.TestCase):
    def test_units_install_into_gnome_kde_and_hyprland_targets(self):
        for text in (autostart.render_unit("/py", "/d.py"), autostart.render_gui_unit("/py", "/g.py")):
            self.assertIn("WantedBy=graphical-session.target hyprland-session.target", text)


class DaemonSurfaceTests(Isolated):
    def make_daemon(self, cfg):
        d = ocr_daemon.Daemon(cfg=cfg, capturer=mock.Mock(backend="fake"))
        self.addCleanup(d.shutdown)
        return d

    def test_status_reports_session_and_store_but_never_a_key(self):
        write_config(self.base, {"gemini": KEY})
        kr = FakeKeyring()
        gui = core.Config(writer=True, secrets=SecretStore(backend=kr))
        cfg = core.Config(writer=False, secrets=SecretStore(backend=kr))
        d = self.make_daemon(cfg)
        status = d.handle("status", {})
        events = d.handle("events", {"since": 0})
        d.failure("something failed")
        wire = json.dumps([status, events, d.handle("events", {"since": 0})])
        self.assertNotIn(KEY, wire)
        self.assertNotIn("providers", json.dumps(status))
        self.assertTrue(status["credential_store"]["secure"])
        self.assertIn("session_type", status["session"])
        text = ocr_daemon._status_text(dict(status, uptime=1))
        self.assertIn("system credential store", text)
        self.assertNotIn(KEY, text)
        del gui

    def test_status_text_when_store_is_down(self):
        write_config(self.base, {"gemini": KEY})
        cfg = core.Config(writer=False, secrets=DownStore())
        d = self.make_daemon(cfg)
        status = d.handle("status", {})
        self.assertFalse(status["credential_store"]["secure"])
        self.assertIn("not encrypted", ocr_daemon._status_text(dict(status, uptime=1)))
        self.assertNotIn(KEY, json.dumps(status))

    def test_log_file_never_contains_keys_after_migration(self):
        write_config(self.base, {"gemini": KEY})
        with self.assertLogs("ocr_scanner", level="INFO") as logs:
            core.Config(writer=True, secrets=SecretStore(backend=FakeKeyring()))
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertIn("gemini", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
