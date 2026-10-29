"""Checkpoint-5 decision rules (pure Python, no Solid Edge calls).

  CAD component -> Engineer BOM / Occurrence property -> Action

1. Electrical components are identified first (STEP-11), motors (STEP-7), protected
   components and laser sensors are classified before any hide rule.
2. Level-1 (first level): Standard BOM is the authority (STEP-8/9).
     in BOM -> Keep | partial / name-only match -> Keep + review | not in BOM -> Hide
3. Nested levels: Reports/Parts List of Occurrence Properties (STEP-5/8/10).
     Yes -> Keep | No -> Hide | unknown -> Keep + review
4. Missing / empty file reference -> Hide (STEP-10).
5. Overrides of rules 2/3: protected component (safety, stickers/labels, enclosures, operator
   interfaces, encoders, bolts/nuts/fasteners) -> Keep; electrical -> Keep (STEP-11);
   'possible' electrical -> flagged for review, never silently assumed.
6. An assembly that is hidden gets "Hide All": its children are 'Hidden via parent' and are not
   checked any further. (Option protect_inside_hidden_assemblies: an assembly that contains
   protected components is 'Partial hide' instead - protected children stay, the rest is hidden.)
7. Laser beam occurrences inside a laser sensor assembly -> Hide (laser beams inside laser
   sensor PARTS are deleted separately, see checkpoints.remove_laser_beam).
"""

import difflib
import re

from .model import Node, part_number
from .references import BomLine

HIDDEN_VIA_PARENT = "Hidden via parent"
PARTIAL_HIDE = "Partial hide (protected content)"

_RATING = re.compile(r"(?<![0-9.])\d+(?:\.\d+)?\s*(?:HP|KW)(?![A-Z])"
                     r"|(?<!\d)(?:208|220|230|380|400|415|460|480|575)\s*/\s*(?:380|400|415|460|480|575|690)(?!\d)")
_RATIO = re.compile(r"(?<![\d.])\d+(?:\.\d+)?\s*:\s*1(?![\d.])")


def _kw_regex(words: list[str]):
    words = sorted({w.strip().upper() for w in words if w and str(w).strip()}, key=len, reverse=True)
    if not words:
        return None
    alt = "|".join(re.escape(w).replace(r"\ ", r"[\s_\-]+") for w in words)
    return re.compile(rf"(?<![A-Z0-9])({alt})(?![A-Z0-9])")


def _beam_regex(words: list[str]):
    """Like _kw_regex but digits / underscores count as separators ('Laser_Beam_1', 'Beam2')."""
    words = sorted({w.strip().upper() for w in words if w and str(w).strip()}, key=len, reverse=True)
    if not words:
        return None
    alt = "|".join(re.escape(w).replace(r"\ ", r"[\s_\-]*") for w in words)
    return re.compile(rf"(?<![A-Z])({alt})(?![A-Z])")


def _norm_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).strip()


def leading_noun(n: Node) -> str:
    """Text before the first comma of the title ('GMT-DR 2HP, 230/460-60' -> 'GMT-DR 2HP').
    Falls back to the description, then the file name."""
    title = (n.title or n.description or "").strip()
    if title:
        return title.split(",")[0].strip().upper()
    return part_number(n.file or n.name)


def _norm_pn(pn: str) -> str:
    return str(pn or "").strip().upper().lstrip("0")


