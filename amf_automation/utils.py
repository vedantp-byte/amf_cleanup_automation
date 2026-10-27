"""Small shared helpers: Windows known folders, time stamps, folder snapshots."""

import ctypes
import os
import time
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

_FOLDER_IDS = {
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def known_folder(name: str) -> Path:
    """Return the real (possibly OneDrive-redirected) Documents / Downloads folder."""
    fallback = Path.home() / name.capitalize()
    try:
        u = uuid.UUID(_FOLDER_IDS[name])
        guid = _GUID()
        guid.Data1, guid.Data2, guid.Data3 = u.fields[0], u.fields[1], u.fields[2]
        for i, b in enumerate(u.bytes[8:]):
            guid.Data4[i] = b
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        if hr != 0 or not out.value:
            return fallback
        path = Path(out.value)
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path
    except Exception:
        return fallback


def now() -> datetime:
    return datetime.now()


def iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def folder_snapshot(folder: Path) -> tuple[int, int, float]:
    """(file count, total bytes, newest mtime) of every file below folder."""
    count, size, newest = 0, 0, 0.0
    if not folder.exists():
        return 0, 0, 0.0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            count += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
    return count, size, newest


def wait_file_stable(path: Path, seconds: float = 3, timeout: float = 120) -> bool:
    """Wait until a file exists and its size stops changing."""
    end = time.time() + timeout
    last = -1
    stable_since = None
    while time.time() < end:
        if path.exists():
            size = path.stat().st_size
            if size == last and size > 0:
                if stable_since and time.time() - stable_since >= seconds:
                    return True
            else:
                stable_since = time.time()
                last = size
        time.sleep(1)
    return path.exists() and path.stat().st_size > 0


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
