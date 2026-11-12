from __future__ import annotations

import logging
import os
import secrets
import socket
import struct
import threading
import time
from collections import deque
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from PIL import Image

from ocr_core import APP_ID, HYPR_KEYS, CaptureError, split_seq

log = logging.getLogger("ocr_scanner")


##############################################################################################################
# 1. compositor protocol probe
##############################################################################################################

# any one of these lets grim take screenshots
SCREENCOPY_PROTOCOLS = ("zwlr_screencopy_manager_v1", "ext_image_copy_capture_manager_v1")
# slurp draws its selection with a layer-shell surface
LAYER_SHELL = "zwlr_layer_shell_v1"

_DISPLAY_ID, _REGISTRY_ID, _CALLBACK_ID = 1, 2, 3
_GET_REGISTRY, _SYNC = 1, 0          # wl_display requests
_GLOBAL, _DONE, _ERROR = 0, 0, 0     # wl_registry.global, wl_callback.done, wl_display.error events


def _socket_path(env) -> Optional[str]:
    name = env.get("WAYLAND_DISPLAY") or ""
    if not name:
        return None
    if os.path.isabs(name):
        return name
    runtime = env.get("XDG_RUNTIME_DIR")
    return os.path.join(runtime, name) if runtime else None


def _request(obj: int, opcode: int, payload: bytes) -> bytes:
    return struct.pack("<II", obj, ((8 + len(payload)) << 16) | opcode) + payload


def _read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("compositor closed the connection")
        buf += chunk
    return buf


def list_globals(env=None, timeout: float = 2.0, sock=None) -> Optional[set]:
    """Interface names the compositor advertises, or None if they could not be read."""
    env = os.environ if env is None else env
    own = sock is None
    try:
        if own:
            path = _socket_path(env)
            if not path:
                return None
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(path)
        sock.sendall(_request(_DISPLAY_ID, _GET_REGISTRY, struct.pack("<I", _REGISTRY_ID))
                     + _request(_DISPLAY_ID, _SYNC, struct.pack("<I", _CALLBACK_ID)))
        found = set()
        while True:
            obj, word = struct.unpack("<II", _read_exact(sock, 8))
            size, opcode = word >> 16, word & 0xFFFF
            if size < 8:
                return None
            body = _read_exact(sock, size - 8)
            if obj == _REGISTRY_ID and opcode == _GLOBAL and len(body) >= 8:
                (length,) = struct.unpack_from("<I", body, 4)
                if length < 1 or 8 + length > len(body):
                    return None
                found.add(body[8:8 + length - 1].decode("utf-8", "replace"))
            elif obj == _CALLBACK_ID and opcode == _DONE:
                return found
            elif obj == _DISPLAY_ID and opcode == _ERROR:
                return None
    except (OSError, struct.error):
        return None
    finally:
        if own and sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def capabilities(env=None, globals_: Optional[set] = None) -> Optional[dict]:
    """{'grim': bool, 'slurp': bool, 'globals': set} or None when the compositor could not be asked."""
    names = list_globals(env) if globals_ is None else globals_
    if names is None:
        return None
    return {"grim": any(p in names for p in SCREENCOPY_PROTOCOLS),
            "slurp": LAYER_SHELL in names,
            "globals": names}


##############################################################################################################
# 2. desktop portal: D-Bus transport and GlobalShortcuts
##############################################################################################################

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
REGISTRY_PATH = "/org/freedesktop/host/portal/Registry"
REGISTRY_IFACE = "org.freedesktop.host.portal.Registry"

# D-Bus errors that mean "this desktop has no GlobalShortcuts portal" (as opposed to a real failure)
_UNSUPPORTED_ERRORS = (
    "org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner",
    "org.freedesktop.DBus.Error.UnknownObject", "org.freedesktop.DBus.Error.UnknownInterface",
    "org.freedesktop.DBus.Error.UnknownProperty", "org.freedesktop.DBus.Error.InvalidArgs",
)
NEEDS_PORTAL_HINT = ("It needs a recent xdg-desktop-portal plus a desktop backend that implements "
                     "GlobalShortcuts (xdg-desktop-portal-gnome, -kde, -hyprland, ...).")


class BusError(Exception):
    """A D-Bus level failure. `name` is the D-Bus error name when there is one."""

    def __init__(self, message: str, name: str = ""):
        super().__init__(message)
        self.name = name


# trigger text