class Classifier:
    def __init__(self, cfg: dict):
        c = cfg["classification"]
        self.strong_re = _kw_regex(c["electrical_keywords_strong"])
        self.weak_re = _kw_regex(c["electrical_keywords_weak"])
        self.categories = [x.upper() for x in c["electrical_categories"]]
        # motors
        self.motor_re = _kw_regex(c["motor_keywords"])
        self.motor_ex_re = _kw_regex(c.get("motor_exclude_words", []))
        self.motor_cats = {x.strip().upper() for x in c.get("motor_categories", [])}
        self.motor_pns = {_norm_pn(x) for x in c.get("motor_part_numbers", []) if str(x).strip()}
        # protected components
        self.prot_cats = [x.strip().upper() for x in c.get("protected_categories", []) if str(x).strip()]
        self.prot_re = _kw_regex(c.get("protected_keywords", []))
        self.prot_title_re = _kw_regex(c.get("protected_title_words", []))
        self.prot_ex_re = _kw_regex(c.get("protected_exclude_words", []))
        self.protect_inside_hidden = bool(c.get("protect_inside_hidden_assemblies", False))
        # laser sensors
        self.laser_pns = {_norm_pn(x) for x in c.get("laser_part_numbers", []) if str(x).strip()}
        self.laser_re = _kw_regex(c.get("laser_keywords", ["LASER"]))
        self.laser_sensor_re = _kw_regex(c.get("laser_sensor_words", []))
        self.laser_cats = [x.strip().upper() for x in c.get("laser_sensor_categories", []) if str(x).strip()]
        self.laser_ex_re = _kw_regex(c.get("laser_exclude_words", []))
        self.beam_re = _beam_regex(c.get("laser_beam_keywords", []))

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

    # ---------------------------------------------------------------- motors
    def motor_reason(self, n: Node) -> str:
        """Why this occurrence is a motor ('' = not a motor). Several independent signals."""
        if self.motor_pns and _norm_pn(n.part_number) in self.motor_pns:
            return f"known motor part number {n.part_number}"
        lead = leading_noun(n)
        if self.motor_ex_re and self.motor_ex_re.search(lead):
            return ""                                   # accessory: mount, kit, cover, seal, hub ...
        m = self.motor_re.search(lead) if self.motor_re else None
        if m:
            return f"'{m.group(1)}' in title '{lead}'"
        title = (n.title or n.description or "").upper()
        cat = (n.category or "").strip().upper()
        if title and _RATING.search(title) and (cat in self.motor_cats or ("MOTOR" in cat and _RATIO.search(title))):
            return f"power rating in title '{title[:50]}' + category '{n.category}'"
        if not title and cat in self.motor_cats:
            return f"category '{n.category}' (no title)"
        return ""

    def classify_motors(self, nodes: list[Node]):
        """Every motor occurrence at every level. A motor inside a motor assembly is covered by the
        assembly's override (motor_covered_by) so the whole motor geometry is red exactly once."""
        seen = set()
        for n in nodes:   # depth-first -> parents first
            n.motor, n.motor_reason, n.motor_covered_by = False, "", ""
            key = id(n.handle) if n.handle is not None else id(n)
            if key in seen:
                continue
            seen.add(key)
            reason = self.motor_reason(n)
            if not reason:
                continue
            n.motor, n.motor_reason = True, reason
            top = next((a for a in reversed(list(n.ancestors())) if a.motor), None)
            if top is not None:
                n.motor_covered_by = top.name

    # ------------------------------------------------------------- protected
    def protected_reason(self, n: Node) -> str:
        cat = (n.category or "").upper()
        for c in self.prot_cats:
            if c in cat:
                return f"category '{n.category}'"
        title = re.sub(r"\s*,\s*", ", ", " ".join((n.title or n.description or "").upper().split()))
        m = self.prot_title_re.search(title) if (self.prot_title_re and title) else None
        if m:                                           # sensor related parts / explicit phrases, anywhere
            return f"'{m.group(1)}' in title"
        lead = leading_noun(n)
        if self.prot_ex_re and self.prot_ex_re.search(lead):
            return ""
        m = self.prot_re.search(lead) if self.prot_re else None
        return f"'{m.group(1)}' in title '{lead}'" if m else ""

    def classify_protected(self, nodes: list[Node]):
        for n in nodes:
            n.protected, n.protected_reason = False, ""
            reason = self.protected_reason(n)
            if not reason and n.parent is not None and n.parent.protected:
                p = n.parent
                reason = p.protected_reason if p.protected_reason.startswith("via parent") else f"via parent {p.name}"
            if reason:
                n.protected, n.protected_reason = True, reason

    # ---------------------------------------------------------------- lasers
    def laser_check(self, n: Node) -> tuple[str, str]:
        if self.laser_pns and _norm_pn(n.part_number) in self.laser_pns:
            return "Known", f"known laser sensor part number {n.part_number}"
        text = " ".join([n.name, part_number(n.file), n.title, n.description, n.keywords]).upper()
        m = self.laser_re.search(text) if self.laser_re else None
        if not m:
            return "", ""
        lead = leading_noun(n)
        if self.laser_ex_re and self.laser_ex_re.search(lead):
            return "", ""                                   # mount / bracket / reflector for a laser
        cat = (n.category or "").upper()
        s = self.laser_sensor_re.search(text) if self.laser_sensor_re else None
        if s or any(c in cat for c in self.laser_cats):
            why = f"sensor word '{s.group(1)}'" if s else f"category '{n.category}'"
            return "Yes", f"'LASER' + {why}"
        return "Possible", "mentions LASER but not confirmed as a sensor"

    def is_beam_name(self, *texts: str) -> str:
        if not self.beam_re:
            return ""
        for t in texts:
            m = self.beam_re.search(str(t or "").upper())
            if m:
                return m.group(1)
        return ""

    def classify_lasers(self, nodes: list[Node]):
        for n in nodes:
            n.laser, n.laser_reason, n.laser_beam = "", "", False
            sensor = next((a for a in n.ancestors() if a.laser in ("Known", "Yes")), None)
            if sensor is not None:
                hit = self.is_beam_name(n.name, part_number(n.file), n.title)
                if hit:
                    n.laser_beam = True
                    n.laser_reason = f"beam '{hit}' inside laser sensor {sensor.name}"
                continue
            n.laser, n.laser_reason = self.laser_check(n)

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
        self.classify_protected(nodes)
        self.classify_lasers(nodes)
        has_protected_below = self._protected_below(nodes) if self.protect_inside_hidden else {}
        have_bom = bool(bom)
        for n in nodes:
            n.action = n.rule = n.reason = n.review = n.in_bom = n.bom_item = ""
        for n in nodes:   # depth-first order -> parents decided before children
            if draft_tokens is not None:
                n.in_draft = "Yes" if (n.part_number in draft_tokens
                                       or part_number(n.doc_number) in draft_tokens) else "No"
            else:
                n.in_draft = "N/A"

            # Rule 6: Hide All on the parent -> children are not checked any further
            hidden_parent = next((a for a in n.ancestors()
                                  if a.action.startswith("Hide") or a.action == HIDDEN_VIA_PARENT), None)
            if hidden_parent is not None:
                n.action, n.rule = HIDDEN_VIA_PARENT, "parent"
                n.reason = f"Hide All on {hidden_parent.name} - not checked further"
                n.in_bom = "N/A (nested)" if n.level > 1 else n.in_bom
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

            # inside an assembly that is partially hidden because it holds protected components
            partial = n.parent if (n.parent is not None and n.parent.action == PARTIAL_HIDE) else None
            if partial is not None:
                if n.protected:
                    n.action, n.rule = "Keep (protected)", "protected"
                    n.reason = f"Protected component ({n.protected_reason}) inside hidden assembly {partial.name}"
                elif has_protected_below.get(id(n)):
                    n.action, n.rule = PARTIAL_HIDE, partial.rule
                    n.reason = f"Inside hidden assembly {partial.name}; contains protected components"
                else:
                    n.action, n.rule = "Hide", partial.rule
                    n.reason = f"Inside hidden assembly {partial.name}"
                continue

            if n.laser_beam:                               # Rule 7
                n.action, n.rule, n.reason = "Hide", "laser", f"Laser beam ({n.laser_reason})"
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

            if n.action == "Hide" and n.rule in ("bom", "occurrence"):   # Rule 5 overrides
                if n.protected:
                    n.action, n.rule = "Keep (protected)", "protected"
                    n.reason += f"; protected component - never hidden ({n.protected_reason})"
                elif n.electrical == "Yes":
                    n.action, n.rule = "Keep (electrical preserved)", "electrical"
                    n.reason += f"; electrical component preserved ({n.electrical_reason})"
                elif n.electrical == "Possible":
                    n.review = f"Possible electrical component hidden ({n.electrical_reason}) - verify"
            if (n.action == "Hide" and n.is_assembly and n.children
                    and self.protect_inside_hidden and has_protected_below.get(id(n))):
                n.action = PARTIAL_HIDE
                n.reason += "; contains protected components - non-protected content hidden individually"
            if n.level == 1 and n.action == "Hide" and n.in_draft == "Yes":
                n.review = (n.review + "; " if n.review else "") + "Not in standard BOM but found in 2D draft - verify"

    @staticmethod
    def _protected_below(nodes: list[Node]) -> dict:
        """id(node) -> True when a descendant is protected (computed bottom-up)."""
        out = {}
        for n in reversed(nodes):
            out[id(n)] = any(c.protected or out.get(id(c)) for c in n.children)
        return out


