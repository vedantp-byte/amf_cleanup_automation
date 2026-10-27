"""Download any single item (part / assembly) from Bluestar.

Used by Process-2 STEP-4.4 to fetch missing files with the same STEP-3 to
STEP-5 logic as the main serial download.
"""

from pathlib import Path

from ..tracker import CycleAbort
from .checkout import checkout_selected
from .search import choose_revision, reselect


def download_item(bs, item: str, dest_root: Path) -> dict:
    """Returns {'status', 'file', 'revision', 'bluestar_status', 'message'}."""
    out = {"status": "", "file": None, "revision": "", "bluestar_status": "", "message": ""}
    try:
        bs.open_home(new_tab=False)
        row, label = choose_revision(bs, item)
        if row is None:
            out.update(status="Not Available in Bluestar", message="No entry found under any revision filter")
            return out
        reselect(bs, item, row, label)
        dest = dest_root / item / item
        res = checkout_selected(bs, dest, item, expect_asm=False)
        out.update(status="Downloaded", file=res["top_file"], revision=row.revision,
                   bluestar_status=row.status, message=f"{res['files']} file(s) in {dest}")
    except CycleAbort:
        raise
    except Exception as e:
        out.update(status="Failed", message=str(e))
    return out
