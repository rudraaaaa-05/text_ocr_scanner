"""The Dictionary tab (widgets only; all logic is in ocr_dict.py) and the popup that looks up selected text."""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QObject, QPoint, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

import ocr_dict as od
from ocr_nlp import romaji_to_kana

DICT_QSS = """
QFrame#banner { background: #2a2417; border: 1px solid #5c4a1e; border-radius: 10px; }
QFrame#banner[level="error"] { background: #2a1a1a; border: 1px solid #6b2c2c; }
QLabel#cmd { font-family: monospace; background: #101013; border: 1px solid #25252a; border-radius: 6px; padding: 6px 8px; }
QPushButton#kindBtn { font-size: 15px; font-weight: 700; padding: 12px 8px; }
QPushButton#kindBtn:checked, QPushButton#alphaBtn:checked { background: #ececee; color: #0e0e10; }
QPushButton#alphaBtn { padding: 5px 0; min-width: 22px; }
QPushButton#navArrow { padding: 8px 14px; font-size: 15px; }
QPushButton#kanaToggle { padding: 8px 12px; font-size: 15px; font-weight: 700; }
QPushButton#kanaToggle:checked { background: #ececee; color: #0e0e10; }
QListView#entryList, QListView#recentList { background: transparent; border: none; outline: 0; }
QLabel#dictName { font-size: 15px; font-weight: 700; }
"""

PAGE_HOME, PAGE_LIST = 0, 1
ROW_H = 64


def _muted(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


def _card() -> tuple:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    return frame, lay


def _scaled(font: QFont, factor: float, bold: bool = False) -> QFont:
    f = QFont(font)
    if f.pixelSize() > 0:
        f.setPixelSize(max(1, int(round(f.pixelSize() * factor))))
    elif f.pointSizeF() > 0:
        f.setPointSizeF(f.pointSizeF() * factor)
    f.setBold(bold)
    return f


# --------------------------------------------------------------------------------------------------------------
# list rows: painted by a delegate, so a list never creates one widget per entry
# --------------------------------------------------------------------------------------------------------------

class EntryListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list = []

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self._entries)

    def data(self, index, role=None):                  # Qt always passes the role; no Qt enum is touched at import time
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        e = self._entries[index.row()]
        if role is None or role == Qt.ItemDataRole.DisplayRole:
            return od.row_texts(e)[0]
        if role == Qt.ItemDataRole.UserRole:
            return e
        if role == Qt.ItemDataRole.ToolTipRole:
            return e.short_meaning(400)
        return None

    def set_entries(self, entries: list):
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def entry_at(self, row: int):
        return self._entries[row] if 0 <= row < len(self._entries) else None


class EntryDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_H)

    def paint(self, painter, option, index):
        e = index.data(Qt.ItemDataRole.UserRole)
        if e is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(QColor("#4a4a55") if selected else QColor("#25252a"))
        painter.setBrush(QColor("#2a2a31") if selected else QColor("#1d1d21") if hover else QColor("#161619"))
        painter.drawRoundedRect(r, 8, 8)
        head, detail = od.row_texts(e)
        inner = r.adjusted(12, 6, -12, -6)
        f1, f2 = _scaled(option.font, 1.2, bold=True), _scaled(option.font, 1.0)
        fm1, fm2 = QFontMetrics(f1), QFontMetrics(f2)
        painter.setFont(f1)
        painter.setPen(QColor("#f4f4f5"))
        painter.drawText(QRect(inner.left(), inner.top(), inner.width(), fm1.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         fm1.elidedText(head, Qt.TextElideMode.ElideRight, inner.width()))
        painter.setFont(f2)
        painter.setPen(QColor("#9a9aa3"))
        painter.drawText(QRect(inner.left(), inner.top() + fm1.height() + 2, inner.width(), fm2.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         fm2.elidedText(detail, Qt.TextElideMode.ElideRight, inner.width()))
        painter.restore()


def _make_list(name: str) -> tuple:
    view = QListView()
    view.setObjectName(name)
    model = EntryListModel(view)
    view.setModel(model)
    view.setItemDelegate(EntryDelegate(view))
    view.setUniformItemSizes(True)
    view.setMouseTracking(True)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return view, model


# --------------------------------------------------------------------------------------------------------------
# the tab
# --------------------------------------------------------------------------------------------------------------

class DictionaryTab(QWidget):
    def __init__(self, parent=None, loader=od.load_dictionary, language: str = od.DEFAULT_LANGUAGE):
        super().__init__(parent)
        self._loader = loader
        self.lang = od.LANGUAGES[language]
        self.dic = None
        self.state = None
        self.status = None
        self._entry = None
        self._shown_sentences = 5
        self._rendering = False
        self._sized = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(10)

        # SEARCH bar -------------------------------------------------------------------------------------------
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_back = QPushButton("\u2190")
        self.btn_back.setObjectName("navArrow")
        self.btn_back.setToolTip("Back (Alt+Left)")
        self.btn_back.clicked.connect(self.go_back)
        self.btn_fwd = QPushButton("\u2192")
        self.btn_fwd.setObjectName("navArrow")
        self.btn_fwd.setToolTip("Forward (Alt+Right)")
        self.btn_fwd.clicked.connect(self.go_forward)
        self.btn_home = QPushButton("Home")
        self.btn_home.clicked.connect(self.go_home)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(38)
        self.search.returnPressed.connect(self.run_search)
        self.search.textEdited.connect(self._search_edited)
        self.btn_kana = QPushButton("\u3042")
        self.btn_kana.setObjectName("kanaToggle")
        self.btn_kana.setCheckable(True)
        self.btn_kana.setToolTip("Type Japanese without an input method: letters become kana as you type "
                                 "(taberu \u2192 \u305f\u3079\u308b). Turn off to search in English.")
        self.btn_kana.toggled.connect(lambda _on: self._set_placeholder())
        self.btn_search = QPushButton("Search")
        self.btn_search.setObjectName("primary")
        self.btn_search.clicked.connect(self.run_search)
        for w in (self.btn_back, self.btn_fwd, self.btn_home):
            bar.addWidget(w)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.btn_kana)
        bar.addWidget(self.btn_search)
        root.addLayout(bar)

        # VOCAB | GRAMMAR | KANJI -------------------------------------------------------------------------------
        kinds = QHBoxLayout()
        kinds.setSpacing(10)
        self.kind_btns = {}
        for kind in od.KINDS:
            b = QPushButton(od.KIND_LABELS[kind].upper())
            b.setObjectName("kindBtn")
            b.setCheckable(True)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _c=False, k=kind: self.browse(k))
            self.kind_btns[kind] = b
            kinds.addWidget(b, 1)
        root.addLayout(kinds)

        # setup / status banner -------------------------------------------------------------------------------
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QVBoxLayout(self.banner)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(6)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        self.banner_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.banner_cmd = QLabel("")
        self.banner_cmd.setObjectName("cmd")
        self.banner_cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        brow = QHBoxLayout()
        self.btn_copy = QPushButton("Copy command")
        self.btn_copy.clicked.connect(self.copy_command)
        self.btn_reload_banner = QPushButton("I ran it: reload")
        self.btn_reload_banner.clicked.connect(self.reload)
        brow.addWidget(self.btn_copy)
        brow.addWidget(self.btn_reload_banner)
        brow.addStretch(1)
        bl.addWidget(self.banner_text)
        bl.addWidget(self.banner_cmd)
        bl.addLayout(brow)
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        # content ------------------------------------------------------------------------------------------------
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.stack.addWidget(self._build_home())
        self.stack.addWidget(self._build_list_page())
        root.addWidget(self.stack, 1)

        # CHOOSE DICTIONARY --------------------------------------------------------------------------------------
        foot, fl = _card()
        row = QHBoxLayout()
        row.setSpacing(14)
        pick = QVBoxLayout()
        cap = QLabel("CHOOSE DICTIONARY")
        cap.setObjectName("cardTitle")
        self.chooser = QComboBox()
        for spec in od.LANGUAGES.values():
            self.chooser.addItem(spec.name, spec.id)
        self.chooser.setMinimumWidth(150)
        self.chooser.currentIndexChanged.connect(self._language_changed)
        pick.addWidget(cap)
        pick.addWidget(self.chooser)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.info_name = QLabel("")
        self.info_name.setObjectName("dictName")
        self.info_counts = QLabel("")
        self.info_counts.setWordWrap(True)
        self.info_source = _muted("")
        for w in (self.info_name, self.info_counts, self.info_source):
            info.addWidget(w)
        self.btn_reload = QPushButton("Reload")
        self.btn_reload.setToolTip("Look for the dictionary file again (after running ocr_dict.py)")
        self.btn_reload.clicked.connect(self.reload)
        row.addLayout(pick)
        row.addLayout(info, 1)
        row.addWidget(self.btn_reload, 0, Qt.AlignmentFlag.AlignVCenter)
        fl.addLayout(row)
        root.addWidget(foot)

        for seq, fn in (("Alt+Left", self.go_back), ("Alt+Right", self.go_forward)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

        self._open()

    # -- building the pages ------------------------------------------------------------------------------------
    def _build_home(self) -> QWidget:
        card, lay = _card()
        head = QHBoxLayout()
        title = QLabel("RECENTS")
        title.setObjectName("cardTitle")
        self.btn_clear_recents = QPushButton("Clear")
        self.btn_clear_recents.clicked.connect(self.clear_recents)
        head.addWidget(title, 1)
        head.addWidget(self.btn_clear_recents)
        lay.addLayout(head)
        self.recent_view, self.recent_model = _make_list("recentList")
        self.recent_view.clicked.connect(self._recent_clicked)
        lay.addWidget(self.recent_view, 1)
        self.recent_empty = _muted("")
        self.recent_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.recent_empty)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return card

    def _build_list_page(self) -> QWidget:
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        left = QWidget()
        left.setMinimumWidth(320)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.setSpacing(8)
        top = QHBoxLayout()
        self.list_title = QLabel("")
        self.list_title.setObjectName("cardTitle")
        self.list_title.setWordWrap(True)
        self.common_box = QCheckBox("Common only")
        self.common_box.toggled.connect(self._common_toggled)
        self.jlpt_box = QComboBox()
        self.jlpt_box.addItem("All levels", "")
        for lv in od.JLPT_ORDER:
            self.jlpt_box.addItem(lv, lv)
        self.jlpt_box.setToolTip("Show the grammar of one JLPT level")
        self.jlpt_box.currentIndexChanged.connect(self._jlpt_changed)
        top.addWidget(self.list_title, 1)
        top.addWidget(self.jlpt_box)
        top.addWidget(self.common_box)
        ll.addLayout(top)

        self.alpha_bar = QWidget()
        ab = QHBoxLayout(self.alpha_bar)
        ab.setContentsMargins(0, 0, 0, 0)
        ab.setSpacing(3)
        self.alpha_btns = {}
        for key, label in [("", "All")] + [(k, k) for k in (r[0] for r in od.ALPHA_ROWS)] + [(od.OTHER_ROW, "A\u2013Z")]:
            b = QPushButton(label)
            b.setObjectName("alphaBtn")
            b.setCheckable(True)
            b.setToolTip("All entries" if not key else "Not kana (Latin letters, digits, symbols)" if key == od.OTHER_ROW
                         else f"Readings starting with the {key} row")
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _c=False, k=key: self._row_clicked(k))
            self.alpha_btns[key] = b
            ab.addWidget(b, 1)
        ll.addWidget(self.alpha_bar)

        self.empty_label = _muted("")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ll.addWidget(self.empty_label)
        self.list_view, self.list_model = _make_list("entryList")
        self.list_view.selectionModel().currentChanged.connect(self._on_current)
        ll.addWidget(self.list_view, 1)

        pager = QHBoxLayout()
        self.btn_prev = QPushButton("\u2039 Prev")
        self.btn_prev.clicked.connect(lambda: self._render(self.state.prev_page()))
        self.btn_next = QPushButton("Next \u203a")
        self.btn_next.clicked.connect(lambda: self._render(self.state.next_page()))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setMaximum(1)
        self.page_spin.setToolTip("Go to page")
        self.page_spin.editingFinished.connect(self._spin_done)
        self.page_total = QLabel("/ 1")
        pager.addWidget(self.btn_prev)
        pager.addStretch(1)
        pager.addWidget(QLabel("Page"))
        pager.addWidget(self.page_spin)
        pager.addWidget(self.page_total)
        pager.addStretch(1)
        pager.addWidget(self.btn_next)
        ll.addLayout(pager)

        right_card, rl = _card()
        right_card.setMinimumWidth(340)
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 6, 0)
        bl.setSpacing(10)
        self.detail_head = QLabel("")
        self.detail_head.setTextFormat(Qt.TextFormat.RichText)
        self.detail_head.setWordWrap(True)
        self.detail_head.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detail_head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.detail_head)
        self.notes = {}
        for slot in od.NOTE_SLOTS:
            ed = QLineEdit()
            ed.setPlaceholderText("Add your own note (saved automatically)")
            ed.editingFinished.connect(lambda s=slot: self._save_note(s))
            self.notes[slot] = ed
            bl.addWidget(ed)
        sent_head = QHBoxLayout()
        sent_title = QLabel("EXAMPLE SENTENCES")
        sent_title.setObjectName("cardTitle")
        self.sent_count = _muted("")
        self.sent_count.setWordWrap(False)
        sent_head.addWidget(sent_title)
        sent_head.addStretch(1)
        sent_head.addWidget(self.sent_count)
        bl.addLayout(sent_head)
        self.sent_body = QLabel("")
        self.sent_body.setTextFormat(Qt.TextFormat.RichText)
        self.sent_body.setWordWrap(True)
        self.sent_body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.sent_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.sent_body)
        self.btn_more = QPushButton("Show more")
        self.btn_more.clicked.connect(self._more_sentences)
        bl.addWidget(self.btn_more, 0, Qt.AlignmentFlag.AlignLeft)
        bl.addStretch(1)
        self.detail_scroll.setWidget(body)
        rl.addWidget(self.detail_scroll, 1)

        self.splitter.addWidget(left)
        self.splitter.addWidget(right_card)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 5)
        return self.splitter

    def showEvent(self, event):
        super().showEvent(event)
        if not self._sized and self.splitter.width() > 0:      # start at 45 / 55, then the user's drag is kept
            w = self.splitter.width()
            self.splitter.setSizes([int(w * 0.45), w - int(w * 0.45)])
            self._sized = True

    # -- opening the dictionary ---------------------------------------------------------------------------------
    def _open(self):
        if self.dic is not None:
            self._save_notes()
            self.dic.close()
            self.dic = None
        try:
            self.dic, self.status = self._loader(self.lang)
        except Exception as exc:   # noqa: BLE001  the tab must still open and say what is wrong
            self.dic, self.status = None, None
            self.banner_text.setText(f"The dictionary could not be opened: {exc}")
            self.banner_cmd.setText("")
            self.banner.setProperty("level", "error")
            self.banner.setVisible(True)
            for w in (self.btn_back, self.btn_fwd, self.btn_home, self.search, self.btn_kana, self.btn_search, *self.kind_btns.values()):
                w.setEnabled(False)
            return
        for w in (self.btn_home, self.search, self.btn_kana, self.btn_search, *self.kind_btns.values()):
            w.setEnabled(True)
        self.state = od.BrowserState(self.dic)
        self._entry = None
        self._update_status()
        self._render(self.state.view())

    def _update_status(self):
        s = od.status_summary(self.status, self.dic)
        self.info_name.setText(s["name"])
        self.info_counts.setText(s["counts"])
        self.info_source.setText(s["source"])
        self.banner_text.setText(s["banner"])
        self.banner_cmd.setText(s["command"])
        self.banner_cmd.setVisible(bool(s["command"]))
        self.btn_copy.setVisible(bool(s["command"]))
        self.btn_reload_banner.setVisible(bool(s["command"]))
        self.banner.setProperty("level", s["level"])
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setVisible(bool(s["banner"]))

    def reload(self):
        self._open()

    def _language_changed(self, _index: int):
        lang = od.LANGUAGES.get(self.chooser.currentData())
        if lang is not None and lang.id != self.lang.id:
            self.lang = lang
            self._open()

    def copy_command(self):
        QGuiApplication.clipboard().setText(self.banner_cmd.text())

    # -- navigation ---------------------------------------------------------------------------------------------
    def go_home(self):
        if self.state:
            self._render(self.state.home())

    def go_back(self):
        if self.state and self.state.can_back:
            self._render(self.state.back())

    def go_forward(self):
        if self.state and self.state.can_forward:
            self._render(self.state.forward())

    def browse(self, kind: str):
        if self.state:
            self._render(self.state.browse(kind))

    def _search_edited(self, text: str):
        """With the \u3042 switch on, romaji typed into the box turns into kana (textEdited is not emitted by setText)."""
        if not self.btn_kana.isChecked():
            return
        kana = romaji_to_kana(text)
        if kana != text:
            from_end = len(text) - self.search.cursorPosition()
            self.search.setText(kana)
            self.search.setCursorPosition(max(0, len(kana) - from_end))

    def _set_placeholder(self):
        scope = self.state.scope if self.state else None
        if self.btn_kana.isChecked():
            self.search.setPlaceholderText("Type romaji, it becomes kana (taberu \u2192 \u305f\u3079\u308b), then press Enter")
        else:
            what = od.KIND_LABELS[scope].lower() if scope else "vocab, grammar and kanji"
            self.search.setPlaceholderText(f"Search {what}: word, reading or English   (\u3042 = type romaji)")

    def run_search(self):
        if self.state and self.search.text().strip():
            if self.btn_kana.isChecked():                       # a last "n" becomes \u3093 now that typing is finished
                self.search.setText(romaji_to_kana(self.search.text(), final=True))
            self._render(self.state.search(self.search.text()))

    def lookup(self, text: str):
        """Entry point for the selection popup (Lookup)."""
        if self.state:
            self._render(self.state.lookup(text))

    def clear_recents(self):
        if self.dic:
            self.dic.clear_recents()
            self._render(self.state.view())

    def _recent_clicked(self, index):
        e = self.recent_model.entry_at(index.row())
        if e is not None and self.state:
            self._render(self.state.open_recent(e))

    def _row_clicked(self, key: str):
        p = self.state.page
        if p.mode == "browse":
            self._render(self.state.browse(p.kind, key, p.common, p.jlpt))

    def _common_toggled(self, on: bool):
        if self._rendering:
            return
        p = self.state.page
        if p.mode == "browse":
            self._render(self.state.browse(p.kind, p.row, on, p.jlpt))

    def _jlpt_changed(self, _index: int):
        if self._rendering:
            return
        p = self.state.page
        if p.mode == "browse" and p.kind == "grammar":
            self._render(self.state.browse(p.kind, p.row, p.common, self.jlpt_box.currentData() or ""))

    def _spin_done(self):
        if self._rendering or self.page_spin.value() - 1 == self.state.page.number:
            return            # editingFinished also fires when the box merely loses focus
        self._render(self.state.goto(self.page_spin.value() - 1))

    # -- drawing a View -----------------------------------------------------------------------------------------
    def _render(self, v: "od.View"):
        self._rendering = True
        try:
            p = v.page
            self.btn_back.setEnabled(self.state.can_back)
            self.btn_fwd.setEnabled(self.state.can_forward)
            for kind, b in self.kind_btns.items():
                b.setChecked(p.mode == "browse" and p.kind == kind)
            self._set_placeholder()
            if v.is_home:
                self.recent_model.set_entries(v.recents)
                self.recent_empty.setText("" if v.recents else v.message)
                self.recent_empty.setVisible(not v.recents)
                self.btn_clear_recents.setEnabled(bool(v.recents))
                self.stack.setCurrentIndex(PAGE_HOME)
                return
            browsing = p.mode == "browse"
            self.list_title.setText(f"{v.title}   \u00b7   {v.total:,} {'entry' if v.total == 1 else 'entries'}")
            self.alpha_bar.setVisible(browsing)
            self.common_box.setVisible(browsing and p.kind == "vocab")
            self.common_box.setChecked(bool(p.common))
            self.jlpt_box.setVisible(browsing and p.kind == "grammar")
            self.jlpt_box.setCurrentIndex(max(0, self.jlpt_box.findData(p.jlpt or "")))
            for key, b in self.alpha_btns.items():
                b.setChecked(browsing and key == p.row)
            self.list_model.set_entries(v.entries)
            self.empty_label.setText(v.message)
            self.empty_label.setVisible(bool(v.message))
            idx = v.selected_index
            if idx >= 0:
                self.list_view.setCurrentIndex(self.list_model.index(idx))
            self.btn_prev.setEnabled(p.number > 0)
            self.btn_next.setEnabled(p.number < v.pages - 1)
            self.page_spin.setMaximum(max(1, v.pages))
            self.page_spin.setValue(p.number + 1)
            self.page_total.setText(f"/ {v.pages:,}")
            self.stack.setCurrentIndex(PAGE_LIST)
            self._show_entry(v.selected)
        finally:
            self._rendering = False

    def _on_current(self, current, _previous):
        if self._rendering or not current.isValid():
            return
        e = self.list_model.entry_at(current.row())
        if e is not None:
            self.state.select(e.id)
            self._show_entry(e)

    # -- the detail panel ---------------------------------------------------------------------------------------
    def _show_entry(self, e):
        self._save_notes()
        self._entry = e
        self._shown_sentences = 5
        if e is None:
            self.detail_head.setText("")
            self.sent_body.setText("")
            self.sent_count.setText("")
            self.btn_more.setVisible(False)
            for ed in self.notes.values():
                ed.setText("")
                ed.setEnabled(False)
            return
        self.detail_head.setText(od.entry_html(e))
        saved = self.dic.get_notes(e)
        for slot, ed in self.notes.items():
            ed.setEnabled(True)
            ed.setText(saved.get(slot, ""))
        self._load_sentences()
        self.detail_scroll.verticalScrollBar().setValue(0)

    def _load_sentences(self):
        e = self._entry
        if e is None:
            return
        res = self.dic.sentences_for(e, limit=self._shown_sentences)
        self.sent_body.setText(od.sentences_html(res))
        self.sent_count.setText(od.sentence_count_text(res))
        remaining = res.total - len(res.items)
        self.btn_more.setVisible(remaining > 0)
        self.btn_more.setText(f"Show more ({remaining}{'+' if res.capped else ''} left)")

    def _more_sentences(self):
        self._shown_sentences += 10
        self._load_sentences()

    def _save_note(self, slot: int):
        if self._entry is not None and self.dic is not None and self.notes[slot].isEnabled():
            self.dic.set_note(self._entry, slot, self.notes[slot].text())

    def _save_notes(self):
        for slot in od.NOTE_SLOTS:
            self._save_note(slot)

    def shutdown(self):
        if self.dic is not None:
            self._save_notes()
            self.dic.close()
            self.dic = None


