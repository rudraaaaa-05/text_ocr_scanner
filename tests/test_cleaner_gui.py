"""Wiring tests for the Table Cleaner tab in ocr_scanner.py.

PySide6 may not be installed (CI, headless boxes), and a real window needs a display, so these tests load the real
ocr_scanner.py against a small STATEFUL fake of the Qt widgets the tab uses. They run the real page-building code and
the real click handlers, including the worker thread. They prove the wiring (tab order, checkbox / dropdown
behaviour, button states, status text, preview, the file that gets written). They do NOT prove how it looks:
check that by launching the app.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OCR_SCANNER_KEYRING", "off")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import ocr_cleaner  # noqa: E402,F401  (imported BEFORE the fake Qt is installed, so the GUI and the tests share one
import ocr_core as core  # noqa: E402     copy: modules first imported inside patch.dict are dropped when it exits)
import ocr_nlp  # noqa: E402,F401


# ---- a tiny fake Qt ------------------------------------------------------------------------------------------

class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for fn in list(self.slots):
            fn(*a)


class _SignalDescriptor:
    def __init__(self, *types_):
        pass

    def __set_name__(self, owner, name):
        self._n = "_sig_" + name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.__dict__.setdefault(self._n, _Sig())


class _Meta(type):
    """Class-level lookups such as QPlainTextEdit.LineWrapMode.NoWrap (enums) also resolve to harmless mocks."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return mock.MagicMock()


class W(metaclass=_Meta):
    """Generic widget: anything not modelled is a harmless no-op."""

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return mock.MagicMock()


class WithState(W):
    def __init__(self, *a, **k):
        self._enabled, self._visible, self._style, self._tip = True, True, "", ""

    def setEnabled(self, v): self._enabled = bool(v)
    def isEnabled(self): return self._enabled
    def setVisible(self, v): self._visible = bool(v)
    def isVisible(self): return self._visible
    def setStyleSheet(self, s): self._style = s


class QLabel(WithState):
    def __init__(self, text=""):
        super().__init__()
        self._t = text

    def setText(self, t): self._t = t
    def text(self): return self._t


class QLineEdit(WithState):
    def __init__(self, text=""):
        super().__init__()
        self._t = text
        self.editingFinished, self.textChanged = _Sig(), _Sig()

    def setText(self, t): self._t = t
    def text(self): return self._t


class QPlainTextEdit(WithState):
    def __init__(self, *a):
        super().__init__()
        self._t = ""

    def setPlainText(self, t): self._t = t
    def toPlainText(self): return self._t


class QPushButton(WithState):
    def __init__(self, text=""):
        super().__init__()
        self._t, self._checked = text, False
        self.clicked = _Sig()

    def text(self): return self._t
    def setChecked(self, v): self._checked = bool(v)
    def isChecked(self): return self._checked


class QCheckBox(WithState):
    def __init__(self, text=""):
        super().__init__()
        self._t, self._checked = text, False
        self.toggled = _Sig()

    def setChecked(self, v):
        v = bool(v)
        changed = v != self._checked
        self._checked = v
        if changed:
            self.toggled.emit(v)

    def isChecked(self): return self._checked


