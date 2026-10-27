"""Connect to Solid Edge (visible), open / close documents, COM helpers."""

import time

import pythoncom
import win32com.client

from ..tracker import StepFailed

ASSEMBLY_TLB = ("{3E2B3BD4-F0B9-11D1-BDFD-080036B4D502}", 0, 1, 0)


def get(obj, name, default=None):
    """Read a COM property safely."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


def call0(obj, name, default=None):
    """Call a zero-argument COM method that dynamic dispatch may expose as a property."""
    try:
        v = getattr(obj, name)
        return v() if callable(v) else v
    except Exception:
        return default


def put(obj, name, value) -> bool:
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def items(collection):
    """Iterate a 1-based COM collection."""
    try:
        n = collection.Count
    except Exception:
        return
    for i in range(1, n + 1):
        try:
            yield collection.Item(i)
        except Exception:
            continue


class SolidEdgeApp:
    def __init__(self, log):
        self.log = log
        self.app = None
        self._typed_ok = False

    def connect(self):
        pythoncom.CoInitialize()
        try:
            self.app = win32com.client.GetActiveObject("SolidEdge.Application")
            self.log.info("      Connected to running Solid Edge.")
        except Exception:
            self.log.info("      Starting Solid Edge (this can take a minute) ...")
            try:
                self.app = win32com.client.Dispatch("SolidEdge.Application")
            except Exception as e:
                raise StepFailed(f"Solid Edge could not be started: {e}")
        put(self.app, "Visible", True)          # all work happens on screen
        put(self.app, "DisplayAlerts", False)   # e.g. 'suppress relations' prompts during replace
        try:
            win32com.client.gencache.EnsureModule(*ASSEMBLY_TLB)
            self._typed_ok = True
        except Exception:
            self._typed_ok = False

    def open(self, path):
        if self.app is None:
            self.connect()
        try:
            doc = self.app.Documents.Open(str(path))
        except Exception as e:
            raise StepFailed(f"Solid Edge could not open {path}: {e}")
        self.idle()
        return doc

    def idle(self, seconds: float = 1.0):
        try:
            self.app.DoIdle()
        except Exception:
            pass
        time.sleep(seconds)

    def typed_assembly(self, doc):
        """Early-bound AssemblyDocument (needed for methods with output arguments, e.g. Range)."""
        if not self._typed_ok:
            return doc
        try:
            return win32com.client.CastTo(doc, "AssemblyDocument")
        except Exception:
            return doc

    def close(self, doc, save: bool = False):
        try:
            doc.Close(save)
        except Exception:
            try:
                doc.Close()
            except Exception:
                pass
        self.idle(0.5)
