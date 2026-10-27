"""Process-1 STEP-2: read machine_ids.xlsx and sort out duplicate / invalid / already-done serials."""

import re
from pathlib import Path

import openpyxl

from .models import SerialRecord

_VALID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.!]{2,59}$")


def _to_text(value) -> str:
    """Excel gives 86216140 as int/float -> '86216140' (never '86216140.0')."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_serials(path: Path, column_candidates: list[str]) -> tuple[list[tuple[int, str]], str]:
    """Return [(excel_row, serial_text)] and the column header that was used."""
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return [], ""

    header = [_to_text(h) for h in rows[0]]
    wanted = [c.strip().lower() for c in column_candidates]
    col, col_name = None, ""
    for cand in wanted:
        for i, h in enumerate(header):
            if h.lower().replace(" ", "_") == cand.replace(" ", "_"):
                col, col_name = i, h
                break
        if col is not None:
            break

    start = 1
    if col is None:
        # No known header: first column; if first cell already looks like a serial keep it.
        col = 0
        col_name = header[0] if header else "column A"
        if header and _VALID.match(header[0] or "") and any(ch.isdigit() for ch in header[0]):
            start, col_name = 0, "column A (no header)"

    out = []
    for idx, row in enumerate(rows[start:], start=start + 1):
        if col >= len(row):
            continue
        text = _to_text(row[col])
        if text:
            out.append((idx, text))
    return out, col_name


def validate_serials(raw: list[tuple[int, str]], completed: dict[str, str], skip_completed: bool) -> list[SerialRecord]:
    """Mark Duplicate / Invalid / Already processed. Only 'Valid' records continue."""
    records, first_row = [], {}
    for row, text in raw:
        serial = text.strip()
        rec = SerialRecord(serial=serial, row=row)
        key = serial.upper()
        if not _VALID.match(serial):
            reason = "contains spaces" if " " in serial else "invalid characters or length"
            rec.validation = f"Invalid ({reason})"
        elif key in first_row:
            rec.validation = f"Duplicate of row {first_row[key]}"
        elif skip_completed and key in completed:
            rec.validation = f"Already processed ({completed[key]})"
        if key not in first_row:
            first_row[key] = row
        records.append(rec)
    return records


def completed_serials(history: list[dict]) -> dict[str, str]:
    """serial(upper) -> 'cycle id' for serials that already produced a STEP (SUCCESS / SUCCESS - REVIEW)."""
    done = {}
    for h in history:
        if str(h.get("overall_status", "")).upper().startswith("SUCCESS"):
            done[str(h.get("serial", "")).upper()] = h.get("cycle_id", "")
    return done