def summarize(nodes: list[Node]) -> dict:
    hidden = [n for n in nodes if n.action.startswith("Hide") or n.action == HIDDEN_VIA_PARENT]
    electrical = [n for n in nodes if n.electrical in ("Yes", "Possible")]
    motors = [n for n in nodes if n.motor]
    return {
        "n_analyzed": len(nodes),
        "n_hidden": len(hidden),
        "n_retained": len(nodes) - len(hidden),
        "n_hidden_missing": sum(1 for n in nodes if n.rule in ("missing", "occurrence") and n.action == "Hide"),
        "n_hidden_bom": sum(1 for n in nodes if n.rule == "bom" and n.action == "Hide"),
        "n_electrical": len(electrical),
        "n_electrical_kept": sum(1 for n in electrical if n.electrical == "Yes" and not n.effectively_hidden),
        "n_electrical_unhidden": sum(1 for n in nodes if n.rule == "electrical"),
        "n_motors": len(motors),
        "n_motors_red": sum(1 for n in motors if n.motor_status.startswith("Red")),
        "n_motors_failed": sum(1 for n in motors if n.motor_status.startswith("FAILED")),
        "n_protected": sum(1 for n in nodes if n.protected),
        "n_protected_kept": sum(1 for n in nodes if n.rule == "protected"),
        "n_hide_all": sum(1 for n in nodes if n.action == "Hide" and n.is_assembly and n.children),
        "n_laser": sum(1 for n in nodes if n.laser in ("Known", "Yes")),
        "n_laser_possible": sum(1 for n in nodes if n.laser == "Possible"),
        "n_review": sum(1 for n in nodes if n.review),
    }
