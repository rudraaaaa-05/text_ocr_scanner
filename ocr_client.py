from __future__ import annotations

import threading

from ocr_core import log
from ocr_ipc import request

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
