"""Process-1 STEP-5: Check out (download) the Solid Edge 3D CAD to c:\\myloadpoint\\SN\\SN.

The actual file transfer is done by "Bluestar Client Services" on this PC, so
completion is detected on disk: the top-level .asm exists and the folder has
stopped changing for `stable_seconds`. During the wait, Bluestar pop-ups are
answered (Wait / I'm here / Yes / OK) and the VPN is checked continuously.
"""

import re
import time
from pathlib import Path

from playwright.sync_api import Error as PwError

from ..tracker import StepFailed
from ..utils import folder_snapshot, human_size
from . import selectors as S
from .client import ensure_running as ensure_client_running


def open_checkout_panel(bs):
    """5.1 - 5.3: press Check out, answer 'Select CAD system' and the 'approved revision' question."""
    ccfg = bs.bcfg["checkout"]
    ensure_client_running(bs.cfg, bs.log)
    bs.click_button(S.BTN_CHECK_OUT, control=S.CTRL_CHECK_OUT)
    end = time.time() + bs.bcfg["action_timeout_seconds"] * 2
    client_restarted = False
    while time.time() < end:
        page = bs.page
        if bs.is_visible_text(S.CLIENT_NOT_RUNNING):
            if client_restarted:
                raise StepFailed("Bluestar says 'Client is currently not running' even after starting "
                                 "Bluestar Client Services - check that it is signed in")
            bs.log.warning("      Bluestar: 'Client is currently not running' -> starting Bluestar Client Services")
            ensure_client_running(bs.cfg, bs.log)
            client_restarted = True
            bs.page.keyboard.press("Escape")
            time.sleep(2)
            bs.click_button(S.BTN_CHECK_OUT, control=S.CTRL_CHECK_OUT)
            continue
        if bs.is_visible_text(S.CAD_SYSTEM_TITLE):
            try:
                page.get_by_text(ccfg["cad_system"], exact=True).first.click()
                time.sleep(0.5)
                bs.click_button("OK", last=True)
                bs.log.info(f"      CAD system selected: {ccfg['cad_system']}")
            except PwError:
                pass
            continue
        if bs.is_visible_text(S.APPROVED_WARNING):
            try:
                bs.click_button("Yes", last=True)
                bs.log.info("      'Revision has been approved ... continue?' -> Yes")
            except Exception:
                pass
            continue
        if bs.is_visible_text(S.CHECKOUT_PANEL_TITLE):
            return
        bs.dismiss_popups()
        time.sleep(1)
    raise StepFailed("Check out pop-up did not open. " + bs.message_bar_text())


def _set_force_full(bs, wanted: bool) -> str:
    page = bs.page
    for role in ("switch", "checkbox"):
        loc = page.get_by_role(role, name=S.FORCE_FULL_CHECKOUT)
        try:
            if loc.count() and loc.first.is_visible():
                state = (loc.first.get_attribute("aria-checked") or "").lower() == "true"
                if state != wanted:
                    loc.first.click()
                    time.sleep(0.5)
                return "Yes" if wanted else "No"
        except PwError:
            continue
    return "not verified"


def start_checkout(bs, dest: Path):
    """5.4 - 5.5: put the destination path in 'Checkout to' and press Check out."""
    dest_text = str(dest)
    if not bs.fill_input_with_value(S.CHECKOUT_PATH_HINT.pattern, dest_text):
        if not bs.fill_labeled("Checkout to", dest_text):
            raise StepFailed("'Checkout to' path box not found in the Check out pop-up")
    force = _set_force_full(bs, bool(bs.bcfg["checkout"]["force_full_checkout"]))
    bs.log.info(f"      Checkout to: {dest_text}  (Force full check out: {force})")
    # the pop-up's own "Check out" button (not the toolbar button cmdCheckOut)
    panel_btn = (bs.page.get_by_role("button", name=re.compile(rf"^\s*{S.BTN_CHECK_OUT}(?![A-Za-z])", re.I))
                 .filter(visible=True)
                 .and_(bs.page.locator(f'button:not([data-dyn-controlname="{S.CTRL_CHECK_OUT}"])')))
    try:
        panel_btn.last.wait_for(state="visible", timeout=15_000)
    except PwError:
        raise StepFailed("'Check out' button of the Check out pop-up not found")
    panel_btn.last.click()
    bs.wait_idle()


