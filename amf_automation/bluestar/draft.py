"""Process-1 STEP-6: export the 2D Draft PDF from 'File info' to c:\\myloadpoint\\SN."""

import time
from pathlib import Path

from playwright.sync_api import Error as PwError

from ..tracker import StepFailed
from ..utils import wait_file_stable
from . import selectors as S


def _read_files(bs) -> list[dict]:
    end = time.time() + bs.bcfg["action_timeout_seconds"]
    while time.time() < end:
        rows = bs.read_grid(S.GRID_HEADER_FILE_TYPE)
        if rows:
            return rows
        time.sleep(1)
    return []


def _visible_cell(bs, row_index: int):
    """First visible, non-link cell of a tagged File info row (file name preferred)."""
    row = bs.page.locator(f'[data-amf-grid] [data-amf-row="{row_index}"]').first
    for col in (S.COL_FILE_NAME, S.COL_FILE_TYPE):
        cell = row.locator(f'[data-amf-col="{col}"]').first
        try:
            if cell.count() and cell.is_visible():
                return cell
        except PwError:
            continue
    cells = row.locator('[role="gridcell"]').filter(visible=True)
    return cells.first if cells.count() else None


def _click_top(cell) -> bool:
    """Click the TOP edge of the cell with a short timeout. The last row of the File info grid is
    often half covered by the grid's horizontal scrollbar; clicking its centre makes Playwright
    scroll up/down for 60 s (the grid ignores scroll-into-view), so never retry long here."""
    try:
        box = cell.bounding_box()
        y = 4 if not box else max(2, min(6, box["height"] / 4))
        cell.click(position={"x": 12, "y": y}, timeout=4000)
        return True
    except PwError:
        return False


def _select_file_row(bs, file_name: str) -> bool:
    """Bring the row into view (grid scrolled DOWN only - no up/down jitter), then click it (6.2)."""
    target = file_name.upper()
    for _attempt in range(8):
        rows = bs.read_grid(S.GRID_HEADER_FILE_TYPE)
        hit = next((r for r in rows if r.get(S.COL_FILE_NAME, "").strip().upper() == target), None)
        if hit is not None and bs.row_visible(hit["_index"]):
            cell = _visible_cell(bs, hit["_index"])
            if cell is not None and _click_top(cell):
                time.sleep(1)
                bs.wait_idle()
                return True
        bs.scroll_grid(400)                  # downwards only: the PDF is normally the last file
    # last resort: select the first file row and move down to the target with the arrow keys
    rows = bs.read_grid(S.GRID_HEADER_FILE_TYPE)
    names = [r.get(S.COL_FILE_NAME, "").strip().upper() for r in rows]
    if target in names and rows:
        first = _visible_cell(bs, rows[0]["_index"])
        if first is not None and _click_top(first):
            time.sleep(0.5)
            for _ in range(names.index(target)):
                bs.page.keyboard.press("ArrowDown")
                time.sleep(0.3)
            time.sleep(1)
            bs.wait_idle()
            bs.log.info(f"      '{file_name}' selected with the arrow keys")
            return True
    return False


def download_2d_draft(bs, serial: str, dest: Path) -> tuple[str, Path | None, str]:
    """Returns (status, pdf_path, message). status: Downloaded / Not available / Failed."""
    bs.click_button(S.BTN_FILE_INFO, tab=S.ENGINEERING_TAB, control=S.CTRL_FILE_INFO)   # 6.1
    try:
        rows = _read_files(bs)
        bs.log.info(f"      File info: {', '.join(r.get(S.COL_FILE_NAME, '') for r in rows) or 'no files listed'}")

        pdfs = [r for r in rows if r.get(S.COL_FILE_TYPE, "").strip().lower() == "pdf"]
        if not pdfs:                                                     # 6.6
            return "Not available", None, "No PDF in File info"
        same_name = [r for r in pdfs if Path(r.get(S.COL_FILE_NAME, "")).stem.upper() == serial.upper()]
        row = (same_name or pdfs)[0]
        name = row.get(S.COL_FILE_NAME, "") or f"{serial}.PDF"
        note = "" if same_name else f"PDF name differs from serial ({name})"

        if not _select_file_row(bs, name):                               # 6.2
            raise StepFailed(f"Could not select '{name}' in the File info grid")
        bs.click_button(S.BTN_EXPORT_FILE)                              # 6.3
        dest.mkdir(parents=True, exist_ok=True)
        if not (bs.fill_labeled(S.EXPORT_FOLDER_LABEL, str(dest))       # 6.4
                or bs.fill_input_with_value(S.CHECKOUT_PATH_HINT.pattern, str(dest))):
            raise StepFailed("'Select destination folder' box not found")
        bs.click_button("OK", last=True)

        target = dest / name
        limit = time.time() + bs.bcfg["export_wait_minutes"] * 60          # 6.5
        while time.time() < limit:
            if bs.is_visible_text(S.OPEN_FILE_QUESTION):
                try:
                    bs.click_button("No", last=True)                       # do not open the PDF
                except Exception:
                    pass
            found = target if target.exists() else next(
                (p for p in dest.glob("*") if p.suffix.lower() == ".pdf" and p.stem.upper() == Path(name).stem.upper()),
                None)
            if found and wait_file_stable(found, seconds=3, timeout=60):
                return "Downloaded", found, note
            bs.dismiss_popups(aggressive=False)
            time.sleep(2)
        return "Failed", None, f"PDF not found in {dest} after export. " + bs.message_bar_text()
    except Exception:
        shot = bs.screenshot(f"{serial}_2D_draft_error")             # evidence BEFORE leaving the page
        if shot:
            bs.log.info(f"      screenshot of error screen: {shot}")
        raise
    finally:
        try:
            if bs.is_visible_text(S.OPEN_FILE_QUESTION):
                bs.click_button("No", last=True)
        except (PwError, StepFailed):
            pass
        bs.go_back()