def to_portal_trigger(seq: str) -> Optional[str]:
    """'Ctrl+Alt+O' -> 'CTRL+ALT+o' (XDG shortcuts syntax: modifiers CTRL/ALT/SHIFT/LOGO + an xkb key name)."""
    sp = split_seq(seq)
    if not sp:
        return None
    mods, key = sp
    mm = {"Ctrl": "CTRL", "Alt": "ALT", "Shift": "SHIFT", "Meta": "LOGO"}
    if len(key) == 1 and key.isalpha():
        k = key.lower()
    else:
        k = HYPR_KEYS.get(key, key)   # HYPR_KEYS holds xkb key names (space, Return, Escape, ...)
    return "+".join([mm[m] for m in mods] + [k])


# bus transport

class JeepneyBus:
    """Blocking session-bus connection. Imports jeepney lazily so the rest of the app never needs it."""

    def __init__(self):
        try:
            from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, message_bus, new_method_call
            from jeepney.io.blocking import Proxy, open_dbus_connection
        except ImportError:
            raise BusError("the 'jeepney' package is not installed (pip install jeepney)") from None
        self._j = {"DBusAddress": DBusAddress, "HeaderFields": HeaderFields, "MatchRule": MatchRule,
                   "MessageType": MessageType, "new_method_call": new_method_call, "Proxy": Proxy,
                   "message_bus": message_bus}
        try:
            self._conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:  # no session bus, bad address, auth failure
            raise BusError(f"cannot connect to the D-Bus session bus ({exc.__class__.__name__})") from None
        self.unique_name = self._conn.unique_name
        self._queue = deque(maxlen=64)
        self._handles = []

    def call(self, path, iface, member, signature="", body=(), timeout=10.0, dest=PORTAL_BUS):
        j = self._j
        msg = j["new_method_call"](j["DBusAddress"](path, bus_name=dest, interface=iface), member, signature, body)
        try:
            reply = self._conn.send_and_get_reply(msg, timeout=timeout)
        except TimeoutError:
            raise BusError(f"{member} timed out") from None
        if reply.header.message_type == j["MessageType"].error:
            name = reply.header.fields.get(j["HeaderFields"].error_name, "")
            detail = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
            raise BusError(f"{name}: {detail}".strip(": "), name)
        return reply.body

    def subscribe(self, iface, member):
        j = self._j
        rule = j["MatchRule"](type="signal", interface=iface, member=member)
        j["Proxy"](j["message_bus"], self._conn).AddMatch(rule)
        handle = self._conn.filter(rule, queue=self._queue)
        handle.__enter__()
        self._handles.append(handle)

    def next_signal(self, timeout):
        """(path, iface, member, body) of the next subscribed signal, or None on timeout."""
        try:
            msg = self._conn.recv_until_filtered(self._queue, timeout=timeout)
        except TimeoutError:
            return None
        h, f = msg.header, self._j["HeaderFields"]
        return (h.fields.get(f.path, ""), h.fields.get(f.interface, ""), h.fields.get(f.member, ""), msg.body)

    def close(self):
        for handle in self._handles:
            try:
                handle.__exit__(None, None, None)
            except Exception:  # cleanup only
                pass
        try:
            self._conn.close()
        except Exception:  # cleanup only
            pass


# support check

def probe(bus_factory: Callable = JeepneyBus):
    """Ask the running portal whether it offers GlobalShortcuts. Returns (supported, reason)."""
    try:
        bus = bus_factory()
    except BusError as exc:
        return False, str(exc)
    try:
        body = bus.call(PORTAL_PATH, PROPS_IFACE, "Get", "ss", (SHORTCUTS_IFACE, "version"), timeout=5.0)
        version = body[0][1] if body and isinstance(body[0], tuple) else body[0] if body else 0
        return True, f"GlobalShortcuts portal version {version}"
    except BusError as exc:
        if exc.name in _UNSUPPORTED_ERRORS:
            return False, f"this desktop's portal does not offer GlobalShortcuts. {NEEDS_PORTAL_HINT}"
        return False, f"the desktop portal did not answer ({exc})"
    finally:
        bus.close()


# the session

