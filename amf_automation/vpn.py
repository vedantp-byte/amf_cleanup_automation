"""Process-1 STEP-1: GlobalProtect VPN check, continuous monitoring and reconnect.

Connected  = the "PANGP Virtual Ethernet Adapter" is Up and has a real IPv4
             address (plus optional TCP reachability test of check_host).
Reconnect  = open the GlobalProtect panel and press "Connect" (pywinauto);
             if that does not work, alert the user and wait for a manual connect.
"""

import socket
import subprocess
import threading
import time
import winsound
from pathlib import Path

from .tracker import CycleAbort

_PS = (
    "Get-NetAdapter | Where-Object {{ $_.InterfaceDescription -like '*{desc}*' }} | ForEach-Object {{ "
    "$ip = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
    "Where-Object {{ $_.IPAddress -notlike '169.254*' }} | Select-Object -First 1).IPAddress; "
    "Write-Output ($_.Status + '|' + $ip) }}"
)


class VpnMonitor:
    def __init__(self, cfg: dict, log):
        self.cfg = cfg["vpn"]
        self.log = log
        self.enabled = bool(self.cfg["enabled"])
        self.drops = 0                 # disconnects seen by the background monitor
        self.last_state = None
        self.last_detail = ""
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ status
    def status(self) -> tuple[bool, str]:
        if not self.enabled:
            return True, "VPN check disabled in config"
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 _PS.format(desc=self.cfg["adapter_description"])],
                capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
        except Exception as e:
            return False, f"Could not query network adapters: {e}"

        lines = [ln.strip() for ln in out.splitlines() if "|" in ln]
        if not lines:
            return False, "GlobalProtect adapter not found (is GlobalProtect installed?)"
        for ln in lines:
            state, _, ip = ln.partition("|")
            if state.strip().lower() == "up" and ip.strip():
                host = (self.cfg.get("check_host") or "").strip()
                if host and not self._reachable(host):
                    return False, f"VPN adapter up ({ip.strip()}) but {host} not reachable"
                return True, f"Connected ({ip.strip()})"
        return False, "Disconnected"

    def is_connected(self) -> bool:
        ok, detail = self.status()
        with self._lock:
            if self.last_state is True and not ok:
                self.drops += 1
            self.last_state, self.last_detail = ok, detail
        return ok

    @staticmethod
    def _reachable(host_port: str) -> bool:
        host, _, port = host_port.partition(":")
        try:
            with socket.create_connection((host, int(port or 443)), timeout=8):
                return True
        except OSError:
            return False

    # --------------------------------------------------------------- reconnect
    def ensure_connected(self, context: str = "") -> str:
        """Return status text when connected; raise CycleAbort if it cannot be restored."""
        if self.is_connected():
            return self.last_detail
        where = f" ({context})" if context else ""
        self.log.warning(f"  !! GlobalProtect VPN is NOT connected{where}: {self.last_detail}")

        if self.cfg["auto_reconnect"]:
            self.log.info("     Trying to reconnect GlobalProtect automatically ...")
            self._click_connect()
            if self._wait_connected(self.cfg["reconnect_timeout_seconds"]):
                self.log.info(f"     VPN reconnected: {self.last_detail}", extra={"tag": "PASS"})
                return self.last_detail

        minutes = self.cfg["manual_wait_minutes"]
        self.log.error("=" * 78)
        self.log.error(f"  ACTION NEEDED: Please connect GlobalProtect VPN now. Waiting up to {minutes} min ...")
        self.log.error("=" * 78)
        self._alert()
        if self._wait_connected(minutes * 60, beep_every=60):
            self.log.info(f"     VPN connected: {self.last_detail}", extra={"tag": "PASS"})
            return self.last_detail
        raise CycleAbort(f"GlobalProtect VPN could not be connected within {minutes} min{where}")

    def _wait_connected(self, seconds: float, beep_every: float = 0) -> bool:
        end, last_beep = time.time() + seconds, time.time()
        while time.time() < end:
            if self.is_connected():
                return True
            if beep_every and time.time() - last_beep > beep_every:
                self._alert()
                last_beep = time.time()
            time.sleep(5)
        return False

    def _click_connect(self):
        exe = Path(self.cfg["globalprotect_exe"])
        try:
            if exe.exists():
                subprocess.Popen([str(exe)], creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(4)
            from pywinauto import Desktop

            for win in Desktop(backend="uia").windows(title_re=".*GlobalProtect.*"):
                try:
                    btn = win.child_window(title="Connect", control_type="Button")
                    if btn.exists(timeout=3):
                        btn.click_input()
                        self.log.info("     Clicked 'Connect' in GlobalProtect.")
                        return
                except Exception:
                    continue
            self.log.info("     GlobalProtect 'Connect' button not found (may need SSO / already connecting).")
        except Exception as e:
            self.log.info(f"     Automatic reconnect not possible: {e}")

    @staticmethod
    def _alert():
        try:
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                time.sleep(0.4)
        except Exception:
            pass

    # ---------------------------------------------------- background monitoring
    def start_monitor(self):
        if not self.enabled or self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vpn-monitor", daemon=True)
        self._thread.start()

    def stop_monitor(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self):
        while not self._stop.wait(self.cfg["check_interval_seconds"]):
            was = self.last_state
            ok = self.is_connected()
            if was and not ok:
                self.log.warning(f"  !! VPN monitor: GlobalProtect disconnected ({self.last_detail}). "
                                 "Process will reconnect before the next Bluestar action.")
                self._alert()
            elif was is False and ok:
                self.log.info(f"  VPN monitor: GlobalProtect connected again ({self.last_detail}).")