class QComboBox(WithState):
    def __init__(self, *a):
        super().__init__()
        self._items, self._idx, self._blocked = [], -1, False
        self.currentIndexChanged, self.currentTextChanged = _Sig(), _Sig()

    def blockSignals(self, b): self._blocked = bool(b)

    def addItem(self, text, data=None):
        self._items.append((text, data))
        if self._idx < 0:
            self._idx = 0

    def addItems(self, texts):
        for t in texts:
            self.addItem(t)

    def clear(self): self._items, self._idx = [], -1
    def count(self): return len(self._items)
    def itemTexts(self): return [t for t, _ in self._items]

    def findData(self, data):
        for i, (_, d) in enumerate(self._items):
            if d == data:
                return i
        return -1

    def setCurrentIndex(self, i):
        changed = i != self._idx
        self._idx = i
        if changed and not self._blocked:
            self.currentIndexChanged.emit(i)

    def currentIndex(self): return self._idx
    def currentData(self): return self._items[self._idx][1] if 0 <= self._idx < len(self._items) else None
    def currentText(self): return self._items[self._idx][0] if 0 <= self._idx < len(self._items) else ""

    def setCurrentText(self, t):
        for i, (txt, _) in enumerate(self._items):
            if txt == t:
                self._idx = i
                break
        else:
            self._items.append((t, None))
            self._idx = len(self._items) - 1
        if not self._blocked:
            self.currentTextChanged.emit(t)

    def select(self, text):
        """What the user does: pick an entry from the list."""
        self.setCurrentText(text)


class QStackedWidget(WithState):
    def __init__(self):
        super().__init__()
        self.pages, self.current = [], -1

    def addWidget(self, w): self.pages.append(w)
    def setCurrentIndex(self, i): self.current = i


class QWidget(WithState):
    pass


class QObject(W):
    pass