# --------------------------------------------------------------------------------------------------------------
# looking up text selected in the Capture output or the table preview
# --------------------------------------------------------------------------------------------------------------

SELECTION_QSS = """
QFrame#selPopup { background: #1d1d21; border: 1px solid #3c3c44; border-radius: 10px; }
QFrame#selPopup QPushButton { padding: 6px 12px; }
"""


class SelectionPopup(QFrame):
    lookup_requested = Signal(str)
    flashcard_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("selPopup")
        self._text = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
        self.btn_card = QPushButton("Make flashcard")
        self.btn_look = QPushButton("Lookup")
        self.btn_look.setObjectName("primary")
        self.btn_card.clicked.connect(self._card)
        self.btn_look.clicked.connect(self._look)
        lay.addWidget(self.btn_card)
        lay.addWidget(self.btn_look)

    def _card(self):
        text = self._text
        self.hide()
        self.flashcard_requested.emit(text)

    def _look(self):
        text = self._text
        self.hide()
        self.lookup_requested.emit(text)

    def show_for(self, text: str, global_pos: QPoint):
        """Show the popup just below global_pos, moved back inside the screen when it would stick out."""
        self._text = text
        self.adjustSize()
        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        x, y = global_pos.x(), global_pos.y() + 14
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(geo.left(), min(x, geo.right() - self.width()))
            if y + self.height() > geo.bottom():
                y = global_pos.y() - self.height() - 10
            y = max(geo.top(), y)
        self.move(x, y)
        self.show()


