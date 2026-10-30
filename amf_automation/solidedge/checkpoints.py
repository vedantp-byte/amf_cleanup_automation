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


def _style_name(fs) -> str:
    """Solid Edge FaceStyle exposes its name as StyleName (not Name)."""
    return str(get(fs, "StyleName", "") or get(fs, "Name", "") or "").strip()


def _face_style(asm, name: str):
    styles = get(asm, "FaceStyles")
    for fs in items(styles):
        if _style_name(fs).lower() == name.lower():
            return fs
    try:                                     # style not in document: create a pure red one
        fs = styles.Add(name, "")
        fs.SetDiffuse(1.0, 0.0, 0.0)
        return fs
    except Exception:
        for fs in items(styles):             # Add can fail when the style already exists
            if _style_name(fs).lower() == name.lower():
                return fs
    return None


def _override_name(handle) -> str:
    for prop in ("Style", "FaceStyle"):
        name = _style_name(get(handle, prop))
        if name:
            return name
    return ""


def color_motors(se, asm, nodes: list[Node], style_name: str) -> tuple[int, list[str]]:
    """Face Override (occurrence style) = Red on every motor occurrence, verified by reading it back.

    The target list is built completely first. The override is set per occurrence through the API
    (no GUI selection), so a motor assembly is coloured as a whole and motors inside it are covered.
    Shared part files are not touched (the override lives in the top-level assembly).
    """
    motors = [n for n in nodes if n.motor]
    if not motors:
        return 0, []
    try:
        asm.SelectSet.RemoveAll()            # clear stale selections
    except Exception:
        pass
    fs = _face_style(asm, style_name)
    if fs is None:
        for n in motors:
            n.motor_status = f"FAILED: face style '{style_name}' not available"
        return 0, [f"Face style '{style_name}' not available"] + [m.path for m in motors]

    targets = [n for n in motors if not n.motor_covered_by]
    done, errors = 0, []
    for n in targets:
        ok = False
        for attempt in range(2):            # retry once (CAD timing)
            put(n.handle, "Style", fs) or put(n.handle, "FaceStyle", fs)
            if attempt:
                se.idle(0.5)
            if _override_name(n.handle).lower() == style_name.lower():
                ok = True
                break
        if ok:
            done += 1
            n.motor_status = f"Red (verified: face override '{style_name}')"
        else:
            n.motor_status = f"FAILED: override not applied (reads '{_override_name(n.handle) or 'none'}')"
            errors.append(n.path)
    by_name = {t.name: t for t in targets}
    for n in motors:
        if n.motor_covered_by:
            parent = next((a for a in n.ancestors() if a.name == n.motor_covered_by), by_name.get(n.motor_covered_by))
            ok = parent is not None and parent.motor_status.startswith("Red")
            n.motor_status = (f"Red via motor assembly {n.motor_covered_by}" if ok
                              else f"FAILED: motor assembly {n.motor_covered_by} not coloured")
            if ok:
                done += 1
            else:
                errors.append(n.path)
    return done, errors


# ------------------------------------------------------------- Checkpoint-5
def hide_all(n: Node) -> list[str]:
    """'Hide All' on an assembly: hide the assembly and every child at every level, then verify."""
    errors = []
    kids = list(n.descendants())
    for d in kids:
        if get(d.handle, "Visible") is not False and not put(d.handle, "Visible", False):
            errors.append(d.path)
        d.applied = f"Hidden by Hide All of {n.name}"
    if get(n.handle, "Visible") is not False and not put(n.handle, "Visible", False):
        errors.append(n.path)
    still = [d.path for d in [n] + kids if get(d.handle, "Visible") is True]   # verify
    for p in still:
        if p not in errors:
            errors.append(p)
    n.applied = (f"Hidden (Hide All: assembly + {len(kids)} child occurrence(s))" if not errors
                 else f"Hide All INCOMPLETE ({len(errors)} occurrence(s) still visible)")
    return errors