def _fake_modules():
    names = {
        "QtCore": dict(QObject=QObject, Signal=_SignalDescriptor, Qt=mock.MagicMock(), QUrl=mock.MagicMock(),
                       QTimer=mock.MagicMock(), QPoint=W, QRect=W, QRectF=W, QSize=W),
        "QtGui": {},
        "QtWidgets": dict(QWidget=QWidget, QLabel=QLabel, QLineEdit=QLineEdit, QPlainTextEdit=QPlainTextEdit, QPushButton=QPushButton,
                          QCheckBox=QCheckBox, QComboBox=QComboBox, QStackedWidget=QStackedWidget),
    }
    mods = {}
    root = types.ModuleType("PySide6")
    mods["PySide6"] = root

    class Mod(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            cls = type(name, (W,), {})
            setattr(self, name, cls)
            return cls
    for short, attrs in names.items():
        m = Mod(f"PySide6.{short}")
        for k, v in attrs.items():
            setattr(m, k, v)
        mods[f"PySide6.{short}"] = m
        setattr(root, short, m)
    return mods


def load_gui():
    """The real ocr_scanner.py, imported under another name with the fake Qt in place only while it loads."""
    with mock.patch.dict(sys.modules, _fake_modules()):
        spec = importlib.util.spec_from_file_location("ocr_scanner_under_test", ROOT / "ocr_scanner.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


GUI = load_gui()


def make_window(cfg_over=None, session_file=None):
    d = core.default_config()
    d.update(cfg_over or {})
    win = GUI.MainWindow.__new__(GUI.MainWindow)
    win.d = d
    win.bus = GUI.Bus()
    win.session_file = session_file
    win._cleaning = False
    win._clean_cancel = threading.Event()
    win._clean_path = None
    win.log_view = mock.MagicMock()
    win.save_soon = mock.MagicMock()        # nothing here touches the real config file
    win.open_url = mock.MagicMock()
    win._saved = []
    win.cleaned = threading.Event()
    win.bus.clean_progress.connect(win._on_clean_progress)
    win.bus.clean_done.connect(win._on_clean_done)
    win.bus.clean_done.connect(lambda *_: win.cleaned.set())
    win.stack = QStackedWidget()
    win.nav_btns = [QPushButton(n) for n in GUI.PAGE_NAMES]
    win.page = win._build_cleaner_page()
    return win


def run_and_wait(win, timeout=10):
    win.cleaned.clear()
    win.run_cleaner()
    assert win.cleaned.wait(timeout), "the cleaner never reported back (the button would stay disabled)"


def rows_of(markdown):
    import re
    out = []
    for ln in markdown.splitlines():
        if ln.startswith("|"):
            out.append([x.strip() for x in re.split(r"(?<!\\)\|", ln.strip())[1:-1]])
    return [out[0]] + out[2:]


# ---- tests ---------------------------------------------------------------------------------------------------

class TabLayoutTests(unittest.TestCase):
    def test_tab_order_and_names(self):
        self.assertEqual(GUI.PAGE_NAMES, ("Capture", "OCR Mode", "Table Cleaner", "Settings"))
        self.assertEqual((GUI.PAGE_CAPTURE, GUI.PAGE_OCR_MODE, GUI.PAGE_CLEANER, GUI.PAGE_SETTINGS), (0, 1, 2, 3))

    def test_build_ui_creates_one_page_per_tab_in_the_same_order(self):
        win = GUI.MainWindow.__new__(GUI.MainWindow)
        win.d = core.default_config()
        order = []
        for name in ("capture", "keys", "cleaner", "settings"):
            setattr(win, f"_build_{name}_page", (lambda n=name: order.append(n) or n))
        win._clean_cancel = threading.Event()
        win.btn_daemon = mock.MagicMock()
        win._refresh_clean_providers = mock.MagicMock()
        win._build_ui()
        self.assertEqual(order, ["capture", "keys", "cleaner", "settings"])
        self.assertEqual(win.stack.pages, ["capture", "keys", "cleaner", "settings"])
        self.assertEqual([b.text() for b in win.nav_btns], list(GUI.PAGE_NAMES))
        self.assertEqual(win.stack.current, 0)
        self.assertTrue(win.nav_btns[0].isChecked())

    def test_clicking_each_tab_shows_its_page(self):
        win = GUI.MainWindow.__new__(GUI.MainWindow)
        win.stack = QStackedWidget()
        win.nav_btns = [QPushButton(n) for n in GUI.PAGE_NAMES]
        win._refresh_clean_providers = mock.MagicMock()
        for i in range(4):
            win.show_page(i)
            self.assertEqual(win.stack.current, i)
            self.assertTrue(win.nav_btns[i].isChecked())
        win._refresh_clean_providers.assert_called_once()          # only when the Cleaner tab opens


class CleanerPageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def raw(self, text="[2026-11-21 10:00:03] 猫\n[2026-11-21 10:00:06] 猫\n[2026-11-21 10:00:09] 犬\n", name="out.txt"):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_controls_and_defaults(self):
        win = make_window()
        self.assertEqual(win.clean_lang.itemTexts(), core.LANGUAGES)
        self.assertIn("Auto-detect", win.clean_lang.itemTexts())            # auto is an option
        self.assertEqual(win.clean_lang.currentText(), "Auto-detect")
        self.assertFalse(win.clean_translate.isChecked())
        self.assertFalse(win.clean_provider_row.isVisible())                # hidden until Translate is ticked
        self.assertEqual(win.clean_provider.itemTexts()[0].split(" ")[0], "Automatic")
        self.assertTrue(win.btn_clean.isEnabled())
        self.assertFalse(win.btn_clean_cancel.isEnabled())
        self.assertFalse(win.btn_clean_open.isEnabled())

    def test_saved_settings_are_restored(self):
        win = make_window({"cleaner_language": "Japanese", "cleaner_translate": True, "cleaner_provider": "groq"})
        self.assertEqual(win.clean_lang.currentText(), "Japanese")
        self.assertTrue(win.clean_translate.isChecked())
        self.assertTrue(win.clean_provider_row.isVisible())

    def test_ticking_translate_shows_provider_row_and_saves_the_choice(self):
        win = make_window()
        win.clean_translate.setChecked(True)
        self.assertTrue(win.clean_provider_row.isVisible())
        self.assertTrue(win.clean_translate_note.isVisible())
        self.assertIs(win.d["cleaner_translate"], True)
        win.save_soon.assert_called()
        win.clean_translate.setChecked(False)
        self.assertFalse(win.clean_provider_row.isVisible())
        self.assertIs(win.d["cleaner_translate"], False)

    def test_language_dropdown_is_saved(self):
        win = make_window()
        win.clean_lang.select("German")
        self.assertEqual(win.d["cleaner_language"], "German")

    def test_english_language_explains_why_there_is_no_translation(self):
        win = make_window()
        win.clean_translate.setChecked(True)
        win.clean_lang.select("English")
        self.assertFalse(win.clean_provider_row.isVisible())
        self.assertIn("no English column", win.clean_translate_note.text())
        win.clean_lang.select("Japanese")
        self.assertTrue(win.clean_provider_row.isVisible())
        self.assertIn("AI provider", win.clean_translate_note.text())

    def test_provider_dropdown_lists_only_ready_providers_and_remembers_the_pick(self):
        win = make_window()
        base = win.clean_provider.count()                                    # Automatic + keyless ones (Ollama)
        self.assertGreaterEqual(base, 1)
        self.assertNotIn("groq", [win.clean_provider._items[i][1] for i in range(base)])   # no key yet
        win.d["providers"]["groq"]["keys"] = "gsk_abc"
        win._refresh_clean_providers()                                      # what opening the tab does
        self.assertEqual(win.clean_provider.count(), base + 1)
        win.clean_provider.setCurrentIndex(win.clean_provider.findData("groq"))
        self.assertEqual(win.d["cleaner_provider"], "groq")
        win._refresh_clean_providers()
        self.assertEqual(win.clean_provider.currentData(), "groq")          # selection survives the refresh

    def test_refresh_does_not_overwrite_a_saved_provider_that_is_temporarily_unavailable(self):
        win = make_window({"cleaner_provider": "groq"})                     # no groq key right now
        self.assertEqual(win.clean_provider.currentData(), "auto")
        self.assertEqual(win.d["cleaner_provider"], "groq")                 # not clobbered by the refresh

    # -- running it

    def test_clean_without_translate_end_to_end(self):
        src = self.raw()
        win = make_window()
        win.clean_src.setText(str(src))
        run_and_wait(win)
        out = self.dir / "out_table.md"
        self.assertTrue(out.exists())
        self.assertEqual(rows_of(out.read_text(encoding="utf-8")),
                         [["#", "Time", "Sentence"], ["1", "2026-11-21 10:00:03", "猫"], ["2", "2026-11-21 10:00:09", "犬"]])
        self.assertTrue(win.btn_clean.isEnabled())
        self.assertFalse(win.btn_clean_cancel.isEnabled())
        self.assertTrue(win.btn_clean_open.isEnabled() and win.btn_clean_folder.isEnabled())
        self.assertIn(str(out), win.clean_status.text())
        self.assertIn("2 sentences from 3 lines", win.clean_status.text())
        self.assertIn("#7ee2a8", win.clean_status._style)                                  # green = success
        self.assertIn("| 猫", win.clean_preview.toPlainText())
        self.assertEqual(src.read_text(encoding="utf-8").count("\n"), 3)                     # source untouched

    def test_empty_source_field_uses_the_session_file(self):
        src = self.raw(name="ocr_output_2026-11-21_10-00-00.txt")
        win = make_window(session_file=src)
        run_and_wait(win)
        self.assertTrue((self.dir / "ocr_output_2026-11-21_10-00-00_table.md").exists())

    def test_use_session_button(self):
        src = self.raw()
        win = make_window(session_file=src)
        win.use_session_for_cleaner()
        self.assertEqual(win.clean_src.text(), str(src))
        win2 = make_window(session_file=None)
        win2.use_session_for_cleaner()
        self.assertEqual(win2.clean_src.text(), "")
        self.assertIn("no session file", win2.clean_status.text())

    def test_nothing_chosen_is_an_error_not_a_crash(self):
        win = make_window()
        win.run_cleaner()
        self.assertIn("Choose a file", win.clean_status.text())
        self.assertFalse(win._cleaning)
        self.assertTrue(win.btn_clean.isEnabled())

    def test_bad_file_reports_error_and_unlocks_the_button(self):
        win = make_window()
        win.clean_src.setText(str(self.dir / "nope.txt"))
        run_and_wait(win)
        self.assertIn("does not exist", win.clean_status.text())
        self.assertIn("#ff6b70", win.clean_status._style)                    # red = error
        self.assertTrue(win.btn_clean.isEnabled())
        self.assertFalse(win.btn_clean_open.isEnabled())
        self.assertEqual(list(self.dir.glob("*_table*")), [])

    def test_translate_without_any_provider_says_what_to_do_and_writes_nothing(self):
        win = make_window()
        win.clean_src.setText(str(self.raw()))
        win.clean_translate.setChecked(True)
        run_and_wait(win)
        self.assertIn("Local OCR can't translate", win.clean_status.text())
        self.assertTrue(win.btn_clean.isEnabled())
        self.assertEqual(list(self.dir.glob("*_table*")), [])

    def test_translate_end_to_end_through_the_http_layer(self):
        def fake_post(url, headers=None, json=None, timeout=None):
            items = __import__("json").loads(json["contents"][0]["parts"][0]["text"].split("\n\n")[-1])
            reply = __import__("json").dumps({k: {"猫": "cat", "犬": "dog"}[v] for k, v in items.items()})
            r = mock.Mock(status_code=200)
            r.json.return_value = {"candidates": [{"content": {"parts": [{"text": reply}]}}]}
            return r
        win = make_window()
        win.d["providers"]["gemini"]["keys"] = "AIza-test"
        win.clean_src.setText(str(self.raw()))
        win.clean_lang.select("Japanese")
        win.clean_translate.setChecked(True)
        with mock.patch.object(core.requests, "post", side_effect=fake_post):
            run_and_wait(win)
        md = (self.dir / "out_table.md").read_text(encoding="utf-8")
        self.assertEqual(rows_of(md), [["#", "Time", "Sentence", "English"],
                                       ["1", "2026-11-21 10:00:03", "猫", "cat"],
                                       ["2", "2026-11-21 10:00:09", "犬", "dog"]])
        self.assertIn("Language: Japanese", md)
        self.assertIn("2 translated", win.clean_status.text())

    def test_worker_gets_its_own_copy_of_the_settings(self):
        seen = {}

        def spy(src, **kw):
            seen["cfg"] = kw["cfg"]
            raise GUI.CleanerError("stop here")
        win = make_window()
        win.clean_src.setText(str(self.raw()))
        with mock.patch.object(GUI, "clean_file", side_effect=spy):
            run_and_wait(win)
        self.assertIsNot(seen["cfg"], win.d)
        self.assertEqual(seen["cfg"], win.d)

    def test_unexpected_exception_in_worker_still_unlocks_the_ui(self):
        win = make_window()
        win.clean_src.setText(str(self.raw()))
        with mock.patch.object(GUI, "clean_file", side_effect=RuntimeError("boom")):
            run_and_wait(win)
        self.assertIn("Unexpected error: RuntimeError: boom", win.clean_status.text())
        self.assertTrue(win.btn_clean.isEnabled())

    def test_cancel(self):
        started, release = threading.Event(), threading.Event()

        def slow(src, **kw):
            started.set()
            release.wait(5)
            if kw["cancel"]():
                raise GUI.CleanerCancelled()
        win = make_window()
        win.clean_src.setText(str(self.raw()))
        with mock.patch.object(GUI, "clean_file", side_effect=slow):
            win.cleaned.clear()
            win.run_cleaner()
            self.assertTrue(started.wait(5))
            self.assertFalse(win.btn_clean.isEnabled())                      # no double start
            self.assertTrue(win.btn_clean_cancel.isEnabled())
            win.run_cleaner()                                                # a second click is ignored
            win.cancel_cleaner()
            release.set()
            self.assertTrue(win.cleaned.wait(5))
        self.assertIn("Cancelled. Nothing was saved.", win.clean_status.text())
        self.assertTrue(win.btn_clean.isEnabled())
        self.assertFalse(win.btn_clean_cancel.isEnabled())

    def test_progress_text(self):
        win = make_window()
        win._cleaning = True
        win._on_clean_progress(40, 120, "Translating")
        self.assertEqual(win.clean_status.text(), "Translating... 40 / 120")
        win._on_clean_progress(0, 1, "Reading the file")
        self.assertEqual(win.clean_status.text(), "Reading the file...")
        win._cleaning = False
        win._on_clean_progress(5, 9, "Translating")                            # late signal after finishing: ignored
        self.assertEqual(win.clean_status.text(), "Reading the file...")

    def test_open_buttons(self):
        win = make_window()
        win.clean_src.setText(str(self.raw()))
        run_and_wait(win)
        win.open_clean_file()
        win.open_clean_folder()
        self.assertEqual(win.open_url.call_count, 2)
        (self.dir / "out_table.md").unlink()
        win.open_url.reset_mock()
        win.open_clean_file()
        win.open_url.assert_not_called()
        self.assertIn("no longer exists", win.clean_status.text())

    def test_long_result_preview_is_trimmed_but_file_is_complete(self):
        src = self.raw("\n".join(f"entry {i}" for i in range(500)))
        win = make_window()
        win.clean_src.setText(str(src))
        run_and_wait(win)
        preview = win.clean_preview.toPlainText()
        self.assertIn("more lines in the file", preview)
        self.assertEqual(len(rows_of((self.dir / "out_table.md").read_text(encoding="utf-8"))), 501)

    def test_closing_the_window_cancels_a_running_job(self):
        win = make_window()
        win._closing = False
        win._save_timer = mock.MagicMock()
        win.cfg = mock.MagicMock()
        win.cfg.save.return_value = None
        win.link = None
        win._cancel_selection = mock.MagicMock()
        win.shutdown()
        self.assertTrue(win._clean_cancel.is_set())


class OrganiseOptionTests(unittest.TestCase):
    """The sentence / word-by-word and furigana controls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        import ocr_cleaner
        import ocr_nlp
        ocr_nlp._cache["tokenizer"] = None
        for target in (ocr_cleaner, ocr_nlp):               # same result whether or not SudachiPy is installed here
            p = mock.patch.object(target, "optional_tokenizer", return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def raw(self, text, name="out.txt"):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_defaults_are_sentence_mode_with_furigana_ignored(self):
        win = make_window()
        self.assertFalse(win.clean_vocab.isChecked())               # sentence by sentence is the default
        self.assertTrue(win.clean_furigana.isChecked())
        self.assertFalse(win.clean_vocab_note.isVisible())
        self.assertEqual(win.d["cleaner_vocab"], False)
        self.assertEqual(win.d["cleaner_furigana"], True)

    def test_saved_choices_are_restored(self):
        win = make_window({"cleaner_vocab": True, "cleaner_furigana": False})
        self.assertTrue(win.clean_vocab.isChecked())
        self.assertFalse(win.clean_furigana.isChecked())

    def test_toggles_are_saved(self):
        win = make_window()
        win.clean_vocab.setChecked(True)
        win.clean_furigana.setChecked(False)
        self.assertIs(win.d["cleaner_vocab"], True)
        self.assertIs(win.d["cleaner_furigana"], False)
        win.save_soon.assert_called()

    def test_furigana_option_only_shows_for_japanese_or_auto(self):
        win = make_window()
        self.assertTrue(win.clean_furigana.isVisible())             # Auto-detect
        win.clean_lang.select("German")
        self.assertFalse(win.clean_furigana.isVisible())
        win.clean_lang.select("Japanese")
        self.assertTrue(win.clean_furigana.isVisible())

    def test_install_hint_appears_only_when_it_is_needed(self):
        hint = "needs SudachiPy: pip install sudachipy sudachidict_core"
        with mock.patch.object(GUI, "tokenizer_problem", return_value=hint):
            win = make_window()
            self.assertFalse(win.clean_vocab_note.isVisible())      # sentence mode needs nothing
            win.clean_vocab.setChecked(True)
            self.assertTrue(win.clean_vocab_note.isVisible())
            self.assertEqual(win.clean_vocab_note.text(), hint)
            win.clean_lang.select("German")                         # spaced languages need no splitter
            self.assertFalse(win.clean_vocab_note.isVisible())
            win.clean_lang.select("Japanese")
            self.assertTrue(win.clean_vocab_note.isVisible())
        with mock.patch.object(GUI, "tokenizer_problem", return_value=None):
            win = make_window({"cleaner_vocab": True})
            self.assertFalse(win.clean_vocab_note.isVisible())      # installed: no hint

    def test_the_options_reach_the_cleaner(self):
        seen = {}

        def spy(src, **kw):
            seen.update(kw)
            raise GUI.CleanerError("stop here")
        win = make_window()
        win.clean_src.setText(str(self.raw("あ\n")))
        with mock.patch.object(GUI, "clean_file", side_effect=spy):
            run_and_wait(win)
            self.assertEqual((seen["mode"], seen["ignore_furigana"]), ("sentence", True))
            win.clean_vocab.setChecked(True)
            win.clean_furigana.setChecked(False)
            run_and_wait(win)
        self.assertEqual((seen["mode"], seen["ignore_furigana"]), ("vocab", False))

    def test_the_users_real_file_through_the_whole_gui_path(self):
        sample = (Path(__file__).resolve().parent / "data" / "user_sample_raw.txt").read_text(encoding="utf-8")
        win = make_window()
        win.clean_src.setText(str(self.raw(sample)))
        win.clean_lang.select("Japanese")
        run_and_wait(win)
        md = (self.dir / "out_table.md").read_text(encoding="utf-8")
        rows = rows_of(md)
        self.assertEqual(rows[0], ["#", "Sentence"])
        self.assertEqual(len(rows) - 1, 22)
        self.assertIn(["6", "上司に言われて、来客にお茶を出しに応接室に入りました。"], rows)
        self.assertIn("22 sentences from 26 lines", win.clean_status.text())
        self.assertIn("3 furigana lines removed", win.clean_status.text())
        self.assertIn("Left out as furigana", win.clean_preview.toPlainText())

    def test_word_by_word_through_the_gui_with_a_language_that_needs_no_splitter(self):
        win = make_window()
        win.clean_src.setText(str(self.raw("The cat saw the dog. The dog ran.\n")))
        win.clean_lang.select("English")
        win.clean_vocab.setChecked(True)
        run_and_wait(win)
        rows = rows_of((self.dir / "out_table.md").read_text(encoding="utf-8"))
        self.assertEqual(rows[0], ["#", "Word", "Sentence"])
        self.assertEqual([r[1] for r in rows[1:]], ["the", "cat", "saw", "dog", "ran"])
        self.assertIn("5 words from 2 sentences", win.clean_status.text())

    def test_word_by_word_japanese_without_the_splitter_says_what_to_install(self):
        import ocr_nlp
        win = make_window()
        win.clean_src.setText(str(self.raw("日本語です。\n")))
        win.clean_lang.select("Japanese")
        win.clean_vocab.setChecked(True)
        with mock.patch.object(ocr_nlp, "tokenizer_problem", return_value="needs SudachiPy: pip install sudachipy"):
            run_and_wait(win)
        self.assertEqual(win.clean_status.text(), "needs SudachiPy: pip install sudachipy")   # the patched text, not the real one
        self.assertIn("#ff6b70", win.clean_status._style)
        self.assertTrue(win.btn_clean.isEnabled())
        self.assertEqual(list(self.dir.glob("*_table*")), [])


if __name__ == "__main__":
    unittest.main()
