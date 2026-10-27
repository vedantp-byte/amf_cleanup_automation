"""python main.py --check : verify the PC is ready before a long run."""

import importlib
import shutil
import winreg
from pathlib import Path

from .input_reader import read_serials
from .vpn import VpnMonitor


def run_selfcheck(cfg: dict, log) -> int:
    res = cfg["resolved"]
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        tag = "PASS" if ok else "FAIL"
        log.info(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""), extra={"tag": tag})

    log.info("  Environment check")
    for mod in ("openpyxl", "yaml", "playwright.sync_api", "win32com.client", "pywinauto", "pypdf"):
        try:
            importlib.import_module(mod)
            check(f"Python package {mod}", True)
        except Exception as e:
            check(f"Python package {mod}", False, f"{e} -> run setup.bat")

    try:
        raw, col = read_serials(res["input_file"], cfg["input"]["serial_column_candidates"])
        check("Input file", bool(raw), f"{res['input_file'].name}: {len(raw)} serial(s) in column '{col}'")
    except Exception as e:
        check("Input file", False, str(e))

    ok, detail = VpnMonitor(cfg, log).status()
    check("GlobalProtect VPN connected", ok, detail)

    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    chrome_x86 = Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
    check("Google Chrome installed", chrome.exists() or chrome_x86.exists())

    from .bluestar.client import is_running
    client = Path(cfg["bluestar"]["client_exe"])
    check("Bluestar Client Services installed", client.exists(), str(client))
    if client.exists():
        log.info(f"  [INFO] Bluestar Client Services running: {'yes' if is_running(client) else 'no (started automatically)'}")

    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "SolidEdge.Application").Close()
        check("Solid Edge registered (COM)", True)
    except OSError:
        check("Solid Edge registered (COM)", False, "SolidEdge.Application not found")

    for label, folder in (("myloadpoint folder", res["myloadpoint"]),
                          ("Working folder", res["working_root"]),
                          ("Reports folder", res["reports"])):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            free = shutil.disk_usage(folder).free / 1024 ** 3
            check(label, True, f"{folder} ({free:.0f} GB free)")
        except Exception as e:
            check(label, False, f"{folder}: {e}")

    check("Downloads folder", res["downloads"].exists(), str(res["downloads"]))
    failed = [r for r in results if not r[1]]
    log.info(f"  {len(results) - len(failed)}/{len(results)} checks passed.")
    return 1 if failed else 0
