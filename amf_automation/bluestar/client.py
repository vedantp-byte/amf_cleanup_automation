"""Bluestar Client Services: the local program that performs check-outs into C:\\myloadpoint.

Bluestar refuses to check out ("Client is currently not running on your computer")
when it is not running, so it is started automatically when needed.
"""

import subprocess
import time
from pathlib import Path

from ..tracker import StepFailed


def is_running(exe: Path) -> bool:
    try:
        # CSV output: the plain table truncates image names to 25 characters
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe.name}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return f'"{exe.name.lower()}"' in out.lower()
    except Exception:
        return False


def ensure_running(cfg: dict, log, settle_seconds: int = 20) -> str:
    exe = Path(cfg["bluestar"]["client_exe"])
    if is_running(exe):
        return "running"
    if not exe.exists():
        raise StepFailed(f"Bluestar Client Services not found: {exe} (set bluestar.client_exe in config.yaml)")
    log.info("      Starting Bluestar Client Services ...")
    subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=subprocess.DETACHED_PROCESS)
    end = time.time() + 90
    while time.time() < end:
        if is_running(exe):
            time.sleep(settle_seconds)          # let it register with Bluestar
            log.info("      Bluestar Client Services started.")
            return "started"
        time.sleep(2)
    raise StepFailed("Bluestar Client Services could not be started - start it manually and retry")