class PortalShortcuts(threading.Thread):
    """Registers `shortcuts` ({id: (description, preferred_trigger)}) and calls on_activated(id) on key press.

    Reports through `status` = (state, message, level), state in pending / ok / err. `failure` holds the
    reason when the thread ended without being asked to. `ready` is set once binding finished either way."""

    CREATE_TIMEOUT = 15.0

    def __init__(self, shortcuts: dict, on_activated: Callable, bus_factory: Callable = JeepneyBus,
                 app_id: str = APP_ID):
        super().__init__(daemon=True, name="portal-shortcuts")
        self.shortcuts, self.on_activated = dict(shortcuts), on_activated
        self._bus_factory, self.app_id = bus_factory, app_id
        self._stop_evt = threading.Event()
        self.ready = threading.Event()
        self.failure: Optional[str] = None
        self.status = ("pending", "Waiting for the desktop to confirm the global shortcuts...", "info")
        self._responses: dict = {}
        self._session: Optional[str] = None
        self._bus = None

    def stop(self):
        self._stop_evt.set()

    def _fail(self, message: str, level: str = "err"):
        self.failure = message
        self.status = ("err", message, level)
        self.ready.set()

    def run(self):
        try:
            self._bus = self._bus_factory()
        except BusError as exc:
            self._fail(f"Portal shortcuts unavailable: {exc}")
            return
        try:
            self._bus.subscribe(REQUEST_IFACE, "Response")
            self._bus.subscribe(SHORTCUTS_IFACE, "Activated")
            self._register_app()
            self._session = self._create_session()
            if self._session and self._bind():
                self._listen()
        except BusError as exc:
            self._fail(f"Portal shortcuts failed: {exc}")
        except Exception as exc:  # thread boundary: record it, never kill the daemon
            log.exception("Portal shortcuts crashed")
            self._fail(f"Portal shortcuts crashed: {exc.__class__.__name__}")
        finally:
            self._close_session()
            self.ready.set()

    # protocol steps

    def _register_app(self):
        """Newer portals want host (non-Flatpak) apps to announce an app id first. Older ones lack the call."""
        try:
            self._bus.call(REGISTRY_PATH, REGISTRY_IFACE, "Register", "sa{sv}", (self.app_id, {}), timeout=5.0)
        except BusError as exc:
            log.info("Portal app registration skipped: %s", exc)

    def _token(self) -> str:
        return "ocr" + secrets.token_hex(6)

    def _request(self, member: str, signature: str, body: tuple, timeout: Optional[float]):
        """Call a portal method that answers through a Request object. Returns (code, results) or None if stopped/timed out."""
        reply = self._bus.call(PORTAL_PATH, SHORTCUTS_IFACE, member, signature, body, timeout=10.0)
        handle = reply[0] if reply else ""
        waited = 0.0
        while handle not in self._responses:
            if self._stop_evt.is_set():
                return None
            if timeout is not None and waited >= timeout:
                return None
            sig = self._bus.next_signal(0.5)
            waited += 0.5 if sig is None else 0.0
            if sig is not None:
                self._dispatch(sig)
        return self._responses.pop(handle)

    def _dispatch(self, sig):
        path, iface, member, body = sig
        if iface == REQUEST_IFACE and member == "Response" and len(body) >= 2:
            self._responses[path] = (body[0], body[1] if isinstance(body[1], dict) else {})
        elif iface == SHORTCUTS_IFACE and member == "Activated" and len(body) >= 2:
            session, shortcut_id = body[0], body[1]
            if session == self._session and shortcut_id in self.shortcuts and not self._stop_evt.is_set():
                try:
                    self.on_activated(shortcut_id)
                except Exception:  # a callback must never end the listener
                    log.exception("Shortcut callback failed")

    def _create_session(self) -> Optional[str]:
        options = {"handle_token": ("s", self._token()), "session_handle_token": ("s", self._token())}
        result = self._request("CreateSession", "a{sv}", (options,), self.CREATE_TIMEOUT)
        if result is None:
            if not self._stop_evt.is_set():
                self._fail("The desktop portal did not answer CreateSession.")
            return None
        code, results = result
        handle = results.get("session_handle")
        handle = handle[1] if isinstance(handle, tuple) else handle
        if code != 0 or not isinstance(handle, str):
            self._fail(f"The desktop portal refused to create a shortcut session (response {code}).")
            return None
        return handle

    def _bind(self) -> bool:
        entries = [(sid, {"description": ("s", desc), "preferred_trigger": ("s", trig)})
                   for sid, (desc, trig) in self.shortcuts.items()]
        body = (self._session, entries, "", {"handle_token": ("s", self._token())})
        result = self._request("BindShortcuts", "oa(sa{sv})sa{sv}", body, None)  # a dialog may wait for the person
        if result is None:
            return False
        code, results = result
        if code == 1:
            self._fail("Shortcut registration was cancelled in the desktop's dialog.", level="warn")
            return False
        if code != 0:
            self._fail(f"The desktop portal could not bind the shortcuts (response {code}).")
            return False
        bound = results.get("shortcuts")
        bound = bound[1] if isinstance(bound, tuple) else bound
        triggers, unassigned = [], []
        for item in bound or []:
            sid, props = item[0], item[1] if len(item) > 1 else {}
            desc = props.get("trigger_description") if isinstance(props, dict) else None
            desc = desc[1] if isinstance(desc, tuple) else desc
            (triggers if desc else unassigned).append(f"{self.shortcuts.get(sid, ('', ''))[0] or sid}: {desc}" if desc else sid)
        if triggers and not unassigned:
            self.status = ("ok", "Global shortcuts registered through the desktop portal. " + "; ".join(triggers) + ".", "ok")
        elif triggers:
            self.status = ("ok", "Global shortcuts registered through the desktop portal, but some have no key yet "
                           "(assign them in your desktop's shortcut settings under 'OCR Scanner'). "
                           + "; ".join(triggers) + ".", "warn")
        else:
            self.status = ("ok", "The desktop portal accepted the shortcuts but assigned no keys. Assign them in your "
                           "desktop's keyboard shortcut settings under 'OCR Scanner', or use the manual binds below.", "warn")
        self.ready.set()
        return True

    def _listen(self):
        while not self._stop_evt.is_set():
            sig = self._bus.next_signal(0.5)
            if sig is not None:
                self._dispatch(sig)

    def _close_session(self):
        bus, session = self._bus, self._session
        if bus is None:
            return
        if session:
            try:
                bus.call(session, SESSION_IFACE, "Close", "", (), timeout=2.0)
            except BusError:
                pass  # the session ends with the connection anyway
        bus.close()


