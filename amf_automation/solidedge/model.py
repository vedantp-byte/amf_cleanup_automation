"""Process-2 STEP-2 / STEP-5: read the complete assembly hierarchy into Node objects.

Level-1 nodes wrap top-level Occurrence objects; deeper nodes wrap
SubOccurrence objects (visibility in the context of the top-level assembly).
'Reports/Parts List' (Occurrence Properties) = IncludeInBom of the occurrence
in its own (sub)assembly and not excluded from reports at top level.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .app import call0, get, items

_CAD_EXT = re.compile(r"\.(ASM|PAR|PSM|PWD)$", re.I)


def part_number(text: str) -> str:
    """'C:\\x\\40700092.ASM!LH' -> '40700092', '25071-1000DV.asm:1' -> '25071-1000DV'."""
    name = Path(str(text or "").replace("/", "\\").split("\\")[-1]).name if text else ""
    name = name.split("!")[0]
    name = re.sub(r":\d+$", "", name)
    name = _CAD_EXT.sub("", name)
    return name.strip().upper()


@dataclass(eq=False)          # identity comparison: every occurrence is unique
class Node:
    level: int
    name: str
    file: str
    handle: object = field(repr=False, default=None)
    parent: "Node | None" = field(repr=False, default=None)
    children: list = field(default_factory=list, repr=False)
    is_assembly: bool = False
    initially_visible: bool | None = None
    include_in_bom: bool | None = None      # Occurrence Properties -> Reports/Parts List
    file_missing: bool = False
    title: str = ""
    category: str = ""
    doc_number: str = ""
    keywords: str = ""
    description: str = ""
    # decisions (filled by classifier)
    in_bom: str = ""
    bom_item: str = ""
    in_draft: str = ""
    electrical: str = "No"
    electrical_reason: str = ""
    motor: bool = False
    action: str = ""
    rule: str = ""
    reason: str = ""
    review: str = ""
    applied: str = ""
    final_visible: bool | None = None

    @property
    def part_number(self) -> str:
        return part_number(self.file or self.name)

    @property
    def path(self) -> str:
        parts, n = [], self
        while n is not None:
            parts.append(n.name)
            n = n.parent
        return " / ".join(reversed(parts))

    @property
    def expected_visible(self) -> bool:
        return self.action.startswith("Keep")

    @property
    def effectively_hidden(self) -> bool:
        n = self
        while n is not None:
            if n.action.startswith("Hide") or n.action == "Hidden via parent":
                return True
            n = n.parent
        return False

    def ancestors(self):
        n = self.parent
        while n is not None:
            yield n
            n = n.parent


class PropertyCache:
    """File properties (Title, Category, Document Number ...) read once per file."""

    def __init__(self):
        self._cache = {}

    def read(self, doc, key: str) -> dict:
        key = (key or "").lower()
        if key in self._cache:
            return self._cache[key]
        out = {"title": "", "category": "", "doc_number": "", "keywords": "", "description": ""}
        if doc is not None:
            props = get(doc, "Properties")
            for set_name, prop, dest in (("SummaryInformation", "Title", "title"),
                                         ("SummaryInformation", "Keywords", "keywords"),
                                         ("SummaryInformation", "Subject", "description"),
                                         ("DocumentSummaryInformation", "Category", "category"),
                                         ("ProjectInformation", "Document Number", "doc_number")):
                try:
                    val = props.Item(set_name).Item(prop).Value
                    out[dest] = str(val or "").strip()
                except Exception:
                    pass
        self._cache[key] = out
        return out


def _fill(node: Node, doc, cache: PropertyCache):
    for k, v in cache.read(doc, node.file).items():
        setattr(node, k, v)


def build_tree(asm_doc, cache: PropertyCache, log=None) -> list[Node]:
    """Return a flat, depth-first list of every occurrence below the top-level assembly."""
    flat: list[Node] = []

    def add_sub(sub, parent: Node, level: int):
        node = Node(level=level, name=str(get(sub, "Name", "") or ""), handle=sub, parent=parent,
                    file=str(get(sub, "SubOccurrenceFileName", "") or ""))
        node.initially_visible = get(sub, "Visible")
        node.file_missing = bool(call0(sub, "FileMissing", False))
        node.is_assembly = bool(get(sub, "Subassembly", False))
        as_occ = get(sub, "ThisAsOccurrence")
        inc = get(as_occ, "IncludeInBom") if as_occ is not None else None
        excl = get(sub, "ExcludeFromReports")
        node.include_in_bom = None if inc is None else (bool(inc) and not bool(excl))
        if not node.file_missing:
            _fill(node, get(sub, "SubOccurrenceDocument"), cache)
        parent.children.append(node)
        flat.append(node)
        if node.is_assembly and not node.file_missing:
            for child in items(get(sub, "SubOccurrences")):
                add_sub(child, node, level + 1)

    for occ in items(get(asm_doc, "Occurrences")):
        node = Node(level=1, name=str(get(occ, "Name", "") or ""), handle=occ,
                    file=str(get(occ, "OccurrenceFileName", "") or ""))
        node.initially_visible = get(occ, "Visible")
        node.file_missing = bool(call0(occ, "FileMissing", False))
        node.is_assembly = bool(get(occ, "Subassembly", False))
        inc = get(occ, "IncludeInBom")
        node.include_in_bom = None if inc is None else bool(inc)
        if not node.file_missing:
            _fill(node, get(occ, "OccurrenceDocument"), cache)
        flat.append(node)
        if node.is_assembly and not node.file_missing:
            for sub in items(get(occ, "SubOccurrences")):
                add_sub(sub, node, 2)
        if log and len(flat) % 500 == 0:
            log.info(f"      ... {len(flat)} occurrences read")
    return flat


def tree_stats(nodes: list[Node]) -> dict:
    return {
        "total": len(nodes),
        "top_level": sum(1 for n in nodes if n.level == 1),
        "subassemblies": sum(1 for n in nodes if n.is_assembly),
        "parts": sum(1 for n in nodes if not n.is_assembly),
        "max_depth": max((n.level for n in nodes), default=0),
        "initially_hidden": sum(1 for n in nodes if n.initially_visible is False),
        "missing": sum(1 for n in nodes if n.file_missing),
    }
