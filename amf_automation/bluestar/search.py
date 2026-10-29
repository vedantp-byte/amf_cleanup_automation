"""Process-1 STEP-4: search the serial and choose the revision to download.

Decision order (exactly as the standard process):
 4.3/4.5  "Latest revisions"        -> latest revision Approved?        -> use it
 4.5/4.6  "All approved revisions"  -> latest Approved revision exists? -> use it
 4.7      fallback                  -> latest revision (Design / other status)
 4.8      "All"                     -> latest of anything, else "Not Available in Bluestar"
Only rows whose Object ID equals the serial exactly (no prefix / suffix) are considered.
"""

import re
import time

from playwright.sync_api import Error as PwError

from ..models import BluestarRow
from ..tracker import StepFailed
from . import selectors as S


def revision_key(rev: str) -> tuple:
    """Sort key for Bluestar revisions such as NEW-0 < NEW-1 < A-0 < A-1 < B-0 < AA-0."""
    rev = (rev or "").strip().upper()
    m = re.match(r"^([A-Z]+)\s*-?\s*(\d+)?$", rev)
    if not m:
        return (-1, -1, rev)
    letters, num = m.group(1), int(m.group(2) or 0)
    if letters == "NEW":
        rank = 0
    else:
        rank = 0
        for ch in letters:
            rank = rank * 26 + (ord(ch) - 64)
    return (rank, num, rev)


def search_serial(bs, serial: str):
    """4.1/4.2: type *serial* in the filter and choose the 'Object ID' field."""
    before = _grid_signature(bs)
    box = bs.filter_box()
    box.click()
    box.fill("")
    box.fill(f"*{serial}*")
    time.sleep(1.5)
    option = bs.page.get_by_text(S.FILTER_FIELD_OPTION)
    try:
        option.first.wait_for(state="visible", timeout=10_000)
        option.first.click()
    except PwError:
        box.press("Enter")       # first entry of the pop-up is Object ID
    bs.wait_idle()
    _wait_grid_refreshed(bs, before)


def set_revision_filter(bs, label: str):
    """4.3 / 4.5 / 4.8: choose a value in 'Revision and approval filter'."""
    page = bs.page
    combo = None
    for loc in (page.get_by_role("combobox", name=S.REVISION_FILTER_LABEL),
                page.get_by_label(S.REVISION_FILTER_LABEL, exact=True),
                page.locator(f'input[aria-label="{S.REVISION_FILTER_LABEL}"]')):
        try:
            if loc.count() and loc.first.is_visible():
                combo = loc.first
                break
        except PwError:
            continue
    if combo is None:
        raise StepFailed("'Revision and approval filter' not found on the Bluestar page")

    if _combo_value(combo).lower() == label.lower():
        return

    before = _grid_signature(bs)
    for _attempt in range(3):
        combo.click()
        time.sleep(0.8)
        picked = False
        for opt in (page.get_by_role("option", name=label, exact=True),
                    page.locator("li, [role='option'], .comboBox-listItem").get_by_text(label, exact=True)):
            try:
                if opt.count() and opt.first.is_visible():
                    opt.first.click()
                    picked = True
                    break
            except PwError:
                continue
        if not picked:                           # type the value instead
            combo.fill(label)
            combo.press("Enter")
        bs.wait_idle()
        if _combo_value(combo).lower() == label.lower():
            break
        bs.log.info(f"      Revision filter shows '{_combo_value(combo)}' instead of '{label}' - retrying")
    else:
        raise StepFailed(f"Could not set 'Revision and approval filter' to '{label}'")
    _wait_grid_refreshed(bs, before)


def _combo_value(combo) -> str:
    try:
        return (combo.input_value() or "").strip()
    except PwError:
        return ""


def _grid_signature(bs) -> tuple:
    return tuple((r.get(S.COL_OBJECT_ID, ""), r.get(S.COL_REVISION, ""), r.get(S.COL_STATUS, ""))
                 for r in bs.read_grid(S.GRID_HEADER_OBJECT_ID))


def _wait_grid_refreshed(bs, before: tuple, timeout: float = 25):
    """Bluestar reloads the grid a moment AFTER the filter changes. Reading too early returns the rows
    of the previous filter, so wait until the rows changed and are stable, or are stable for ~4 s."""
    end = time.time() + timeout
    last, same = None, 0
    while time.time() < end:
        time.sleep(1)
        sig = _grid_signature(bs)
        same = same + 1 if sig == last else 0
        last = sig
        if (sig != before and same >= 1) or same >= 3:
            return
    bs.log.info("      Grid did not settle after the filter change; continuing with the current rows.")


def exact_rows(bs, serial: str) -> list[BluestarRow]:
    rows = []
    for r in bs.read_grid(S.GRID_HEADER_OBJECT_ID):
        oid = r.get(S.COL_OBJECT_ID, "").strip()
        if oid.upper() == serial.upper():
            rows.append(BluestarRow(object_id=oid, revision=r.get(S.COL_REVISION, ""),
                                    status=r.get(S.COL_STATUS, ""), name=r.get(S.COL_OBJECT_NAME, ""),
                                    index=r["_index"], raw=r))
    return rows


def rows_for_filter(bs, serial: str, label: str) -> list[BluestarRow]:
    set_revision_filter(bs, label)
    rows = exact_rows(bs, serial)
    bs.log.info(f"      '{label}': {len(rows)} exact row(s) "
                + ", ".join(f"{r.revision}/{r.status}" for r in rows))
    return rows


def choose_revision(bs, serial: str) -> tuple[BluestarRow | None, str]:
    """Returns (row, filter label that shows that row). Grid is left on that filter."""
    search_serial(bs, serial)
    latest = rows_for_filter(bs, serial, S.REV_LATEST)
    if latest:
        best = max(latest, key=lambda r: revision_key(r.revision))
        if best.approved:
            return best, S.REV_LATEST

    approved = [r for r in rows_for_filter(bs, serial, S.REV_ALL_APPROVED) if r.approved]
    if approved:
        return max(approved, key=lambda r: revision_key(r.revision)), S.REV_ALL_APPROVED

    if latest:                                   # 4.7 fallback: latest revision with any status
        rows = rows_for_filter(bs, serial, S.REV_LATEST)
        if rows:
            return max(rows, key=lambda r: revision_key(r.revision)), S.REV_LATEST

    every = rows_for_filter(bs, serial, S.REV_ALL)  # 4.8
    if every:
        return max(every, key=lambda r: revision_key(r.revision)), S.REV_ALL
    return None, ""


def reselect(bs, serial: str, row: BluestarRow, label: str) -> BluestarRow:
    """Make sure the chosen row is shown & selected (used before every action on it).
    Tries the filter it was found with first, then searches again, then the other filters."""
    others = [x for x in (S.REV_ALL_APPROVED, S.REV_LATEST, S.REV_ALL) if x != label]
    for attempt, lab in enumerate([label, label] + others):
        if attempt == 1:
            search_serial(bs, serial)
        set_revision_filter(bs, lab)
        for r in exact_rows(bs, serial):
            if r.revision == row.revision:
                bs.click_grid_cell(r.index, S.COL_REVISION, S.COL_STATUS, S.COL_OBJECT_NAME)
                if lab != label:
                    bs.log.info(f"      {serial} rev {row.revision} found under '{lab}'")
                return r
    raise StepFailed(f"Could not re-select {serial} revision {row.revision} in Bluestar")
