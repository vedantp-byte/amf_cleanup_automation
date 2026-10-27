"""Checkpoint-5 decision rules (pure Python, no Solid Edge calls).

  CAD component -> Engineer BOM / Occurrence property -> Action

1. Electrical components are identified first (STEP-11), motors flagged (STEP-7).
2. Level-1 (first level): Standard BOM is the authority (STEP-8/9).
     in BOM -> Keep | partial / name-only match -> Keep + review | not in BOM -> Hide
3. Nested levels: Reports/Parts List of Occurrence Properties (STEP-5/8/10).
     Yes -> Keep | No -> Hide | unknown -> Keep + review
4. Missing / empty file reference -> Hide (STEP-10).
5. Electrical override: anything rule 2/3 would hide but is electrical -> Keep (STEP-11);
   'possible' electrical -> flagged for review, never silently assumed.
6. Children of a hidden assembly are 'Hidden via parent' (no change needed).
"""

import difflib
import re

from .model import Node, part_number
from .references import BomLine


def _kw_regex(words: list[str]):
    words = sorted({w.strip().upper() for w in words if w and w.strip()}, key=len, reverse=True)
    if not words:
        return None
    alt = "|".join(re.escape(w).replace(r"\ ", r"[\s_\-]+") for w in words)
    return re.compile(rf"(?<![A-Z0-9])({alt})(?![A-Z0-9])")


def _norm_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).strip()