##############################################################################################################
# 3. portal screenshots
##############################################################################################################

SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
RESPONSE_TIMEOUT = 60.0  # the first call may wait for the person to answer a permission dialog
_portal_permission_granted = False


def _unwrap(value):
    """jeepney returns a{sv} values as (signature, value)."""
    return value[1] if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str) else value


def _path_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme not in ("file", ""):
        raise CaptureError(f"The desktop returned a screenshot at an unsupported address ({parsed.scheme}:).")
    return unquote(parsed.path if parsed.scheme else uri)


def take_screenshot(bus_factory: Callable = JeepneyBus, timeout: float = RESPONSE_TIMEOUT,
                    interactive: bool | None = None, app_id: str = APP_ID) -> Image.Image:
    """One screenshot of the whole desktop as an RGB PIL image. Raises CaptureError with a readable reason."""
    global _portal_permission_granted
    try:
        bus = bus_factory()
    except BusError as exc:
        raise CaptureError(f"Screenshot portal unavailable: {exc}") from None
    path = None
    try:
        bus.subscribe(REQUEST_IFACE, "Response")
        try:  # newer portals want host (non-Flatpak) apps to announce an app id first; older ones lack this
            bus.call(REGISTRY_PATH, REGISTRY_IFACE, "Register", "sa{sv}", (app_id, {}), timeout=5.0)
        except BusError as exc:
            log.info("Portal app registration skipped: %s", exc)
        ask_permission = (not _portal_permission_granted) if interactive is None else bool(interactive)
        options = {"handle_token": ("s", "ocr" + secrets.token_hex(6)), "interactive": ("b", ask_permission)}
        try:
            reply = bus.call(PORTAL_PATH, SCREENSHOT_IFACE, "Screenshot", "sa{sv}", ("", options), timeout=10.0)
        except BusError as exc:
            raise CaptureError(_explain(exc)) from None
        handle = reply[0] if reply else ""
        deadline = time.monotonic() + timeout
        response = None
        while response is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureError("The desktop did not answer the screenshot request in time "
                                   "(is a permission dialog waiting for you?).")
            sig = bus.next_signal(min(0.5, remaining))
            if sig is None:
                continue
            sig_path, iface, member, body = sig
            if iface == REQUEST_IFACE and member == "Response" and sig_path == handle and len(body) >= 2:
                response = (body[0], body[1] if isinstance(body[1], dict) else {})
        code, results = response
        if code == 1:
            raise CaptureError("The screenshot was cancelled in the desktop's dialog.")
        if code != 0:
            raise CaptureError("The desktop refused the screenshot (response %s). Allow this app to take "
                               "screenshots in your desktop's privacy / app permission settings." % code)
        uri = _unwrap(results.get("uri"))
        if not isinstance(uri, str) or not uri:
            raise CaptureError("The desktop portal answered without a screenshot file.")
        _portal_permission_granted = True
        path = _path_from_uri(uri)
        try:
            img = Image.open(path)
            img.load()
        except (OSError, ValueError) as exc:
            raise CaptureError(f"Could not read the portal's screenshot file: {exc}") from None
        return img.convert("RGB")
    finally:
        bus.close()
        if path:  # the portal wrote this file for us; recording would otherwise fill the disk
            try:
                os.unlink(path)
            except OSError:
                pass


def _explain(exc: BusError) -> str:
    if exc.name in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner",
                    "org.freedesktop.DBus.Error.UnknownMethod", "org.freedesktop.DBus.Error.UnknownInterface"):
        return ("This desktop has no screenshot portal. Install xdg-desktop-portal plus the backend for your "
                "desktop (xdg-desktop-portal-gnome or xdg-desktop-portal-kde).")
    return f"The screenshot portal failed: {exc}"
