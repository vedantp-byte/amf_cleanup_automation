"""Solid Edge actions for Checkpoints 1-7 (everything that changes the model)."""

import shutil
from pathlib import Path

from .app import call0, get, items, put
from .model import Node, part_number


# ------------------------------------------------------------- Checkpoint-1
def show_all(nodes: list[Node]) -> tuple[int, list[str]]:
    """Show every occurrence at every level (Show All on top level + all sub levels)."""
    changed, errors = 0, []
    for n in nodes:
        if get(n.handle, "Visible") is False:
            if put(n.handle, "Visible", True):
                changed += 1
            else:
                errors.append(n.path)
    return changed, errors


# ------------------------------------------------------------- Checkpoint-2
def missing_groups(nodes: list[Node]) -> dict[str, list[Node]]:
    """Missing file name (lower) -> occurrences using it (like the Repair Missing Files list)."""
    groups: dict[str, list[Node]] = {}
    for n in nodes:
        if n.file_missing:
            groups.setdefault(Path(n.file.split("!")[0]).name.lower() or n.name.lower(), []).append(n)
    return groups


def copy_into(src_folder: Path, dst_folder: Path) -> int:
    """Copy downloaded files next to the assembly (Solid Edge also searches the container folder)."""
    copied = 0
    for p in src_folder.rglob("*"):
        if p.is_file():
            target = dst_folder / p.name
            if not target.exists():
                shutil.copy2(p, target)
                copied += 1
    return copied


def repair_missing(group: list[Node], new_file: Path | None) -> int:
    """RecheckMissingFile on every occurrence, Replace (top level) if still missing. Returns repaired count."""
    repaired = 0
    for n in group:
        call0(n.handle, "RecheckMissingFile")
        still = bool(call0(n.handle, "FileMissing", True))
        if still and new_file is not None and n.level == 1:
            try:
                n.handle.Replace(str(new_file), True)
            except Exception:
                pass
            still = bool(call0(n.handle, "FileMissing", True))
        if not still:
            repaired += 1
            n.file_missing = False
    return repaired


# ------------------------------------------------------------- Checkpoint-3
def assembly_range(se, asm) -> tuple | None:
    typed = se.typed_assembly(asm)
    try:
        r = typed.Range()
        if isinstance(r, (tuple, list)) and len(r) >= 6:
            return tuple(float(x) for x in r[-6:])
    except Exception:
        pass
    return None


def check_orientation(se, asm, tolerance: float) -> tuple[str, str]:
    """Z must be 'up': the model's Z extent may not dominate its X/Y footprint.

    The model is never rotated automatically (standard process: report only).
    """
    rng = assembly_range(se, asm)
    if not rng:
        return "REVIEW", "Model range could not be read - verify Z-up manually"
    x0, y0, z0, x1, y1, z1 = rng
    dx, dy, dz = abs(x1 - x0) * 1000, abs(y1 - y0) * 1000, abs(z1 - z0) * 1000
    dims = f"X={dx:.0f} mm, Y={dy:.0f} mm, Z={dz:.0f} mm"
    if dz <= max(dx, dy) * tolerance:
        return "PASS", f"Z-up OK ({dims})"
    if dy <= max(dx, dz) * 0.5:
        return "FAIL", f"Model looks Y-up / lying on its side ({dims}); rotate so Z is up (manual)"
    return "REVIEW", f"Z extent is the largest dimension ({dims}); verify Z-up manually"


# ------------------------------------------------------------- Checkpoint-4
def use_part_style(nodes: list[Node], recursive: bool) -> tuple[int, list[str]]:
    done, errors = 0, []
    for n in nodes:
        if n.level == 1 or recursive:
            try:
                n.handle.PutStyleUsePartStyle()
                done += 1
            except Exception:
                errors.append(n.path)
    return done, errors


def _face_style(asm, name: str):
    styles = get(asm, "FaceStyles")
    for fs in items(styles):
        if str(get(fs, "Name", "")).strip().lower() == name.lower():
            return fs
    try:                                     # style not in document: create a pure red one
        fs = styles.Add(name, "")
        fs.SetDiffuse(1.0, 0.0, 0.0)
        return fs
    except Exception:
        return None


