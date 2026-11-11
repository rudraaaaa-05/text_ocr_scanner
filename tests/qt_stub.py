"""Lets the test-suite import ocr_scanner on machines without PySide6 (CI, headless boxes).

The tested logic (session files, region maths, capture routing, hotkeys, the Bus) does not need a real Qt.
If PySide6 is installed it is used as is and nothing is stubbed. Widget behaviour is NOT covered here.
"""
import sys
import types


def install_if_missing():
    try:
        import PySide6  # noqa: F401
        return False
    except ImportError:
        pass

    class _Signal:
        def __init__(self, *types_):
            self._name = None

        def __set_name__(self, owner, name):
            self._name = "_sig_" + name

        def __get__(self, obj, owner=None):
            if obj is None:
                return self
            bound = obj.__dict__.get(self._name)
            if bound is None:
                bound = obj.__dict__[self._name] = _BoundSignal()
            return bound

    class _BoundSignal:
        def __init__(self):
            self.slots = []

        def connect(self, fn):
            self.slots.append(fn)

        def emit(self, *args):
            for fn in list(self.slots):
                fn(*args)

    class _Mod(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            cls = type(name, (), {"__init__": lambda self, *a, **k: None})
            setattr(self, name, cls)
            return cls

    for name in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
        sys.modules[name] = _Mod(name)
    sys.modules["PySide6.QtCore"].Signal = _Signal
    return True