class Classifier:
    def __init__(self, cfg: dict):
        c = cfg["classification"]
        self.motor_re = _kw_regex(c["motor_keywords"])
        self.strong_re = _kw_regex(c["electrical_keywords_strong"])
        self.weak_re = _kw_regex(c["electrical_keywords_weak"])
        self.categories = [x.upper() for x in c["electrical_categories"]]

    # ------------------------------------------------------------ electrical
    @staticmethod
    def _own_text(n: Node) -> str:
        return " ".join([n.name, part_number(n.file), n.title, n.category, n.keywords, n.description]).upper()

    def _strong(self, n: Node) -> str:
        if n.category and any(c in n.category.upper() for c in self.categories):
            return f"category '{n.category}'"
        m = self.strong_re.search(self._own_text(n)) if self.strong_re else None
        return f"keyword '{m.group(1)}'" if m else ""

    def classify_electrical(self, nodes: list[Node]):
        for n in nodes:   # depth-first -> parent already classified
            n.electrical, n.electrical_reason = "No", ""
            hit = self._strong(n)
            if hit:
                n.electrical, n.electrical_reason = "Yes", hit
                continue
            p = n.parent
            if p is not None and p.electrical == "Yes":
                reason = p.electrical_reason if p.electrical_reason.startswith("via parent") else f"via parent {p.name}"
                n.electrical, n.electrical_reason = "Yes", reason
                continue
            m = self.weak_re.search(self._own_text(n)) if self.weak_re else None
            if m:
                n.electrical, n.electrical_reason = "Possible", f"weak keyword '{m.group(1)}'"

    def classify_motors(self, nodes: list[Node]):
        for n in nodes:
            text = " ".join([n.name, part_number(n.file), n.title, n.keywords, n.description]).upper()
            n.motor = bool(self.motor_re and self.motor_re.search(text))

    # -------------------------------------------------------------- matching
    @staticmethod
    def match_bom(n: Node, bom: list[BomLine]) -> tuple[str, str, str]:
        """(in_bom 'Yes'/'Ambiguous'/'No', bom item, note)."""
        keys = {k for k in (part_number(n.file or n.name), part_number(n.doc_number)) if k}
        by_key = {b.key: b for b in bom if b.key}
        for k in keys:
            if k in by_key:
                return "Yes", by_key[k].item_id, ""
        for k in keys:
            for bk, b in by_key.items():
                if len(k) >= 5 and len(bk) >= 5 and (k.startswith(bk) or bk.startswith(k)):
                    return "Ambiguous", b.item_id, f"part number '{k}' only partially matches BOM item '{b.item_id}'"
        title = _norm_name(n.title)
        if len(title) >= 6:
            for b in bom:
                if b.name and difflib.SequenceMatcher(None, title, _norm_name(b.name)).ratio() >= 0.9:
                    return "Ambiguous", b.item_id, f"title matches BOM item '{b.item_id}' but part number differs"
        return "No", "", ""

    # ------------------------------------------------------------- decisions
    def decide(self, nodes: list[Node], bom: list[BomLine] | None, draft_tokens: set[str] | None):
        self.classify_electrical(nodes)
        self.classify_motors(nodes)
        have_bom = bool(bom)
        for n in nodes:
            n.action = n.rule = n.reason = n.review = n.in_bom = n.bom_item = ""
        for n in nodes:   # depth-first order -> parents decided before children
            if draft_tokens is not None:
                n.in_draft = "Yes" if (n.part_number in draft_tokens
                                       or part_number(n.doc_number) in draft_tokens) else "No"
            else:
                n.in_draft = "N/A"

            hidden_parent = next((a for a in n.ancestors()
                                  if a.action.startswith("Hide") or a.action == "Hidden via parent"), None)
            if hidden_parent is not None:
                n.action, n.rule = "Hidden via parent", "parent"
                n.reason = f"Parent assembly hidden ({hidden_parent.name})"
                n.in_bom = "N/A (nested)" if n.level > 1 else n.in_bom
                if n.electrical == "Yes":
                    n.review = f"Electrical component inside hidden assembly {hidden_parent.name} - verify"
                continue

            if n.level == 1:
                if have_bom:
                    n.in_bom, n.bom_item, note = self.match_bom(n, bom)
                else:
                    n.in_bom, note = "BOM not available", ""
            else:
                n.in_bom, note = "N/A (nested)", ""

            if n.file_missing or not n.file:
                n.action, n.rule = "Hide", "missing"
                n.reason = "No valid occurrence/part reference (file missing)"
                if n.electrical == "Yes":
                    n.review = "Electrical component file is missing - manual check needed"
                continue

            if n.level == 1 and have_bom:
                if n.in_bom == "Yes":
                    n.action, n.rule, n.reason = "Keep", "bom", f"In standard BOM ({n.bom_item})"
                elif n.in_bom == "Ambiguous":
                    n.action, n.rule, n.reason = "Keep", "bom", "Uncertain BOM match - kept"
                    n.review = f"Ambiguous BOM match: {note}"
                else:
                    n.action, n.rule, n.reason = "Hide", "bom", "Not in standard BOM"
            else:
                prefix = "Standard BOM not available - " if n.level == 1 else ""
                if n.include_in_bom is True:
                    n.action, n.rule, n.reason = "Keep", "occurrence", prefix + "Reports/Parts List = Yes"
                elif n.include_in_bom is False:
                    n.action, n.rule, n.reason = "Hide", "occurrence", prefix + "Reports/Parts List = No"
                else:
                    n.action, n.rule, n.reason = "Keep", "occurrence", prefix + "Reports/Parts List unknown"
                    n.review = "Occurrence property could not be read - kept"
                if n.level == 1 and not have_bom:
                    n.review = (n.review + "; " if n.review else "") + "Standard BOM missing - decision from occurrence property"

            if n.action == "Hide" and n.rule in ("bom", "occurrence"):
                if n.electrical == "Yes":
                    n.action, n.rule = "Keep (electrical preserved)", "electrical"
                    n.reason += f"; electrical component preserved ({n.electrical_reason})"
                elif n.electrical == "Possible":
                    n.review = f"Possible electrical component hidden ({n.electrical_reason}) - verify"
            if n.level == 1 and n.action == "Hide" and n.in_draft == "Yes":
                n.review = (n.review + "; " if n.review else "") + "Not in standard BOM but found in 2D draft - verify"


def summarize(nodes: list[Node]) -> dict:
    hidden = [n for n in nodes if n.action.startswith("Hide") or n.action == "Hidden via parent"]
    electrical = [n for n in nodes if n.electrical in ("Yes", "Possible")]
    return {
        "n_analyzed": len(nodes),
        "n_hidden": len(hidden),
        "n_retained": len(nodes) - len(hidden),
        "n_hidden_missing": sum(1 for n in nodes if n.rule in ("missing", "occurrence") and n.action == "Hide"),
        "n_hidden_bom": sum(1 for n in nodes if n.rule == "bom" and n.action == "Hide"),
        "n_electrical": len(electrical),
        "n_electrical_kept": sum(1 for n in electrical if n.electrical == "Yes" and not n.effectively_hidden),
        "n_electrical_unhidden": sum(1 for n in nodes if n.rule == "electrical"),
        "n_motors": sum(1 for n in nodes if n.motor),
        "n_review": sum(1 for n in nodes if n.review),
    }