def color_motors(asm, nodes: list[Node], style_name: str) -> tuple[int, list[str]]:
    motors = [n for n in nodes if n.motor]
    if not motors:
        return 0, []
    fs = _face_style(asm, style_name)
    if fs is None:
        return 0, [f"Face style '{style_name}' not available"] + [m.path for m in motors]
    done, errors = 0, []
    for n in motors:
        if put(n.handle, "Style", fs) or put(n.handle, "FaceStyle", fs):
            done += 1
        else:
            errors.append(n.path)
    return done, errors


# ------------------------------------------------------------- Checkpoint-5
def apply_visibility(nodes: list[Node], rules: set[str] | None = None) -> tuple[int, list[str]]:
    """Set Visible for nodes decided Keep/Hide (optionally only for the given rules)."""
    changed, errors = 0, []
    for n in nodes:
        if n.action == "Hidden via parent":
            n.applied = "No change (parent hidden)"
            continue
        if rules is not None and n.rule not in rules:
            continue
        want = n.expected_visible
        if get(n.handle, "Visible") == want:
            n.applied = n.applied or "Already correct"
            continue
        if put(n.handle, "Visible", want):
            changed += 1
            n.applied = "Shown" if want else "Hidden"
        else:
            n.applied = "FAILED to change visibility"
            errors.append(n.path)
    return changed, errors


def verify_visibility(nodes: list[Node]) -> list[str]:
    bad = []
    for n in nodes:
        n.final_visible = get(n.handle, "Visible")
        if n.action == "Hidden via parent":
            continue
        if n.final_visible is not None and bool(n.final_visible) != n.expected_visible:
            bad.append(n.path)
    return bad


# ------------------------------------------------------------- Checkpoint-6
_DISPLAY_FLAGS = ["DisplayReferencePlanes", "DisplayCoordinateSystems", "DisplaySketches",
                  "DisplayDimensions", "DisplayAnnotations"]


def hide_reference_items(asm, nodes: list[Node]) -> dict:
    """Show/Hide All Components -> hide Reference Planes, Sketches, Coordinate Systems, PMI."""
    res = {"ok": [], "failed": []}

    def rec(label, ok):
        res["ok" if ok else "failed"].append(label)

    rec("Reference planes (top)", put(get(asm, "AsmRefPlanes"), "Visible", False))
    rec("Coordinate systems (top)", put(get(asm, "CoordinateSystems"), "Visible", False))
    rec("Sketches / layouts (top)", put(get(asm, "Layouts"), "Visible", False))
    sk3d_ok = True
    for sk in items(get(asm, "Sketches3D")):
        sk3d_ok &= put(sk, "Visible", False)
    rec("3D sketches (top)", sk3d_ok)
    pmi = get(asm, "PMI")
    rec("PMI (top)", pmi is not None and put(pmi, "Show", False)
        | put(pmi, "ShowDimensions", False) | put(pmi, "ShowAnnotations", False))

    occ_fail = 0
    for n in nodes:
        if n.effectively_hidden:
            continue
        for flag in _DISPLAY_FLAGS:
            if get(n.handle, flag) and not put(n.handle, flag, False):
                occ_fail += 1
    rec(f"Planes/sketches/coord. systems/PMI inside components ({occ_fail} errors)", occ_fail == 0)
    return res


# ------------------------------------------------------------- Checkpoint-7
def export_step(se, asm, step_path: Path) -> tuple[bool, str]:
    step_path.parent.mkdir(parents=True, exist_ok=True)
    if step_path.exists():
        step_path.unlink()
    errors = []
    for method in ("SaveAs", "SaveCopyAs"):
        try:
            getattr(asm, method)(str(step_path))
            se.idle(2)
        except Exception as e:
            errors.append(f"{method}: {e}")
            continue
        if step_path.exists() and step_path.stat().st_size > 0:
            with open(step_path, "rb") as fh:
                head = fh.read(64)
            if b"ISO-10303-21" in head:
                return True, f"{step_path.name} ({step_path.stat().st_size // 1024} KB) via {method}"
            return True, f"{step_path.name} written via {method} (header not ISO-10303-21 - verify)"
    return False, "STEP export failed: " + "; ".join(errors or ["file not created"])


def item_from_missing_name(file_name: str) -> str:
    return part_number(file_name)
