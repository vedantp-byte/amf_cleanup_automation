"""Cleanup report per serial number (saved in the serial's working folder)."""

from pathlib import Path

import openpyxl

from .bom_compare import write_bom_comparison
from .excel import write_key_values, write_table
from .summary import _save

COMPONENT_HEADERS = [
    "BOM Level Depth", "Occurrence path", "Part number", "Title", "Category", "Included in Standard BOM",
    "Standard BOM Item", "Reports/Parts List (Occurrence Property)", "In 2D Draft", "Electrical Component",
    "Electrical Reason", "Motor (Red)", "File Missing", "Initially Visible", "Action Taken", "Reason",
    "Applied in Solid Edge", "Final Visible", "Need review or manual intervention", "File",
    "Motor Reason", "Motor Colour Result", "Protected", "Protected Reason", "Laser Sensor", "Laser Result",
]


def _yn(v):
    if v is None:
        return "Unknown"
    return "Yes" if v else "No"


def write_cleanup_report(path: Path, ctx: dict):
    """ctx keys: summary_pairs, nodes, missing_rows, checkpoint_rows, validation_rows, manual_rows."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    write_key_values(ws, ctx["summary_pairs"], title=f"Cleanup Report - {ctx['serial']}")

    ws = wb.create_sheet("Components")
    rows = []
    for n in ctx["nodes"]:
        action = n.action
        if n.motor:
            action += " + coloured red"
        rows.append([n.level, n.path, n.part_number, n.title, n.category, n.in_bom, n.bom_item,
                     _yn(n.include_in_bom), n.in_draft, n.electrical, n.electrical_reason, _yn(n.motor),
                     _yn(n.file_missing), _yn(n.initially_visible), action, n.reason, n.applied,
                     _yn(n.final_visible), n.review or "No", n.file,
                     n.motor_reason, n.motor_status, _yn(n.protected), n.protected_reason,
                     ("Laser beam" if n.laser_beam else n.laser), n.laser_status or n.laser_reason])
    write_table(ws, COMPONENT_HEADERS, rows,
                {"Included in Standard BOM", "Electrical Component", "Final Visible"},
                widths={"Occurrence path": 70, "Reason": 50, "Need review or manual intervention": 60, "File": 60})

    ws = wb.create_sheet("Motors")
    write_table(ws, ["BOM Level Depth", "Occurrence path", "Part number", "Title", "Category", "Identified by",
                     "Covered by motor assembly", "Face Override result", "Need manual action"],
                ctx.get("motor_rows", []), {"Need manual action"},
                widths={"Occurrence path": 70, "Identified by": 45, "Face Override result": 50})

    ws = wb.create_sheet("Laser Sensors")
    write_table(ws, ["Part file", "Occurrences", "Identified as", "Title", "Result", "Details"],
                ctx.get("laser_rows", []), {"Result"}, widths={"Details": 90, "Identified as": 50})

    ws = wb.create_sheet("Missing Files")
    write_table(ws, ["File name", "Occurrences", "Bluestar search", "Revision", "Bluestar status",
                     "Result", "Reports/Parts List", "Action / Manual step"], ctx["missing_rows"],
                {"Result"}, widths={"Action / Manual step": 70})

    ws = wb.create_sheet("Checkpoints")
    write_table(ws, ["Checkpoint", "Step", "Status", "Details"], ctx["checkpoint_rows"], {"Status"},
                widths={"Details": 100})

    ws = wb.create_sheet("Validation Checklist")
    write_table(ws, ["Area", "Check", "Result", "Details"], ctx["validation_rows"], {"Result"},
                widths={"Details": 90})

    ws = wb.create_sheet("Manual Actions")
    write_table(ws, ["What could not be done / needs review", "Why", "Affected components", "Manual action required"],
                ctx["manual_rows"], widths={"Affected components": 70, "Manual action required": 60})

    if "bom_lines" in ctx:                 # added last: Standard BOM vs 3D CAD 1st level
        write_bom_comparison(wb, ctx["bom_lines"], ctx["nodes"], ctx.get("bom_name", ""))
    _save(wb, path)