def find_top_asm(folder: Path, name: str) -> Path | None:
    if not folder.exists():
        return None
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() == ".asm" and p.stem.upper() == name.upper():
            return p
    return None


def find_item_file(folder: Path, name: str) -> Path | None:
    """Top file of a part or assembly item (used for missing-file downloads)."""
    if not folder.exists():
        return None
    for ext in (".asm", ".par", ".psm", ".pwd"):
        for p in folder.rglob(f"*{ext}"):
            if p.stem.upper() == name.upper():
                return p
    return None


def wait_for_download(bs, dest: Path, item: str, started: float, expect_asm: bool = True) -> dict:
    """5.6 - 5.7: wait until the full CAD is downloaded. Returns stats dict."""
    ccfg = bs.bcfg["checkout"]
    timeout = time.time() + ccfg["download_timeout_minutes"] * 60
    stable_needed = ccfg["stable_seconds"]
    no_activity_limit = ccfg["no_activity_minutes"] * 60

    initial = folder_snapshot(dest)
    last = initial
    last_change = time.time()
    activity = False
    vpn_drops_before = bs.vpn.drops
    vpn_interrupted = False
    last_log = 0.0

    while time.time() < timeout:
        bs.dismiss_popups(aggressive=True)
        if not bs.vpn.is_connected():
            vpn_interrupted = True
            bs.vpn.ensure_connected("during 3D CAD download")
        snap = folder_snapshot(dest)
        if snap != last:
            activity = True
            last = snap
            last_change = time.time()
        top = find_top_asm(dest, item) if expect_asm else find_item_file(dest, item)
        idle_for = time.time() - last_change

        if time.time() - last_log > 30:
            bs.log.info(f"      downloading ... {snap[0]} files, {human_size(snap[1])}, "
                        f"unchanged for {idle_for:.0f}s")
            last_log = time.time()

        if top and snap[0] > 0 and idle_for >= stable_needed and not bs.is_visible_text(S.BUSY_TEXT):
            if activity or snap[0] > initial[0]:
                break
            if time.time() - started > no_activity_limit:
                bs.log.warning("      No new file activity detected; files were already present.")
                break
        if not top and not activity and time.time() - started > no_activity_limit:
            raise StepFailed(f"No files arrived in {dest} within {ccfg['no_activity_minutes']} min. "
                             + bs.message_bar_text())
        time.sleep(5)
    else:
        raise StepFailed(f"3D CAD download not finished within {ccfg['download_timeout_minutes']} min")

    top = find_top_asm(dest, item) if expect_asm else find_item_file(dest, item)
    if not top:
        raise StepFailed(f"Top-level file for {item} not found in {dest} after download")
    count, size, _ = folder_snapshot(dest)
    return {"top_file": top, "files": count, "size": size,
            "vpn_interrupted": vpn_interrupted or bs.vpn.drops > vpn_drops_before}


def checkout_selected(bs, dest: Path, item: str, expect_asm: bool = True) -> dict:
    """Full STEP-5 on the currently selected row, with retry when the VPN dropped."""
    dest.mkdir(parents=True, exist_ok=True)
    attempts = 1 + int(bs.bcfg["checkout"]["retries"])
    result = None
    for attempt in range(1, attempts + 1):
        bs.vpn.ensure_connected("before check out")
        started = time.time()
        open_checkout_panel(bs)
        start_checkout(bs, dest)
        result = wait_for_download(bs, dest, item, started, expect_asm=expect_asm)
        if not result["vpn_interrupted"] or attempt == attempts:
            break
        bs.log.warning(f"      VPN was interrupted during download -> repeating full check out "
                       f"(attempt {attempt + 1}/{attempts}) to avoid missing files.")
    result["attempts"] = attempt
    return result