class SelectionWatcher(QObject):
    """Shows the popup after a mouse selection (also a double click on a word) or a Shift+arrow selection."""

    def __init__(self, edit: QPlainTextEdit, on_lookup, on_flashcard):
        super().__init__(edit)
        self.edit, self._on_lookup, self._on_flashcard = edit, on_lookup, on_flashcard
        self._popup = None
        edit.viewport().installEventFilter(self)
        edit.installEventFilter(self)

    def _ensure_popup(self) -> SelectionPopup:
        # created on first use so that edit.window() is the real main window (a Wayland popup needs a parent)
        if self._popup is None:
            self._popup = SelectionPopup(self.edit.window())
            self._popup.lookup_requested.connect(self._on_lookup)
            self._popup.flashcard_requested.connect(self._on_flashcard)
        return self._popup

    def _maybe_show(self, global_pos: QPoint):
        cur = self.edit.textCursor()
        if not cur.hasSelection():
            return
        text = od.clean_selection(cur.selectedText())
        if text:
            self._ensure_popup().show_for(text, global_pos)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if (obj is self.edit.viewport() and t == QEvent.Type.MouseButtonRelease
                and ev.button() == Qt.MouseButton.LeftButton):
            pos = ev.globalPosition().toPoint()
            QTimer.singleShot(0, lambda: self._maybe_show(pos))   # after the selection has been updated
        elif (obj is self.edit and t == QEvent.Type.KeyRelease
              and ev.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                               Qt.Key.Key_Home, Qt.Key.Key_End)
              and ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            pos = self.edit.viewport().mapToGlobal(self.edit.cursorRect().bottomRight())
            QTimer.singleShot(0, lambda: self._maybe_show(pos))
        return False


def attach_selection_popup(edit: QPlainTextEdit, on_lookup, on_flashcard) -> SelectionWatcher:
    """on_lookup(text) and on_flashcard(text) are called with the cleaned selection."""
    return SelectionWatcher(edit, on_lookup, on_flashcard)
