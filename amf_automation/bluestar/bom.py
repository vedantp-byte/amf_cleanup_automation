"""Process-1 STEP-7: export the Standard BOM ('BOM lines') to Excel.

  7.1 BOM 'Lines'  ->  BOM lines page
  7.2 'Open in Microsoft Office' (Office icon, top right)
  7.3 'BOM lines' under EXPORT TO EXCEL  ->  'Export to Excel' panel
  7.4 'Download'  ->  file captured from the browser
  7.5/7.6 saved as 'BOM lines_<SN>.xlsx' in c:\\myloadpoint\\SN
If the browser capture fails, the newest 'BOM lines_*.xlsx' in Downloads is used.
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


def _first_visible(*locators):
    for loc in locators:
        try:
            vis = loc.filter(visible=True)
            if vis.count():
                return vis.first
        except PwError:
            continue
    return None


def _wait_visible(make_locators, seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        found = _first_visible(*make_locators())
        if found is not None:
            return found
        time.sleep(1)
    return None


def _click_office_button(bs):                                           # 7.2
    page = bs.page
    btn = _wait_visible(lambda: (
        page.locator(f'[data-dyn-controlname="{S.CTRL_OPEN_IN_OFFICE}"]'),
        page.locator(f'button[title="{S.BTN_OPEN_IN_OFFICE}"], button[aria-label="{S.BTN_OPEN_IN_OFFICE}"]'),
        page.get_by_role("button", name=S.BTN_OPEN_IN_OFFICE),
        page.get_by_title(S.BTN_OPEN_IN_OFFICE),
    ), 20)
    if btn is None:
        bs.log.info(f"      Visible buttons: {bs.visible_button_names()}")
        raise StepFailed("'Open in Microsoft Office' button not found on BOM lines page")
    btn.click()
    time.sleep(1.5)


def _click_export_menu(bs):                                             # 7.3
    page = bs.page
    heading = page.get_by_text("EXPORT TO EXCEL", exact=False)
    item = _wait_visible(lambda: (
        # the 'BOM lines' entry directly under the EXPORT TO EXCEL heading of the drop-down
        heading.locator(f"xpath=following::*[normalize-space(.)='{S.MENU_EXPORT_BOM_LINES}'][1]"),
        page.get_by_role("menuitem", name=S.MENU_EXPORT_BOM_LINES, exact=True),
        page.locator("[role=menu], .flyout, .popupContainer, [role=dialog]")
            .get_by_text(S.MENU_EXPORT_BOM_LINES, exact=True),
    ), 15)
    if item is None:
        bs.log.info(f"      Visible buttons/menu items: {bs.visible_button_names()}")
        raise StepFailed("'BOM lines' entry under EXPORT TO EXCEL not found")
    item.click()


def _find_download_button(bs, seconds: float):                          # 7.4
    """'Download' in the 'Export to Excel' panel (searched in the page and any embedded frame)."""
    end = time.time() + seconds
    while time.time() < end:
        for frame in bs.page.frames:
            btn = _first_visible(
                frame.get_by_role("button", name=S.BTN_DOWNLOAD, exact=True),
                frame.locator("button, [role=button], a").filter(has_text=S.BTN_DOWNLOAD),
                frame.get_by_text(S.BTN_DOWNLOAD, exact=True),
            )
            if btn is not None:
                return btn
        bs.dismiss_popups()
        time.sleep(1)
    return None


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
        _click_office_button(bs)
        _click_export_menu(bs)
        download_btn = _find_download_button(bs, bs.bcfg["action_timeout_seconds"])
        if download_btn is None:
            bs.log.info(f"      Visible buttons: {bs.visible_button_names()}")
            raise StepFailed("'Download' button of the Export to Excel panel not found")

        started = time.time()
        timeout_ms = int(bs.bcfg["export_wait_minutes"] * 60 * 1000)
        try:
            with bs.page.expect_download(timeout=timeout_ms) as info:
                download_btn.click()
            info.value.save_as(str(target))                              # 7.5 / 7.6
            original = info.value.suggested_filename
        except PwError as e:
            bs.log.info(f"      Browser download capture failed ({e}); checking Downloads folder.")
            found = None
            end = time.time() + 90
            while time.time() < end and not found:
                found = _newest_download(downloads, started)
                time.sleep(2)
            if not found:
                return "Failed", None, "BOM export file not downloaded. " + bs.message_bar_text()
            original = found.name
            shutil.move(str(found), str(target))
        lines = count_bom_lines(target)
        msg = f"{original} -> {target}, {lines} line(s)"
        if lines == 0:
            return "Downloaded (empty)", target, msg
        return "Downloaded", target, msg
    except Exception:
        shot = bs.screenshot(f"{serial}_BOM_error")                  # evidence BEFORE leaving the page
        if shot:
            bs.log.info(f"      screenshot of error screen: {shot}")
        raise
    finally:
        _close_export_panel(bs)
        bs.go_back()


def _close_export_panel(bs):
    try:
        cancel = bs.page.get_by_role("button", name="Cancel", exact=True).filter(visible=True)
        if cancel.count():
            cancel.last.click()
            time.sleep(1)
    except PwError:
        pass
