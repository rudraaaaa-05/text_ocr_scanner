from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from ocr_core import *  # noqa: F401,F403  (Qt-free core: config, capture, OCR providers, constants)
from ocr_core import _merge, _is_int  # noqa: F401
from ocr_ipc import InstanceLock, cli_trigger, request, send_command, start_daemon  # noqa: F401
from ocr_client import DaemonLink

if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--trigger":
    sys.exit(cli_trigger(sys.argv[1:]))

#third party imports

if is_wayland() and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"

try:
    from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import (
        QAction, QColor, QDesktopServices, QGuiApplication, QIcon, QImage,
        QKeySequence, QPainter, QPen, QPixmap,
    )
    from PySide6.QtWidgets import (
        QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
        QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QMainWindow,
        QMenu, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox,
        QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
    )
except ImportError as _exc:  # pragma: no cover
    sys.stderr.write(
        f"Missing dependency: {getattr(_exc, 'name', None) or _exc}\n"
        "Install everything with:  pip install -r requirements.txt   (or just use run.sh / run.bat)\n"
    )
    sys.exit(1)


def match_screens(screens, monitors) -> dict:
    """Pair Qt screens with mss monitors.

    screens: [(x, y, width, height, device_pixel_ratio)] in Qt coordinates; monitors: mss monitor dicts
    in physical pixels. Returns {screen_index: monitor_index} for confident matches only. Qt reports a
    screen's size in device independent pixels, and (depending on version) its position either native or
    scaled, so both readings are tried. Negative coordinates are fine. Best global score wins first, so
    identical monitors are told apart by position.
    """
    cands = []
    for si, (x, y, w, h, dpr) in enumerate(screens):
        for mi, m in enumerate(monitors):
            size = min(abs(m["width"] - w * dpr) + abs(m["height"] - h * dpr),
                       abs(m["width"] - w) + abs(m["height"] - h))
            pos = min(abs(m["left"] - x * dpr) + abs(m["top"] - y * dpr),
                      abs(m["left"] - x) + abs(m["top"] - y))
            if size <= max(8.0, 0.01 * (m["width"] + m["height"])):
                cands.append((size * 10 + pos, si, mi))
    cands.sort()
    out, used = {}, set()
    for _score, si, mi in cands:
        if si in out or mi in used:
            continue
        out[si] = mi
        used.add(mi)
    return out


def map_selection(mon: dict, sel, widget_size):
    """Map a rectangle dragged on a screen overlay to screenshot coordinates.

    sel = (x, y, w, h) and widget_size = (w, h), both in Qt logical pixels; mon is the physical mss
    monitor. The ratio monitor / widget size absorbs Windows display scaling and any device pixel
    ratio. Edges are rounded, not sizes, so the crop and the stored region always agree. Returns
    (region dict, crop box in image pixels). Raises ValueError if nothing is left after clamping.
    """
    ww, wh = widget_size
    if ww <= 0 or wh <= 0:
        raise ValueError("overlay has no size")
    mw, mh = int(mon["width"]), int(mon["height"])
    fx, fy = mw / ww, mh / wh
    x, y, w, h = sel
    x0, x1 = max(0, round(x * fx)), min(mw, round((x + w) * fx))
    y0, y1 = max(0, round(y * fy)), min(mh, round((y + h) * fy))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ValueError("empty selection")
    region = {"type": "mss", "left": int(mon["left"]) + x0, "top": int(mon["top"]) + y0,
              "width": x1 - x0, "height": y1 - y0}
    return region, (x0, y0, x1, y1)


