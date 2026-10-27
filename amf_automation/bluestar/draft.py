"""Process-1 STEP-6: export the 2D Draft PDF from 'File info' to c:\\myloadpoint\\SN."""

import time
from pathlib import Path

from playwright.sync_api import Error as PwError

from ..tracker import StepFailed
from ..utils import wait_file_stable
from . import selectors as S


def download_2d_draft(bs, serial: str, dest: Path) -> tuple[str, Path | None, str]:
    """Returns (status, pdf_path, message). status: Downloaded / Not available / Failed."""
    bs.click_button(S.BTN_FILE_INFO, tab=S.ENGINEERING_TAB, control=S.CTRL_FILE_INFO)   # 6.1
    end = time.time() + bs.bcfg["action_timeout_seconds"]
    rows = []
    while time.time() < end:
        rows = bs.read_grid(S.GRID_HEADER_FILE_TYPE)
        if rows:
            break
        time.sleep(1)
    try:
        files = [(r.get(S.COL_FILE_NAME, ""), r.get(S.COL_FILE_TYPE, "")) for r in rows]
        bs.log.info(f"      File info: {', '.join(f for f, _ in files) or 'no files listed'}")

        pdfs = [r for r in rows if r.get(S.COL_FILE_TYPE, "").strip().lower() == "pdf"]
        if not pdfs:                                                     # 6.6
            return "Not available", None, "No PDF in File info"
        same_name = [r for r in pdfs if Path(r.get(S.COL_FILE_NAME, "")).stem.upper() == serial.upper()]
        row = (same_name or pdfs)[0]
        name = row.get(S.COL_FILE_NAME, "") or f"{serial}.PDF"
        note = "" if same_name else f"PDF name differs from serial ({name})"

        bs.click_grid_cell(row["_index"], S.COL_FILE_TYPE)              # 6.2
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
    finally:
        try:
            if bs.is_visible_text(S.OPEN_FILE_QUESTION):
                bs.click_button("No", last=True)
        except PwError:
            pass
        bs.go_back()
