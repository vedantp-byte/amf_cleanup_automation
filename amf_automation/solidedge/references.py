"""Read the reference inputs for Checkpoint-5: Standard BOM (Excel) and 2D Draft (PDF)."""

import re
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from .model import part_number

_ITEM_HEADERS = ["item id", "item number", "item no", "item", "part number", "object id", "component", "number"]
_NAME_HEADERS = ["name", "item name", "object name", "description"]
_POS_HEADERS = ["position", "pos", "line", "line number", "find number"]
_REV_HEADERS = ["revision", "rev"]
_QTY_HEADERS = ["final quantity", "quantity", "qty", "bom qty"]


@dataclass
class BomLine:
    position: str
    item_id: str
    name: str
    revision: str
    quantity: str

    @property
    def key(self) -> str:
        return part_number(self.item_id)


def _find(header: list[str], candidates: list[str]) -> int | None:
    for cand in candidates:
        for i, h in enumerate(header):
            if h == cand:
                return i
    for cand in candidates:
        for i, h in enumerate(header):
            if h.startswith(cand):
                return i
    return None


def read_standard_bom(path: Path) -> list[BomLine]:
    """Standard BOM = first level only (as exported from Bluestar 'BOM lines')."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    header_row, header = None, []
    for i, row in enumerate(rows[:15]):
        cells = [str(c or "").strip().lower() for c in row]
        if _find(cells, _ITEM_HEADERS) is not None:
            header_row, header = i, cells
            break
    if header_row is None:
        raise ValueError(f"No 'Item ID' column found in {path.name}")

    ci = _find(header, _ITEM_HEADERS)
    cn, cp, cr, cq = (_find(header, h) for h in (_NAME_HEADERS, _POS_HEADERS, _REV_HEADERS, _QTY_HEADERS))

    def cell(row, idx):
        if idx is None or idx >= len(row) or row[idx] is None:
            return ""
        v = row[idx]
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()

    lines = []
    for row in rows[header_row + 1:]:
        item = cell(row, ci)
        if item:
            lines.append(BomLine(cell(row, cp), item, cell(row, cn), cell(row, cr), cell(row, cq)))
    return lines


def read_draft_tokens(pdf: Path) -> set[str]:
    """All part-number-like tokens in the 2D draft text (parts list / balloons)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return set()
    text = []
    reader = PdfReader(str(pdf))
    for page in reader.pages:
        try:
            text.append(page.extract_text() or "")
        except Exception:
            continue
    joined = "\n".join(text).upper()
    return set(re.findall(r"[A-Z0-9][A-Z0-9\-_.]{3,}[A-Z0-9]", joined))