class RegionOverlay(QWidget):
    """Frozen-screenshot overlay for dragging out a region (X11, Windows, macOS)."""
    picked = Signal(int, QRect, QSize)
    cancelled = Signal()

    def __init__(self, shot_index: int, screen, pil_img):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.shot_index = shot_index
        self.target_screen = screen
        self.dismissed = False  # set by the manager before it closes us on purpose
        self.setScreen(screen)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        rgb = pil_img.convert("RGB")
        w, h = rgb.size
        buf = rgb.tobytes("raw", "RGB")
        self.pix = QPixmap.fromImage(QImage(buf, w, h, w * 3, QImage.Format.Format_RGB888))
        self.start = None
        self.cur = None

    def launch(self):
        self.setGeometry(self.target_screen.geometry())
        if IS_MAC:
            self.show()
        else:
            self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def sel_rect(self):
        if self.start is None or self.cur is None:
            return None
        return QRect(self.start, self.cur).normalized().intersected(self.rect())

    def paintEvent(self, _event):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pix)
        p.fillRect(self.rect(), QColor(0, 0, 0, 130))
        sel = self.sel_rect()
        if sel is not None and sel.width() > 0 and sel.height() > 0:
            sx = self.pix.width() / max(1, self.width())
            sy = self.pix.height() / max(1, self.height())
            p.drawPixmap(QRectF(sel), self.pix, QRectF(sel.x() * sx, sel.y() * sy, sel.width() * sx, sel.height() * sy))
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawRect(sel)
            label = f"{sel.width()} \u00d7 {sel.height()}"
            p.setPen(QColor("#ffffff"))
            p.fillRect(QRect(sel.x(), max(0, sel.y() - 24), 92, 22), QColor(0, 0, 0, 190))
            p.drawText(QRect(sel.x() + 6, max(0, sel.y() - 24), 86, 22), Qt.AlignmentFlag.AlignVCenter, label)
        else:
            hint = "Drag to select a region  \u00b7  Esc or right-click to cancel"
            box = QRect(self.width() // 2 - 200, 28, 400, 34)
            p.fillRect(box, QColor(0, 0, 0, 200))
            p.setPen(QColor("#ececee"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
        elif e.button() == Qt.MouseButton.LeftButton:
            self.start = e.position().toPoint()
            self.cur = self.start
            self.update()

    def mouseMoveEvent(self, e):
        if self.start is not None:
            self.cur = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.start is None:
            return
        self.cur = e.position().toPoint()
        r = self.sel_rect()
        self.start = None
        if r is None or r.width() < MIN_SELECTION_PX or r.height() < MIN_SELECTION_PX:
            self.cur = None  # too small: ignore it and let the user drag again
            self.update()
            return
        self.picked.emit(self.shot_index, r, self.size())

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()

    def closeEvent(self, event):
        if not self.dismissed:  # closed by the window manager (Alt+F4 ...): that is a cancel
            self.dismissed = True
            self.cancelled.emit()
        event.accept()


class OverlayManager(QObject):
    finished = Signal(object, str)  # (region dict, PIL image) or None, and an error text ("" if none)

    def __init__(self, shots: list, screens: list):
        super().__init__()
        self.shots = shots
        self.layout = monitor_layout([m for m, _ in shots])
        self.overlays = []
        self.unmatched = []
        self._done = False
        geoms = []
        for qs in screens:
            g = qs.geometry()
            geoms.append((g.x(), g.y(), g.width(), g.height(), qs.devicePixelRatio()))
        mapping = match_screens(geoms, [m for m, _ in shots])
        self.unmatched = [i for i in range(len(screens)) if i not in mapping]
        for si, mi in mapping.items():
            ov = RegionOverlay(mi, screens[si], shots[mi][1])
            ov.picked.connect(self._picked)
            ov.cancelled.connect(lambda: self._finish(None, ""))
            self.overlays.append(ov)

    def start(self):
        if not self.overlays:
            self._finish(None, "None of the screens could be matched to a capture monitor.")
            return
        for ov in self.overlays:
            ov.launch()

    def _picked(self, si: int, rect: QRect, wsize: QSize):
        mon, img = self.shots[si]
        try:
            region, crop_box = map_selection(mon, (rect.x(), rect.y(), rect.width(), rect.height()),
                                             (wsize.width(), wsize.height()))
        except ValueError as exc:
            self._finish(None, f"Invalid selection ({exc}).")
            return
        region["layout"] = self.layout  # lets us detect a changed monitor setup later
        self._finish((region, img.crop(crop_box)), "")

    def _close_overlays(self):
        for ov in self.overlays:
            ov.dismissed = True
            ov.close()
            ov.deleteLater()
        self.overlays = []

    def cancel(self):
        """Synchronous teardown for application shutdown. No signal is emitted."""
        self._done = True
        self._close_overlays()

    def _finish(self, payload, err: str):
        if self._done:
            return
        self._done = True

        def close_all():
            self._close_overlays()
            self.finished.emit(payload, err)

        QTimer.singleShot(0, close_all)



class Bus(QObject):
    log = Signal(str, str)                # level, message
    command = Signal(str)                 # "show"
    update = Signal(object, bool)         # daemon events reply, is-replay
    connection = Signal(bool)             # daemon link up / down
    reply = Signal(str, object, object)   # command, reply dict or None, on_ok callback
    select_done = Signal(object)          # (region, image, error)
    shots_ready = Signal(object)          # screenshots for the overlay selector
    models_ready = Signal(str, object, str)
    test_done = Signal(str, bool, str)
    notify = Signal(str, str)
    failure = Signal(str)                 # an error the user must see (log + notification)

    def __init__(self):
        super().__init__()
        self._closed = threading.Event()

    def close(self):
        self._closed.set()

    def post(self, name: str, *args) -> bool:
        """Emit a signal from any thread. After close() (shutdown) emits are dropped instead of touching
        Qt objects that may already be destroyed."""
        if self._closed.is_set():
            return False
        try:
            getattr(self, name).emit(*args)
        except RuntimeError:
            if self._closed.is_set():
                return False
            raise
        return True


# css bullshit 
# this is vibecoded because i couldnt be bothered to style eveything, just tell it make it black and greay
# that'll d the job
QSS = """
QWidget { background: transparent; color: #e6e6e8; font-size: 13px; }
QMainWindow, QWidget#root { background: #0e0e0f; }
QDialog, QFileDialog { background: #121214; }
QToolTip { background: #1d1d21; color: #e6e6e8; border: 1px solid #34343a; padding: 4px 6px; }
QLabel#h1 { font-size: 24px; font-weight: 700; color: #f4f4f5; }
QLabel#muted { color: #85858d; }
QLabel#cardTitle { font-size: 15px; font-weight: 600; color: #f1f1f3; }
QLabel#brand { font-size: 17px; font-weight: 700; color: #f4f4f5; }
QLabel#badge { background: #26262b; color: #c9c9ce; border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 600; }
QFrame#sidebar { background: #121214; border-right: 1px solid #222226; }
QFrame#card { background: #161619; border: 1px solid #25252a; border-radius: 12px; }
QPushButton { background: #222226; border: 1px solid #303036; border-radius: 8px; padding: 8px 16px; color: #e6e6e8; }
QPushButton:hover { background: #2b2b31; border-color: #3c3c44; }
QPushButton:pressed { background: #1b1b1f; }
QPushButton:disabled { background: #151518; color: #5c5c63; border-color: #222226; }
QPushButton#primary { background: #ececee; color: #0e0e0f; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #ffffff; }
QPushButton#primary:disabled { background: #2c2c31; color: #6b6b72; }
QPushButton#danger { background: #2a1719; border: 1px solid #4a2428; color: #ff8a8f; }
QPushButton#danger:hover { background: #351c1f; }
QPushButton#danger:disabled { background: #151518; color: #5c5c63; border-color: #222226; }
QPushButton#nav { text-align: left; padding: 10px 16px; border: none; border-radius: 8px; background: transparent; color: #9a9aa2; font-size: 14px; }
QPushButton#nav:hover { background: #1b1b1f; color: #e6e6e8; }
QPushButton#nav:checked { background: #26262b; color: #ffffff; font-weight: 600; }
QPushButton#seg { background: #101012; border: 1px solid #2e2e33; padding: 8px 20px; }
QPushButton#seg:hover { border-color: #46464e; }
QPushButton#seg:checked { background: #ececee; color: #0e0e0f; border-color: #ececee; font-weight: 600; }
QPushButton#seg:disabled { color: #5c5c63; }
QPushButton#seg:checked:disabled { background: #3a3a3f; color: #85858d; border-color: #3a3a3f; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #101012; border: 1px solid #2e2e33; border-radius: 8px; padding: 7px 10px; selection-background-color: #4a4a54; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #8a8a94; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #5c5c63; }
QPlainTextEdit { font-family: "JetBrains Mono", "DejaVu Sans Mono", "Menlo", "Consolas", monospace; font-size: 12px; }
QComboBox { padding-right: 26px; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: url("@ARROW@"); width: 10px; height: 6px; }
QComboBox QAbstractItemView { background: #17171a; border: 1px solid #34343a; selection-background-color: #2e2e34; selection-color: #ffffff; outline: 0; padding: 4px; }
QSpinBox::up-button, QSpinBox::down-button { width: 0; border: none; }
QCheckBox, QRadioButton { spacing: 10px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid #4a4a52; background: #101012; }
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 9px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #8a8a94; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #ececee; border-color: #ececee; }
QCheckBox::indicator:checked { image: url("@CHECK@"); }
QRadioButton#providerName { font-size: 15px; font-weight: 600; color: #f1f1f3; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #34343a; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #46464e; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { height: 0; }
QMenu { background: #17171a; border: 1px solid #2a2a2f; padding: 6px; }
QMenu::item { padding: 7px 20px; border-radius: 6px; }
QMenu::item:selected { background: #2a2a30; }
"""


def make_assets():
    d = config_dir() / "assets"
    d.mkdir(parents=True, exist_ok=True)
    check, arrow = d / "check.png", d / "arrow.png"
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#0e0e0f"), 2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPoint(4, 8), QPoint(7, 11), QPoint(12, 5)])
    p.end()
    pm.save(str(check))
    pm = QPixmap(12, 8)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#9a9aa2"))
    p.drawPolygon([QPoint(1, 1), QPoint(11, 1), QPoint(6, 7)])
    p.end()
    pm.save(str(arrow))
    return check.as_posix(), arrow.as_posix()


def make_app_icon() -> QIcon:
    pm = QPixmap(128, 128)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#18181b"))
    p.setPen(QPen(QColor("#3a3a40"), 4))
    p.drawRoundedRect(6, 6, 116, 116, 26, 26)
    pen = QPen(QColor("#ececee"), 9)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline([QPoint(30, 52), QPoint(30, 30), QPoint(52, 30)])
    p.drawPolyline([QPoint(76, 30), QPoint(98, 30), QPoint(98, 52)])
    p.drawPolyline([QPoint(98, 76), QPoint(98, 98), QPoint(76, 98)])
    p.drawPolyline([QPoint(52, 98), QPoint(30, 98), QPoint(30, 76)])
    p.drawLine(46, 58, 82, 58)
    p.drawLine(46, 74, 70, 74)
    p.end()
    return QIcon(pm)


def make_card(title: str, subtitle: str = ""):
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 18, 20, 18)
    lay.setSpacing(12)
    t = QLabel(title)
    t.setObjectName("cardTitle")
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("muted")
        s.setWordWrap(True)
        lay.addWidget(s)
    return frame, lay


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


def page_header(title: str, sub: str):
    h = QLabel(title)
    h.setObjectName("h1")
    return h, muted(sub)


def scroll_wrap(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(inner)
    return sa


def h_row(*items, stretch_end: bool = False) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(10)
    for it in items:
        if isinstance(it, int):
            lay.addSpacing(it)
        else:
            lay.addWidget(it)
    if stretch_end:
        lay.addStretch(1)
    return lay


class ProviderCard(QFrame):
    def __init__(self, win, pdef: dict):
        super().__init__()
        self.setObjectName("card")
        self.win, self.pdef = win, pdef
        pc = win.d["providers"][pdef["id"]]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.active = QRadioButton(pdef["name"])
        self.active.setObjectName("providerName")
        head.addWidget(self.active)
        if pdef["free"]:
            badge = QLabel(pdef["free"].upper())
            badge.setObjectName("badge")
            head.addWidget(badge)
        head.addStretch(1)
        self.fallback = QCheckBox("Use as fallback")
        self.fallback.setToolTip("If the active provider fails or is rate limited, try this one next.")
        head.addWidget(self.fallback)
        lay.addLayout(head)
        lay.addWidget(muted(pdef["note"]))
        if pdef["kind"] == "local":
            self._build_local(lay, pc)
            return

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        r = 0
        self.base = None
        if pdef["edit_base"]:
            grid.addWidget(QLabel("Base URL"), r, 0)
            self.base = QLineEdit(pc["base_url"])
            self.base.setPlaceholderText("https://host/v1")
            grid.addWidget(self.base, r, 1, 1, 2)
            r += 1
        grid.addWidget(QLabel("API key" if pdef["needs_key"] else "API key (optional)"), r, 0)
        self.key = QLineEdit(pc["keys"])
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Paste key. Several keys separated by commas are rotated.")
        grid.addWidget(self.key, r, 1)
        self.show_btn = QPushButton("Show")
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(self._toggle_show)
        grid.addWidget(self.show_btn, r, 2)
        r += 1
        grid.addWidget(QLabel("Model"), r, 0)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.addItem(pc["model"])
        self.model.setCurrentText(pc["model"])
        grid.addWidget(self.model, r, 1, 1, 2)
        lay.addLayout(grid)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        self.get_btn = None
        btns = []
        if pdef["key_url"]:
            self.get_btn = QPushButton(pdef["key_label"])
            self.get_btn.clicked.connect(lambda: win.open_url(pdef["key_url"]))
            btns.append(self.get_btn)
        self.fetch_btn = QPushButton("Fetch models")
        self.fetch_btn.clicked.connect(lambda: win.fetch_models(self))
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(lambda: win.test_provider(self))
        btns += [self.fetch_btn, self.test_btn]
        lay.addLayout(h_row(*btns, stretch_end=True))
        lay.addWidget(self.status)

        self.fallback.setChecked(bool(pc["fallback"]))
        self.key.textChanged.connect(lambda _t: win.on_provider_edit(self))
        self.model.currentTextChanged.connect(lambda _t: win.on_provider_edit(self))
        self.fallback.toggled.connect(lambda _v: win.on_provider_edit(self))
        if self.base is not None:
            self.base.textChanged.connect(lambda _t: win.on_provider_edit(self))

    def _build_local(self, lay, pc: dict):
        win = self.win
        self.base = self.key = self.get_btn = self.fetch_btn = None
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.addWidget(QLabel("Model"), 0, 0)
        self.model = QComboBox()
        self.model.addItem("Small: fast, included with the install", "small")
        self.model.addItem("Medium: more accurate, slower, one-time download", "medium")
        self.model.setCurrentIndex(1 if pc.get("model") == "medium" else 0)
        grid.addWidget(self.model, 0, 1, 1, 2)
        self.conf_spin = QSpinBox()
        self.conf_spin.setRange(20, 99)
        self.conf_spin.setSuffix(" %")
        self.conf_spin.setMinimumWidth(110)
        win._bind_spin(self.conf_spin, "local_min_conf")
        grid.addWidget(QLabel("Minimum confidence"), 1, 0)
        grid.addWidget(self.conf_spin, 1, 1)
        self.side_spin = QSpinBox()
        self.side_spin.setRange(640, 4096)
        self.side_spin.setSingleStep(128)
        self.side_spin.setSuffix(" px")
        self.side_spin.setMinimumWidth(110)
        win._bind_spin(self.side_spin, "local_max_side")
        grid.addWidget(QLabel("Largest image side"), 2, 0)
        grid.addWidget(self.side_spin, 2, 1)
        lay.addLayout(grid)
        lay.addWidget(muted("Lines scoring below the minimum confidence are dropped, never guessed. Raise it if junk "
                            "gets through; lower it if real text goes missing. Bigger images read small text better "
                            "but are slower. Supports Japanese, Chinese, English and Latin-script languages (not "
                            "Korean, Russian, Arabic, Hindi or Thai)."))
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(lambda: win.test_provider(self))
        lay.addLayout(h_row(self.test_btn, stretch_end=True))
        lay.addWidget(self.status)
        self.fallback.setChecked(bool(pc["fallback"]))
        self.model.currentIndexChanged.connect(lambda _i: win.on_provider_edit(self))
        self.fallback.toggled.connect(lambda _v: win.on_provider_edit(self))

    def _toggle_show(self, on: bool):
        self.key.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self.show_btn.setText("Hide" if on else "Show")

    def first_key(self) -> str:
        if self.key is None:
            return ""
        keys = parse_keys(self.key.text())
        return keys[0] if keys else ""

    def snapshot(self) -> dict:
        if self.pdef["kind"] == "local":
            return {"keys": "", "model": self.model.currentData() or "small", "base_url": "",
                    "fallback": self.fallback.isChecked()}
        return {
            "keys": self.key.text().strip(),
            "model": self.model.currentText().strip(),
            "base_url": self.base.text().strip() if self.base is not None else self.pdef["base_url"],
            "fallback": self.fallback.isChecked(),
        }

    def set_models(self, models: list):
        cur = self.model.currentText()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(models)
        self.model.setCurrentText(cur)
        self.model.blockSignals(False)

    def set_status(self, text: str, level: str = "info"):
        color = {"info": "#85858d", "ok": "#7ee2a8", "warn": "#f5b942", "err": "#ff6b70"}[level]
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
        self.status.setVisible(bool(text))


# MAIN WINDOW

class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, bus: Bus, startup_error: str = ""):
        super().__init__()
        self.cfg, self.d, self.bus = cfg, cfg.data, bus
        get_cfg = lambda: self.cfg.data  # noqa: E731
        # the GUI keeps its own capture/OCR helpers only for settings work (monitor list, region checks,
        # provider tests); recording and scanning happen in the daemon
        self.capturer, self.ocr = Capturer(), OCRClient(get_cfg)
        self._startup_error = startup_error
        self._closing = False
        self._restore_state = None
        self.state = "idle"
        self.captures = 0
        self.lines_total = 0
        self.daemon_ok = None
        self.session_file = None
        self._hk_last = None
        self._last_select_id = 0
        self._selecting = False
        self._select_cb = None
        self._overlay = None
        self._really_quit = False
        self.cards = {}
        self.tray = None
        self.link = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._flush_config)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_app_icon())
        self.resize(1040, 740)
        self.setMinimumSize(880, 620)
        self._build_ui()
        self._build_tray()

        bus.log.connect(self.add_log)
        bus.command.connect(self.on_command)
        bus.update.connect(self._on_update)
        bus.connection.connect(self._on_connection)
        bus.reply.connect(self._on_reply)
        bus.select_done.connect(self._on_select_done)
        bus.shots_ready.connect(self._on_shots_ready)
        bus.failure.connect(self._report_error)
        bus.models_ready.connect(self._on_models_ready)
        bus.test_done.connect(self._on_test_done)
        bus.notify.connect(self.notify)

        self._set_state("idle")
        self._apply_mode_ui()
        self.refresh_monitors()
        self._refresh_hotkey_hints()
        self._refresh_session_label()
        self._startup_notes()

    
    def save_soon(self):
        self._save_timer.start()

    def _flush_config(self, on_done=None):
        """Write the settings file, then tell the daemon to re-read it (credentials never travel over IPC)."""
        self._save_timer.stop()
        err = self.cfg.save()
        if err:
            self._report_error(err)
            return
        self.send("reload_config", on_ok=lambda _d: on_done() if on_done else None)

    def _sync_config(self, on_done):
        """Run `on_done` once the daemon has the latest settings."""
        if self._save_timer.isActive():
            self._flush_config(on_done)
        else:
            on_done()

    # talking to the daemon
    def send(self, cmd: str, args=None, on_ok=None):
        """Run an IPC command off the GUI thread; the reply is handled on the GUI thread."""
        def work():
            self.bus.post("reply", cmd, request(cmd, args or {}, timeout=15.0), on_ok)
        threading.Thread(target=work, daemon=True, name=f"ipc-{cmd}").start()

    def _on_reply(self, cmd: str, reply, on_ok):
        if reply is None:
            self._on_connection(False)
            self.add_log("err", "The daemon is not running. Press 'Start daemon' in the sidebar.")
            return
        if not reply.get("ok"):
            err = reply.get("error") or {}
            self.add_log("err", err.get("message", "The command failed."))
            if err.get("code") == "ocr_unavailable":
                self.show_page(1)
            return
        data = reply.get("data") or {}
        if isinstance(data, dict) and "state" in data:
            self._set_state(data["state"])
        if on_ok:
            on_ok(data)

    def connect_daemon(self):
        """Start polling the daemon, and start the daemon itself if it is not running."""
        self.link = DaemonLink(lambda d, replay: self.bus.post("update", d, replay),
                               lambda ok: self.bus.post("connection", ok))
        self.link.start()
        self.ensure_daemon()

    def ensure_daemon(self):
        def work():
            ok, msg = start_daemon()
            self.bus.post("log", "ok" if ok else "err", msg)
        threading.Thread(target=work, daemon=True, name="start-daemon").start()

    def _on_connection(self, ok: bool):
        if ok == self.daemon_ok:
            return
        self.daemon_ok = ok
        self.btn_daemon.setVisible(not ok)
        if ok:
            self.add_log("ok", "Connected to the daemon.")
        else:
            self.add_log("warn", "Lost the connection to the daemon.")
            self.session_file = None
            self._refresh_session_label()
        self._set_state(self.state if ok else "offline")

    def _on_update(self, data: dict, replay: bool):
        if self._closing:
            return
        self.captures, self.lines_total = data["captures"], data["lines"]
        for ev in data["events"]:
            kind = ev["type"]
            if kind == "log":
                self.add_log(ev["level"], ev["message"], to_file=False)
            elif kind == "result":
                self._show_result(ev)
            elif kind == "notify" and not replay:
                self.notify(ev["title"], ev["body"], force=True)
            elif kind == "show" and not replay:
                self.on_command("show")
        self._set_state(data["state"])
        sf = data.get("session_file")
        if sf != (str(self.session_file) if self.session_file else None):
            self.session_file = Path(sf) if sf else None
            self._refresh_session_label()
        hk = data.get("hotkeys")
        if hk and hk != self._hk_last:
            self._hk_last = hk
            self._show_hotkey_status(hk["ok"], hk["message"], hk["level"])
        sel = data.get("select")
        if sel and sel["id"] != self._last_select_id:
            self._last_select_id = sel["id"]
            self.send("select_ack", {"id": sel["id"]})
            if not self.select_region(self._oneshot_selected):
                self.send("scan_region", {"region": None, "silent": True})

    def _report_error(self, msg: str):
        """Errors the user has to see: activity log plus a notification even if the window is hidden."""
        self.add_log("err", msg)
        self.notify(APP_NAME, msg, force=True)

    def set_cfg(self, key, value):
        self.d[key] = value
        self.save_soon()

    def open_url(self, url: str):
        if not QDesktopServices.openUrl(QUrl(url)):
            webbrowser.open(url)

    def add_log(self, level: str, msg: str, to_file: bool = True):
        if to_file:  # events that come from the daemon are already in the daemon's log file
            log.log({"err": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO), "%s", msg)
        color = {"info": "#9a9aa2", "ok": "#7ee2a8", "warn": "#f5b942", "err": "#ff6b70"}.get(level, "#9a9aa2")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendHtml(
            f'<span style="color:#5f5f67">{stamp}</span>&nbsp;&nbsp;<span style="color:{color}">{html.escape(msg)}</span>')

    def notify(self, title: str, body: str, force: bool = False):
        if not force and not self.d["notifications"]:
            return
        if IS_LINUX and shutil.which("notify-send"):
            try:
                subprocess.Popen(["notify-send", "-a", APP_NAME, "-t", "3500", title, body[:300]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, body[:300], QSystemTrayIcon.MessageIcon.NoIcon, 3500)

    def _startup_notes(self):
        self.add_log("info", f"Capture backend: {self.capturer.backend}")
        prob = self.capturer.problem(need_region=True)
        if prob:
            self.add_log("warn", prob)
        local_err = self.ocr.local.problem()
        if local_err is not None and self.d["active_provider"] == "local":
            self.add_log("warn", local_err)
        if not self.ocr.chain(self.d):
            self.add_log("warn", "No OCR provider is ready yet. Install Local OCR (no key needed) or add a free key "
                                 "in the AI Keys tab.")
        if self._startup_error:
            self._report_error(self._startup_error)
        if self.d["mode"] == "region" and self.d["region"] is not None:
            err = self.capturer.region_error(self.d["region"])
            if err:
                self.add_log("warn", err)

    # ui
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 22, 14, 18)
        sl.setSpacing(6)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        sl.addWidget(brand)
        sl.addWidget(muted("Screen text to file"))
        sl.addSpacing(18)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_btns = []
        for i, name in enumerate(("Capture", "OCR Mode", "Settings")):
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, idx=i: self.show_page(idx))
            self.nav_group.addButton(b)
            self.nav_btns.append(b)
            sl.addWidget(b)
        sl.addStretch(1)
        self.side_status = QLabel("")
        self.side_status.setObjectName("muted")
        sl.addWidget(self.side_status)
        self.btn_daemon = QPushButton("Start daemon")
        self.btn_daemon.setToolTip("The background daemon is not running. Recording and scans need it.")
        self.btn_daemon.clicked.connect(self.ensure_daemon)
        self.btn_daemon.setVisible(False)
        sl.addWidget(self.btn_daemon)
        sl.addWidget(muted(f"v{VERSION}"))
        outer.addWidget(side)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_capture_page())
        self.stack.addWidget(self._build_keys_page())
        self.stack.addWidget(self._build_settings_page())
        outer.addWidget(self.stack, 1)
        self.show_page(0)

    def show_page(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self.nav_btns[idx].setChecked(True)

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(30, 28, 30, 28)
        lay.setSpacing(16)
        return w, lay

    # capture page
    def _build_capture_page(self):
        page, lay = self._page()
        h, s = page_header("Capture", "Read the whole screen, or just a region. New text is appended to your output file.")
        lay.addWidget(h)
        lay.addWidget(s)

        card, cl = make_card("Recording", "Reads the screen again every few seconds and saves text that changed.")
        self.btn_full = QPushButton("Full screen")
        self.btn_region = QPushButton("Region")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for b in (self.btn_full, self.btn_region):
            b.setObjectName("seg")
            b.setMinimumWidth(130)
            b.setCheckable(True)
            self.mode_group.addButton(b)
        self.btn_full.clicked.connect(lambda: self._set_mode("full"))
        self.btn_region.clicked.connect(lambda: self._set_mode("region"))
        cl.addLayout(h_row(self.btn_full, self.btn_region, stretch_end=True))

        self.monitor_row = QWidget()
        mr = QHBoxLayout(self.monitor_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.setSpacing(10)
        mr.addWidget(QLabel("Monitor"))
        self.monitor_combo = QComboBox()
        self.monitor_combo.setMinimumWidth(260)
        self.monitor_combo.currentIndexChanged.connect(self._monitor_changed)
        mr.addWidget(self.monitor_combo)
        mr.addStretch(1)
        cl.addWidget(self.monitor_row)

        self.region_row = QWidget()
        rr = QHBoxLayout(self.region_row)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(10)
        self.btn_select_region = QPushButton("Select region")
        self.btn_select_region.clicked.connect(self.pick_region_clicked)
        self.region_label = QLabel("")
        self.region_label.setWordWrap(True)
        rr.addWidget(self.btn_select_region)
        rr.addWidget(self.region_label, 1)
        cl.addWidget(self.region_row)

        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.start_recording)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.stop_recording)
        cl.addLayout(h_row(self.btn_start, self.btn_pause, self.btn_stop, stretch_end=True))

        self.status_dot = QLabel("\u25cf")
        self.status_text = QLabel("Idle")
        self.stats_label = muted("")
        self.stats_label.setWordWrap(False)
        cl.addLayout(h_row(self.status_dot, self.status_text, 16, self.stats_label, stretch_end=True))
        lay.addWidget(card)

        card2, c2 = make_card("Quick scan", "One-off scans that work with or without recording.")
        self.btn_oneshot = QPushButton("Select new region and scan")
        self.btn_oneshot.setToolTip("Opens the region selector and reads the area you drag. The region is not stored.")
        self.btn_oneshot.clicked.connect(self.one_shot)
        self.hotkey_hint = muted("")
        c2.addLayout(h_row(self.btn_oneshot, self.hotkey_hint, stretch_end=True))
        self.btn_current = QPushButton("Scan current")
        self.btn_current.setToolTip("Region mode: reads the stored region, no selector. Full-screen mode: reads the configured monitor.")
        self.btn_current.clicked.connect(self.scan_current)
        self.hotkey_hint_current = muted("")
        c2.addLayout(h_row(self.btn_current, self.hotkey_hint_current, stretch_end=True))
        lay.addWidget(card2)

        card3, c3 = make_card("Activity")
        self.last_text = QPlainTextEdit()
        self.last_text.setReadOnly(True)
        self.last_text.setPlaceholderText("The last text that was read appears here.")
        self.last_text.setFixedHeight(96)
        c3.addWidget(self.last_text)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(400)
        self.log_view.setMinimumHeight(120)
        c3.addWidget(self.log_view, 1)
        btn_file = QPushButton("Open output file")
        btn_file.clicked.connect(self.open_output_file)
        btn_dir = QPushButton("Open folder")
        btn_dir.clicked.connect(self.open_output_folder)
        c3.addLayout(h_row(btn_file, btn_dir, stretch_end=True))
        lay.addWidget(card3, 1)
        return scroll_wrap(page)

    # ai keys page
    def _build_keys_page(self):
        page, lay = self._page()
        h, s = page_header("AI Keys", "Pick what reads your screen. Local OCR needs no key; cloud providers have free tiers.")
        lay.addWidget(h)
        lay.addWidget(s)

        self.fallback_chk = QCheckBox("If the active provider fails, automatically try the other providers marked as fallback")
        self.fallback_chk.setChecked(bool(self.d["fallback"]))
        self.fallback_chk.toggled.connect(lambda v: self.set_cfg("fallback", bool(v)))
        lay.addWidget(self.fallback_chk)
        lay.addWidget(muted("Tip: paste several keys (comma separated) into one provider and they are used in turn, "
                            "which stretches free-tier rate limits. Screenshots are sent to the cloud provider you choose; Local OCR never leaves this computer."))

        self.active_group = QButtonGroup(self)
        self.active_group.setExclusive(True)
        for pdef in PROVIDERS:
            card = ProviderCard(self, pdef)
            self.cards[pdef["id"]] = card
            self.active_group.addButton(card.active)
            card.active.setChecked(self.d["active_provider"] == pdef["id"])
            card.active.toggled.connect(lambda on, pid=pdef["id"]: self._active_changed(pid, on))
            lay.addWidget(card)
        lay.addStretch(1)
        return scroll_wrap(page)

    def _active_changed(self, pid: str, on: bool):
        if not on:
            return
        self.set_cfg("active_provider", pid)  # the daemon warms the local engine when it reloads the settings

    # -- settings page
    def _bind_check(self, chk: QCheckBox, key: str):
        chk.setChecked(bool(self.d[key]))
        chk.toggled.connect(lambda v, k=key: self.set_cfg(k, bool(v)))

    def _bind_spin(self, spin: QSpinBox, key: str):
        spin.setValue(int(self.d[key]))
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda v, k=key: self.set_cfg(k, int(v)))

    def _build_settings_page(self):
        page, lay = self._page()
        h, s = page_header("Settings", "Everything here is saved automatically on this computer.")
        lay.addWidget(h)
        lay.addWidget(s)

        # output
        card, cl = make_card("Output file", "Every launch writes to a NEW file named from this path plus the date and "
                                            "time, e.g. ocr_output_2026-01-31_14-05-09.txt. Earlier files are never touched. "
                                            "A changed path applies from the next launch.")
        self.session_label = QLabel("")
        self.session_label.setWordWrap(True)
        self.session_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.session_label)
        self._refresh_session_label()
        self.out_edit = QLineEdit(self.d["output_file"])
        self.out_edit.editingFinished.connect(self._output_edited)
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self.browse_output)
        cl.addLayout(h_row(self.out_edit, btn_browse))
        self.out_edit.setMinimumWidth(320)
        c_ts = QCheckBox("Add a timestamp to every saved line")
        c_dup = QCheckBox("Skip text that is identical to the previous capture")
        c_new = QCheckBox("Only save lines not already saved in this session")
        for chk, key in ((c_ts, "timestamps"), (c_dup, "skip_duplicates"), (c_new, "only_new_lines")):
            self._bind_check(chk, key)
            cl.addWidget(chk)
        lay.addWidget(card)

        # recording
        card, cl = make_card("Recording", "How often the screen is read while recording.")
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(250, 600000)
        self.spin_interval.setSingleStep(250)
        self.spin_interval.setSuffix(" ms")
        self.spin_interval.setMinimumWidth(130)
        self._bind_spin(self.spin_interval, "interval_ms")
        cl.addLayout(h_row(QLabel("Delay between captures"), self.spin_interval, stretch_end=True))
        cl.addWidget(muted("Free tiers allow roughly 10 to 30 requests per minute. Keep this at 3000 ms or more unless you rotate several keys."))
        c_unch = QCheckBox("Skip the AI call when the screen has not changed (saves quota)")
        c_hide = QCheckBox("Hide this window while selecting a region (turn off on tiling window managers)")
        for chk, key in ((c_unch, "skip_unchanged"), (c_hide, "hide_on_select")):
            self._bind_check(chk, key)
            cl.addWidget(chk)
        self.spin_side = QSpinBox()
        self.spin_side.setRange(640, 8192)
        self.spin_side.setSingleStep(128)
        self.spin_side.setSuffix(" px")
        self.spin_side.setMinimumWidth(130)
        self._bind_spin(self.spin_side, "max_side")
        cl.addLayout(h_row(QLabel("Largest image side sent to cloud AI"), self.spin_side, stretch_end=True))
        lay.addWidget(card)

        # ocr
        card, cl = make_card("Text recognition", "Hints that make the AI more accurate.")
        self.lang_combo = QComboBox()
        self.lang_combo.setEditable(True)
        self.lang_combo.addItems(LANGUAGES)
        self.lang_combo.setCurrentText(self.d["language"])
        self.lang_combo.setMinimumWidth(220)
        self.lang_combo.currentTextChanged.connect(lambda t: self.set_cfg("language", t.strip()))
        cl.addLayout(h_row(QLabel("Language of the text"), self.lang_combo, stretch_end=True))
        self.extra_edit = QLineEdit(self.d["extra_prompt"])
        self.extra_edit.setPlaceholderText("Optional extra instructions, e.g. 'only read the subtitles at the bottom'")
        self.extra_edit.textChanged.connect(lambda t: self.set_cfg("extra_prompt", t.strip()))
        cl.addWidget(self.extra_edit)
        cl.addWidget(muted("Extra instructions apply to cloud providers only. Local OCR reads exactly what is printed."))
        lay.addWidget(card)

        # hotkeys
        card, cl = make_card("Hotkeys", "Click a box and press the keys you want. Clear the box to disable a hotkey.")
        self.hk_edits = {}
        for action in HOTKEY_ORDER:
            edit = QKeySequenceEdit()
            if hasattr(edit, "setMaximumSequenceLength"):
                edit.setMaximumSequenceLength(1)
            if hasattr(edit, "setClearButtonEnabled"):
                edit.setClearButtonEnabled(True)
            edit.setKeySequence(QKeySequence.fromString(self.d["hotkeys"].get(action, ""), QKeySequence.SequenceFormat.PortableText))
            edit.setMinimumWidth(220)
            edit.editingFinished.connect(lambda a=action: self._hotkey_edited(a))
            self.hk_edits[action] = edit
            lab = QLabel(HOTKEY_LABELS[action])
            lab.setMinimumWidth(170)
            cl.addLayout(h_row(lab, edit, stretch_end=True))
        self.hk_status = QLabel("")
        self.hk_status.setWordWrap(True)
        cl.addWidget(self.hk_status)
        lay.addWidget(card)

        # compositor binds (Linux)
        self.bind_card = None
        if IS_LINUX:
            self.desktop = detect_desktop()
            card, cl = make_card("Manual hotkey setup" if self.desktop != "hyprland" else "Hyprland / compositor binds",
                                 "Used when the desktop portal can't register global hotkeys (see the status above). "
                                 "Set them up once in your desktop; they trigger the running app.")
            self.syntax_combo = QComboBox()
            self.syntax_combo.addItem("Hyprland Lua (0.55+)", "lua")
            self.syntax_combo.addItem("Hyprland hyprlang (hyprland.conf)", "conf")
            cur = self.d["hypr_syntax"] if self.d["hypr_syntax"] in ("lua", "conf") else detect_hypr_syntax()
            self.syntax_combo.setCurrentIndex(0 if cur == "lua" else 1)
            self.syntax_combo.currentIndexChanged.connect(self._syntax_changed)
            self.syntax_combo.setVisible(self.desktop == "hyprland")
            self.snippet = QPlainTextEdit()
            self.snippet.setReadOnly(True)
            self.snippet.setFixedHeight(190)
            self.snippet.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            btn_copy = QPushButton("Copy")
            btn_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.snippet.toPlainText()))
            cl.addLayout(h_row(self.syntax_combo, btn_copy, stretch_end=True))
            cl.addWidget(self.snippet)
            lay.addWidget(card)
            self.bind_card = card
            self.refresh_snippet()

        # general
        card, cl = make_card("General")
        self.chk_tray = QCheckBox("Closing the window hides it in the system tray (the daemon keeps running)")
        self.chk_tray.setChecked(True)
        self.chk_tray.setEnabled(False)
        if self.tray is None:
            self.chk_tray.setEnabled(False)
        c_notif = QCheckBox("Show a notification after a hotkey scan")
        self._bind_check(c_notif, "notifications")
        cl.addWidget(self.chk_tray)
        cl.addWidget(c_notif)
        lay.addWidget(card)
        lay.addStretch(1)
        return scroll_wrap(page)

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            if hasattr(self, "chk_tray"):
                self.chk_tray.setEnabled(False)
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        for text, fn in (("Open Window", lambda: self.on_command("show")),
                         ("Open Output Folder", self.open_output_folder),
                         ("Stop Daemon", self.quit_and_stop_daemon)):
            act = QAction(text, menu)
            act.triggered.connect(lambda _c=False, f=fn: f())
            menu.addAction(act)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.on_command("show")
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()
        if hasattr(self, "chk_tray"):
            self.chk_tray.setEnabled(True)

    
    def _refresh_session_label(self):
        p = self.session_file
        if self.daemon_ok is None:
            text = "This session: (connecting to the daemon...)"
        elif not self.daemon_ok:
            text = "This session: (the daemon is not running)"
        else:
            text = f"This session: {p}" if p else "This session: (no file could be created, see the log)"
        self.session_label.setText(text)

    def _output_edited(self):
        new = self.out_edit.text().strip() or default_config()["output_file"]
        if new != self.d["output_file"]:
            self.add_log("info", "Output path saved. It applies the next time the daemon starts; this session keeps its file.")
        self.set_cfg("output_file", new)
        self.out_edit.setText(self.d["output_file"])

    def browse_output(self):
        start = os.path.expanduser(self.d["output_file"])
        path, _ = QFileDialog.getSaveFileName(self, "Choose the output base name", start,
                                              "Text files (*.txt);;All files (*)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.out_edit.setText(path)
            self._output_edited()

    def open_output_file(self):
        p = self.session_file
        if p is None or not p.exists():
            self.add_log("warn", "The session output file does not exist (it could not be created).")
            return
        self.open_url(QUrl.fromLocalFile(str(p)).toString())

    def open_output_folder(self):
        p = self.session_file
        if p is None or not p.parent.exists():
            self.add_log("warn", "The session output folder does not exist (the file could not be created).")
            return
        self.open_url(QUrl.fromLocalFile(str(p.parent)).toString())

    def _hotkey_edited(self, action: str):
        edit = self.hk_edits[action]
        seq = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        err = validate_hotkey(seq) if seq else None
        if not err and seq and seq in [v for k, v in self.d["hotkeys"].items() if k != action and v]:
            err = "already used by another action"
        if err:
            self.hk_status.setStyleSheet("color: #ff6b70;")
            self.hk_status.setText(f"Not saved: {err}.")
            edit.setKeySequence(QKeySequence.fromString(self.d["hotkeys"].get(action, ""), QKeySequence.SequenceFormat.PortableText))
            return
        self.d["hotkeys"][action] = seq
        self.save_soon()
        self.apply_hotkeys()
        self.refresh_snippet()

    def apply_hotkeys(self):
        """Hotkeys are registered by the daemon: save, tell it to re-read the settings, show what it says."""
        self._refresh_hotkey_hints()
        self._flush_config()

    def _refresh_hotkey_hints(self):
        self.hotkey_hint.setText(f"Hotkey: {self.d['hotkeys'].get('region') or '(not set)'}")
        self.hotkey_hint_current.setText(f"Hotkey: {self.d['hotkeys'].get('current') or '(not set)'}")

    def _show_hotkey_status(self, ok: bool, msg: str, level: str):
        # errors are already reported (log + notification) by the daemon; the GUI only shows the state
        color = {"ok": "#7ee2a8", "info": "#85858d", "warn": "#f5b942", "err": "#ff6b70"}[level]
        self.hk_status.setStyleSheet(f"color: {color};")
        self.hk_status.setText(msg)

    def _syntax_changed(self):
        self.set_cfg("hypr_syntax", self.syntax_combo.currentData())
        self.refresh_snippet()

    def refresh_snippet(self):
        if self.bind_card is None:
            return
        self.snippet.setPlainText(manual_bind_help(self.d["hotkeys"], self.desktop, self.syntax_combo.currentData()))

    
    def on_provider_edit(self, card: ProviderCard):
        self.d["providers"][card.pdef["id"]] = {**self.d["providers"][card.pdef["id"]], **card.snapshot()}
        self.save_soon()

    def fetch_models(self, card: ProviderCard):
        pdef, snap, key = card.pdef, card.snapshot(), card.first_key()
        card.set_status("Fetching models...")

        def work():
            try:
                self.bus.post("models_ready", pdef["id"], fetch_models(pdef, snap, key), "")
            except Exception as exc:
                self.bus.post("models_ready", pdef["id"], None, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _on_models_ready(self, pid: str, models, err: str):
        card = self.cards[pid]
        if err:
            card.set_status(err, "err")
        elif not models:
            card.set_status("No models found. You can still type a model name.", "warn")
        else:
            card.set_models(models)
            card.set_status(f"{len(models)} models loaded. Pick one from the Model box.", "ok")

    def test_provider(self, card: ProviderCard):
        pdef, snap, key = card.pdef, card.snapshot(), card.first_key()
        if pdef["needs_key"] and not key:
            card.set_status("Enter an API key first.", "err")
            return
        cfg_copy = dict(self.d)
        if pdef["kind"] == "local":
            card.set_status("Loading the local engine and reading a sample image...")

            def work_local():
                try:
                    text, ms = self.ocr.local.selftest(cfg_copy)
                    self.bus.post("test_done", pdef["id"], "TEST" in text.upper(), f"{text or '(nothing read)'}  [{ms} ms]")
                except Exception as exc:
                    self.bus.post("test_done", pdef["id"], False, f"ERROR:{exc}")
            threading.Thread(target=work_local, daemon=True).start()
            return
        card.set_status("Testing with a sample image...")

        def work():
            try:
                text = run_test(pdef, snap, key, cfg_copy)
                ok = "TEST" in text.upper()
                self.bus.post("test_done", pdef["id"], ok, text or "(empty reply)")
            except Exception as exc:
                self.bus.post("test_done", pdef["id"], False, f"ERROR:{exc}")
        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, pid: str, ok: bool, text: str):
        card = self.cards[pid]
        if text.startswith("ERROR:"):
            card.set_status(text[6:], "err")
        elif ok:
            card.set_status(f"Works. Read: {text.replace(chr(10), ' / ')}", "ok")
        else:
            card.set_status(f"It answered but did not read the sample correctly: {text[:120]}", "warn")

    
    def _set_mode(self, mode: str):
        self.set_cfg("mode", mode)
        self._apply_mode_ui()

    def _apply_mode_ui(self):
        full = self.d["mode"] == "full"
        self.btn_full.setChecked(full)
        self.btn_region.setChecked(not full)
        self.monitor_row.setVisible(full)
        self.region_row.setVisible(not full)
        self._refresh_region_label()

    def _refresh_region_label(self):
        region = self.d["region"]
        if not region:
            self.region_label.setText("No region selected")
            return
        err = self.capturer.region_error(region)  # never discards the stored region; it only says why it is unusable
        text = describe_region(region)
        self.region_label.setText(text if not err else f"{text}  \u00b7  not usable: {err}")

    def refresh_monitors(self):
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        try:
            items = self.capturer.list_monitors()
        except CaptureError as exc:
            items = [("Primary / focused monitor", "auto"), ("All monitors", "all")]
            self.add_log("warn", str(exc))
        for label, key in items:
            self.monitor_combo.addItem(label, key)
        idx = self.monitor_combo.findData(self.d["monitor"])
        if idx < 0:  # keep the choice visible; a scan then fails with a clear error instead of switching screens
            self.monitor_combo.addItem(f"{self.d['monitor']} (not connected)", self.d["monitor"])
            idx = self.monitor_combo.count() - 1
            self.add_log("warn", f"The configured monitor '{self.d['monitor']}' is not connected. "
                                 "Choose another one on the Capture page.")
        self.monitor_combo.setCurrentIndex(idx)
        self.monitor_combo.blockSignals(False)

    def _monitor_changed(self, _i):
        key = self.monitor_combo.currentData()
        if key is not None:
            self.set_cfg("monitor", key)

   
    def _set_state(self, state: str):
        self.state = state
        idle = state in ("idle", "offline")
        online = state != "offline"
        self.btn_start.setEnabled(state == "idle")
        self.btn_pause.setEnabled(not idle)
        self.btn_stop.setEnabled(not idle)
        for w in (self.btn_oneshot, self.btn_current):
            w.setEnabled(online)
        self.btn_pause.setText("Resume" if state == "paused" else "Pause")
        for w in (self.btn_full, self.btn_region, self.monitor_combo, self.btn_select_region):
            w.setEnabled(idle)
        color, text = {"idle": ("#6b6b73", "Idle"), "recording": ("#ff5a5f", "Recording"),
                       "paused": ("#f5b942", "Paused"), "offline": ("#6b6b73", "Daemon offline")}[state]
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.status_text.setText(text)
        self.side_status.setText(f"\u25cf {text}")
        self.side_status.setStyleSheet(f"color: {color};")
        self.stats_label.setText(f"{self.captures} scans \u00b7 {self.lines_total} lines saved this run")

    # recording: the daemon does the work, these send commands
    def start_recording(self):
        if self._selecting or self.state != "idle":
            return
        if self.d["mode"] == "region" and not self.capturer.region_ok(self.d["region"]):
            if self.d["region"] is not None:
                self.add_log("warn", f"{self.capturer.region_error(self.d['region'])} Opening the selector.")
            self.select_region(self._region_then_start)
            return
        self._sync_config(lambda: self.send("start"))

    def _region_then_start(self, region, _img):
        if region is None:
            return
        self._store_region(region)
        self._flush_config(lambda: self.send("start"))

    def stop_recording(self):
        self.send("stop")

    def toggle_pause(self):
        self.send("pause")  # the daemon toggles pause / resume

   # region select
    def pick_region_clicked(self):
        prob = self.capturer.problem(need_region=True)
        if prob:
            self.add_log("err", prob)
            return
        self.select_region(lambda region, _img: self._store_region(region) if region else None)

    def _store_region(self, region):
        self.d["region"] = region
        self.save_soon()
        self._refresh_region_label()
        self.add_log("ok", f"Region set: {describe_region(region)}")

    def select_region(self, callback) -> bool:
        """Starts a selection. callback(region, image) runs on the GUI thread; both None if it was
        cancelled or failed (the reason is logged). Returns False if a selection could not be started."""
        if self._selecting or self._closing:
            return False
        self._selecting = True
        self._select_cb = callback
        self._restore_state = None
        hide = bool(self.d["hide_on_select"]) and self.isVisible()
        if hide:
            self._restore_state = (self.windowState(), self.isActiveWindow())
            self.hide()
        threading.Thread(target=self._select_worker, args=(0.3 if hide else 0.03,), daemon=True).start()
        return True

    def _select_worker(self, delay: float):
        """Screenshot / slurp work happens here so the GUI stays responsive."""
        time.sleep(delay)
        try:
            if self.capturer.wayland:
                region = self.capturer.select_region_wayland()
                img = self.capturer.grab_region(region) if region else None
                self.bus.post("select_done", (region, img, None))
            else:
                self.bus.post("shots_ready", self.capturer.grab_all_monitors())
        except (CaptureError, OSError) as exc:
            self.bus.post("select_done", (None, None, str(exc)))
        except Exception as exc:  # thread boundary: the GUI must always get a result or it stays stuck selecting
            log.exception("Region selection failed")
            self.bus.post("select_done", (None, None, f"Region selector failed: {exc}"))

    def _on_shots_ready(self, shots):
        if not self._selecting or self._closing:
            return
        try:
            mgr = OverlayManager(shots, QGuiApplication.screens())
            if mgr.unmatched:
                self.add_log("warn", f"{len(mgr.unmatched)} screen(s) could not be matched to a capture monitor "
                                     "and cannot be used for selection.")
            self._overlay = mgr
            mgr.finished.connect(self._on_overlay_finished)
            mgr.start()
        except Exception as exc:  # GUI boundary: whatever fails here must still end the selection state
            log.exception("Could not open the region selector")
            self._on_select_done((None, None, f"Region selector failed: {exc}"))

    def _on_overlay_finished(self, payload, err: str):
        self._overlay = None
        if payload is None:
            self._on_select_done((None, None, err or None))
        else:
            self._on_select_done((payload[0], payload[1], None))

    def _on_select_done(self, payload):
        if not self._selecting:
            return
        region, img, err = payload
        self._selecting = False
        self._restore_window()
        cb, self._select_cb = self._select_cb, None
        if err:
            self.add_log("err", err)
        elif region is None or img is None:
            self.add_log("info", "Selection cancelled.")
        ok = not err and region is not None and img is not None
        if cb:
            cb(region if ok else None, img if ok else None)

    def _restore_window(self):
        st, self._restore_state = self._restore_state, None
        if st is None:
            return
        state, was_active = st
        if state & Qt.WindowState.WindowMinimized:
            self.showMinimized()
            return
        if state & Qt.WindowState.WindowMaximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        if was_active:  # do not steal focus from another app if we were not focused before
            self.activateWindow()

    def _cancel_selection(self):
        if self._overlay is not None:
            self._overlay.cancel()
            self._overlay = None
        self.capturer.cancel_selection()
        self._selecting = False
        self._select_cb = None

    # "select new region and scan": the daemon asks for the selector (see _on_update) and scans the result
    def one_shot(self):
        if self._closing or self._selecting:
            return
        self.send("region")

    def _oneshot_selected(self, region, _img):
        if region is None:  # cancelled or failed: the reason is already in the log; free the daemon's busy flag
            self.send("scan_region", {"region": None, "silent": True})
        else:
            self.send("scan_region", {"region": region})

    # scan current: the stored region (region mode) or the configured screen (full mode), no selector
    def scan_current(self):
        if self._closing:
            return
        self._sync_config(lambda: self.send("current"))

    def _show_result(self, ev: dict):
        text, lines, provider = ev["text"], ev["lines"], ev["provider"]
        if text.strip():
            self.last_text.setPlainText(text)
            if lines:
                self.add_log("ok", f"Saved {lines} line{'s' if lines != 1 else ''} ({provider})", to_file=False)
            else:
                self.add_log("info", f"Same text as before, not saved ({provider})", to_file=False)
        else:
            self.add_log("info", f"No text found ({provider})", to_file=False)

    def on_command(self, cmd: str):
        if cmd == "show":
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def quit_app(self):
        """Hide the window; only stopping the daemon exits the GUI."""
        self.hide()

    def quit_and_stop_daemon(self):
        request("shutdown", timeout=2.0)
        self._really_quit = True
        self.close()

    def shutdown(self):
        """Idempotent. Stops the GUI's own threads; never the daemon."""
        if self._closing:
            return
        self._closing = True
        self.bus.close()  # worker threads may keep running; their signals are now dropped
        self._cancel_selection()
        if self.link is not None:
            self.link.stop()
        self._save_timer.stop()
        err = self.cfg.save()
        if err:
            log.error("%s", err)

    def closeEvent(self, event):
        if self.tray is not None and self.tray.isVisible() and not self._really_quit:
            event.ignore()
            self.hide()
            return
        self.shutdown()
        event.accept()
        QApplication.quit()


def main() -> int:
    hidden = "--hidden" in sys.argv[1:]
    lock = InstanceLock("gui")
    if not lock.acquire():
        send_command("show")  # the running window comes forward
        print(f"{APP_NAME} is already running; brought its window forward.")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # overlays and tray must not end the app
    QGuiApplication.setDesktopFileName(APP_ID)  # Wayland app_id, handy for window rules
    app.setStyle("Fusion")
    try:
        check, arrow = make_assets()
        app.setStyleSheet(QSS.replace("@CHECK@", check).replace("@ARROW@", arrow))
    except Exception:
        app.setStyleSheet(QSS.replace("image: url(\"@CHECK@\");", "").replace("image: url(\"@ARROW@\");", ""))

    setup_logging()
    cfg = Config(writer=True)  # the GUI is the one process that migrates plain-text API keys
    bus = Bus()
    win = MainWindow(cfg, bus)
    app.aboutToQuit.connect(win.shutdown)
    if hidden:
        # Autostart keeps only the tray icon visible; the window opens on demand.
        win.hide()
    else:
        win.show()
    win.connect_daemon()
    if cfg.migrated:
        win.add_log("ok", "API keys were moved from the config file to the system credential store.")
    if cfg.secret_status:
        win.add_log("warn", cfg.secret_status)
    code = app.exec()
    win.shutdown()
    lock.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
