"""Data classes shared by every module."""

from dataclasses import dataclass, field
from datetime import datetime


class Status:
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"          # done, but needs attention / manual review
    SKIP = "SKIPPED"
    NA = "N/A"
    RUNNING = "RUNNING"
    PENDING = "PENDING"


# Every main step that is tracked as a flag (key, description).
PROCESS1_STEPS = [
    ("P1.S1", "Check GlobalProtect VPN connected"),
    ("P1.S2", "Check serial number (duplicate / invalid / already done)"),
    ("P1.S3", "Open Bluestar PLM"),
    ("P1.S4", "Search serial & select revision"),
    ("P1.S5", "Check out (download) Solid Edge 3D CAD"),
    ("P1.S6", "Download 2D Draft (PDF)"),
    ("P1.S7", "Download Standard BOM (Excel)"),
    ("P1.S8", "Copy to working folder (zip / move / unzip / delete zip)"),
]

PROCESS2_STEPS = [
    ("P2.S1", "Identify input files (ASM / BOM / PDF)"),
    ("P2.S2", "Open & inspect top-level assembly"),
    ("P2.S3", "Checkpoint-1: Show All"),
    ("P2.S4", "Checkpoint-2: Repair missing files"),
    ("P2.S5", "Read Occurrence Properties (Reports/Parts List)"),
    ("P2.S6", "Checkpoint-3: Z-axis up orientation"),
    ("P2.S7", "Checkpoint-4: Use Part Style + motors red"),
    ("P2.S8", "Checkpoint-5: Compare CAD vs BOM / 2D Draft / Occurrence"),
    ("P2.S9", "Checkpoint-5: Hide components not in standard BOM"),
    ("P2.S10", "Checkpoint-5: Hide components without valid occurrence/part"),
    ("P2.S11", "Checkpoint-5: Identify & preserve electrical components"),
    ("P2.S12", "Checkpoint-6: Final model validation"),
    ("P2.S13", "Checkpoint-6: Hide planes / sketches / coord. systems / PMI"),
    ("P2.S14", "Checkpoint-7: Export STEP"),
    ("P2.S15", "Write cleanup report"),
]


@dataclass
class StepFlag:
    key: str
    name: str
    status: str = Status.PENDING
    message: str = ""
    started: datetime | None = None
    finished: datetime | None = None


@dataclass
class SerialRecord:
    serial: str
    row: int
    validation: str = "Valid"
    steps: dict = field(default_factory=dict)      # key -> StepFlag
    data: dict = field(default_factory=dict)       # summary column values
    manual_actions: list = field(default_factory=list)
    started: datetime | None = None
    finished: datetime | None = None

    @property
    def is_valid(self) -> bool:
        return self.validation == "Valid"

    def set(self, **kw):
        self.data.update(kw)

    def add_manual(self, text: str):
        if text and text not in self.manual_actions:
            self.manual_actions.append(text)


@dataclass
class BluestarRow:
    """One line of the Bluestar engineering object grid."""
    object_id: str
    revision: str
    status: str
    name: str = ""
    index: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.status.strip().lower() == "approved"