def apply_visibility(nodes: list[Node], rules: set[str] | None = None) -> tuple[int, list[str]]:
    """Set Visible for nodes decided Keep/Hide (optionally only for the given rules).
    Assemblies to hide get 'Hide All'; their children were not checked and are skipped here."""
    changed, errors = 0, []
    for n in nodes:
        if n.action == "Hidden via parent":
            continue                          # handled by the parent's Hide All
        if rules is not None and n.rule not in rules:
            continue
        want = n.expected_visible
        if not want and n.is_assembly and n.children:
            errs = hide_all(n)
            changed += 1
            errors.extend(errs)
            continue
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
            parent_hidden = any(a.action == "Hide" for a in n.ancestors())
            if parent_hidden and n.final_visible is True:      # Hide All must have hidden it
                bad.append(n.path)
            continue
        if n.final_visible is not None and bool(n.final_visible) != n.expected_visible:
            bad.append(n.path)
    return bad


# --------------------------------------------------- Laser beam removal (parts)
def _obj_name(obj) -> str:
    for prop in ("EdgebarName", "Name", "DisplayName", "BodyName", "SystemName"):
        v = get(obj, prop)
        if v:
            return str(v)
    return ""


def _beam_candidates(part, is_beam) -> list[tuple[str, str, object]]:
    """(kind, name, object) of features / bodies / constructions whose name is beam-like."""
    found, seen = [], set()
    for f in items(get(part, "DesignEdgebarFeatures")):
        name = _obj_name(f)
        if name and is_beam(name) and name not in seen:
            seen.add(name)
            found.append(("feature", name, f))
    models = list(items(get(part, "Models")))
    if len(models) > 1:                       # multi-body part: a separate beam body
        for m in models:
            names = [str(get(m, p) or "") for p in ("Name", "BodyName", "DisplayName", "EdgebarName")]
            hit = next((x for x in names if x and is_beam(x)), "")
            if hit and hit not in seen:
                seen.add(hit)
                found.append(("body", hit, m))
    for c in items(get(part, "Constructions")):
        name = _obj_name(c)
        if name and is_beam(name) and name not in seen:
            seen.add(name)
            found.append(("construction", name, c))
    return found


def _body_present(part) -> bool:
    return any(get(m, "Body") is not None for m in items(get(part, "Models")))


def _feature_names(part, limit: int = 25) -> list[str]:
    return [n for n in (_obj_name(f) for f in items(get(part, "DesignEdgebarFeatures"))) if n][:limit]


def remove_laser_beam(se, path: Path, is_beam, backup_dir: Path) -> dict:
    """Open one laser sensor part separately, delete only the beam geometry, verify, save."""
    out = {"status": "", "removed": [], "features": [], "message": ""}
    if path.suffix.lower() not in (".par", ".psm"):
        out.update(status="Not a part file", message=path.name)
        return out
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / path.name
    if not backup.exists():
        shutil.copy2(path, backup)
    part = se.open(path)
    saved = False
    try:
        cands = _beam_candidates(part, is_beam)
        out["features"] = _feature_names(part)
        if not cands:
            out.update(status="No beam identified",
                       message="No feature/body named like a laser beam - not modified; check manually")
            return out
        if not _body_present(part):
            out.update(status="FAILED", message="Sensor body not found before deletion - not modified")
            return out
        errors = []
        for kind, name, obj in reversed(cands):
            try:
                if kind == "body":            # delete every feature that builds the beam body
                    for f in reversed(list(items(get(obj, "Features")))):
                        f.Delete()
                else:
                    obj.Delete()
                out["removed"].append(f"{kind} '{name}'")
            except Exception as e:
                errors.append(f"{kind} '{name}': {e}")
        se.idle(1)
        left = [n for _k, n, _o in _beam_candidates(part, is_beam)]
        if errors or left or not _body_present(part):
            out.update(status="FAILED",
                       message=f"Verification failed (errors: {errors or '-'}, beam left: {left or '-'}, "
                               f"body present: {_body_present(part)}) - changes NOT saved")
            return out
        part.Save()
        saved = True
        out.update(status="Beam removed", message=f"{', '.join(out['removed'])}; backup: {backup}")
        return out
    except Exception as e:
        out.update(status="FAILED", message=f"{e} - changes NOT saved")
        return out
    finally:
        se.close(part, save=False)           # unsaved changes are discarded -> file stays original
        out["saved"] = saved


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
