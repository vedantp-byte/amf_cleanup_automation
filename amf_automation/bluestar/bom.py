"""Process-1 STEP-7: export the Standard BOM ('BOM lines') to Excel.

The download is captured directly from the browser and saved as
'BOM lines_<SN>.xlsx' in c:\\myloadpoint\\SN (7.4 - 7.6: rename + move). If the
browser capture fails, the newest 'BOM lines_*.xlsx' in Downloads is used.
"""

import shutil
import time
from pathlib import Path

import openpyxl
from playwright.sync_api import Error as PwError

from ..tracker import StepFailed
from . import selectors as S


def _open_bom_lines(bs):
    btn = bs.button(S.BTN_BOM_LINES, control=S.CTRL_BOM_LINES)
    try:
        if btn is None or not btn.is_visible():
            bs.open_tab(S.ENGINEERING_TAB)
            btn = bs.button(S.BTN_BOM_LINES, control=S.CTRL_BOM_LINES)
        btn.wait_for(state="visible", timeout=bs.timeout_ms)
    except (PwError, AttributeError):
        raise StepFailed("BOM 'Lines' button not found on the Bluestar page")
    if not btn.is_enabled():
        return False
    btn.click()                                                          # 7.1
    bs.wait_idle()
    end = time.time() + bs.bcfg["action_timeout_seconds"]
    while time.time() < end:
        if bs.is_visible_text(S.BOM_PAGE_TITLE):
            return True
        time.sleep(1)
    raise StepFailed("BOM lines page did not open")


def _click_office_button(bs):
    page = bs.page
    for loc in (page.get_by_role("button", name=S.BTN_OPEN_IN_OFFICE),
                page.locator(f'[aria-label="{S.BTN_OPEN_IN_OFFICE}"], [title="{S.BTN_OPEN_IN_OFFICE}"]')):
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                time.sleep(1)
                return
        except PwError:
            continue
    raise StepFailed("'Open in Microsoft Office' button not found on BOM lines page")


def _click_export_menu(bs):
    page = bs.page
    for loc in (page.get_by_role("menuitem", name=S.MENU_EXPORT_BOM_LINES, exact=True),
                page.get_by_role("button", name=S.MENU_EXPORT_BOM_LINES, exact=True),
                page.get_by_text(S.MENU_EXPORT_BOM_LINES, exact=True)):
        try:
            vis = loc.filter(visible=True)
            if vis.count():
                vis.last.click()
                time.sleep(1)
                return
        except PwError:
            continue
    raise StepFailed("'BOM lines' entry under EXPORT TO EXCEL not found")


def _newest_download(downloads: Path, since: float) -> Path | None:
    cands = [p for p in downloads.glob("BOM lines_*.xlsx") if p.stat().st_mtime >= since - 2]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def count_bom_lines(path: Path) -> int:
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            return max(0, sum(1 for r in ws.iter_rows(values_only=True) if any(v not in (None, "") for v in r)) - 1)
        finally:
            wb.close()
    except Exception:
        return -1


def download_bom(bs, serial: str, dest: Path, downloads: Path) -> tuple[str, Path | None, str]:
    """Returns (status, path, message)."""
    if not _open_bom_lines(bs):
        return "Not available", None, "BOM 'Lines' button disabled (no BOM for this object)"
    try:
        target = dest / f"BOM lines_{serial}.xlsx"
        dest.mkdir(parents=True, exist_ok=True)
        _click_office_button(bs)                                         # 7.2
        _click_export_menu(bs)                                           # 7.3
        started = time.time()
        timeout_ms = int(bs.bcfg["export_wait_minutes"] * 60 * 1000)
        try:
            with bs.page.expect_download(timeout=timeout_ms) as info:      # 7.4
                bs.click_button(S.BTN_DOWNLOAD, last=True)
            info.value.save_as(str(target))                              # 7.5 / 7.6
            original = info.value.suggested_filename
        except PwError as e:
            bs.log.info(f"      Browser download capture failed ({e}); checking Downloads folder.")
            found = None
            end = time.time() + 60
            while time.time() < end and not found:
                found = _newest_download(downloads, started)
                time.sleep(2)
            if not found:
                return "Failed", None, "BOM export file not downloaded. " + bs.message_bar_text()
            original = found.name
            shutil.move(str(found), str(target))
        lines = count_bom_lines(target)
        msg = f"{original} -> {target.name}, {lines} line(s)"
        if lines == 0:
            return "Downloaded (empty)", target, msg
        return "Downloaded", target, msg
    finally:
        bs.go_back()
