"""Process 2 - Machine clean up and STEP export in Solid Edge (STEP-1 ... STEP-14) for one serial."""

from collections import defaultdict
from pathlib import Path

from .bluestar.checkout import find_top_asm
from .bluestar.item import download_item
from .models import PROCESS2_STEPS, SerialRecord, Status
from .reports.cleanup import write_cleanup_report
from .solidedge import checkpoints as cp
from .solidedge.app import call0
from .solidedge.classifier import Classifier, summarize
from .solidedge.model import PropertyCache, build_tree, part_number, tree_stats
from .solidedge.references import read_draft_tokens, read_standard_bom
from .tracker import CycleAbort, StepFailed
from .utils import iso, now


def _flag_to_cp(*flags) -> str:
    statuses = [f.status for f in flags if f is not None]
    if not statuses:
        return ""
    if Status.FAIL in statuses:
        return "FAIL"
    if Status.WARN in statuses:
        return "WARN"
    if all(s in (Status.SKIP, Status.NA) for s in statuses):
        return "SKIPPED"
    return "PASS"


def find_standard_bom(work: Path) -> Path | None:
    return next((p for p in work.glob("*.xlsx") if "bom" in p.name.lower()), None)


def interlock(rec: SerialRecord, work: Path, process1_ran: bool) -> list[str]:
    """Process 2 may only start when the Standard BOM and the 3D CAD were downloaded.
    Returns the list of blocking problems (empty = OK). The 2D Draft is optional."""
    from .bluestar.bom import count_bom_lines
    sn = rec.serial
    problems = []
    if process1_ran and not str(rec.data.get("download_status", "")).startswith("Downloaded"):
        problems.append(f"3D CAD not downloaded ({rec.data.get('download_status') or 'unknown'})")
    elif find_top_asm(work / sn, sn) is None:
        problems.append(f"3D CAD {sn}.asm not found in {work / sn}")
    bom_status = str(rec.data.get("bom_status", ""))
    bom = find_standard_bom(work) if work.exists() else None
    if process1_ran and bom_status != "Downloaded":
        problems.append(f"Standard BOM not downloaded ({bom_status or 'unknown'})")
    elif bom is None:
        problems.append(f"Standard BOM (BOM lines_{sn}.xlsx) not found in {work}")
    elif count_bom_lines(bom) <= 0:
        problems.append(f"Standard BOM {bom.name} has no lines")
    return problems


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except (OSError, ValueError):
        return False


class Process2:
    def __init__(self, cfg: dict, tracker, se, bs_provider, log):
        self.cfg = cfg
        self.scfg = cfg["solidedge"]
        self.tracker = tracker
        self.se = se
        self.bs_provider = bs_provider        # callable -> started BluestarSession (for missing files)
        self.log = log
        self.classifier = Classifier(cfg)

    # ------------------------------------------------------------------ main
    def run(self, rec: SerialRecord) -> bool:
        sn = rec.serial
        t = self.tracker
        work = Path(rec.data.get("working_folder") or (self.cfg["resolved"]["working_root"] / sn))
        asm_dir = work / sn
        r = {"serial": sn, "nodes": [], "missing_rows": [], "checkpoint_rows": [], "validation_rows": [],
             "manual_rows": [], "laser_rows": [], "asm": None, "bom": None, "pdf": None, "orientation": ("", ""),
             "bom_lines": None, "step": None}
        doc = None
        ok = False
        try:
            # ---------------------------------------------------------- STEP-1
            with t.step(rec, "P2.S1", "Identify input files (ASM / BOM / PDF)") as st:
                asm = find_top_asm(asm_dir, sn) or next(
                    (p for p in work.rglob("*") if p.suffix.lower() == ".asm" and p.stem.upper() == sn.upper()), None)
                if asm is None:
                    asms = [p for p in asm_dir.glob("*") if p.suffix.lower() == ".asm"] if asm_dir.exists() else []
                    if len(asms) == 1:
                        asm = asms[0]
                    else:
                        st.fail(f"Top-level assembly {sn}.asm not found in {asm_dir}")
                bom = find_standard_bom(work)
                pdfs = [p for p in work.glob("*") if p.suffix.lower() == ".pdf"]
                pdf = next((p for p in pdfs if p.stem.upper() == sn.upper()), pdfs[0] if pdfs else None)
                r.update(asm=asm, bom=bom, pdf=pdf)
                msg = f"ASM={asm.name} | BOM={bom.name if bom else 'MISSING'} | PDF={pdf.name if pdf else 'MISSING (optional)'}"
                if bom is None:                 # clean-up depends on the Standard BOM
                    st.fail("Standard BOM missing - clean-up not possible. " + msg)
                st.warn(msg) if pdf is None else st.ok(msg)

            # ---------------------------------------------------------- STEP-2
            with t.step(rec, "P2.S2", "Open & inspect top-level assembly") as st:
                if self.se.app is None:
                    self.se.connect()
                doc = self.se.open(r["asm"])
                nodes = r["nodes"] = self._read_tree(doc)
                s = tree_stats(nodes)
                rec.set(top_assembly=r["asm"].name)
                st.ok(f"{s['top_level']} top-level, {s['subassemblies']} subassemblies, {s['parts']} parts, "
                      f"depth {s['max_depth']}, {s['initially_hidden']} hidden, {s['missing']} missing")

            # ---------------------------------------------------------- STEP-3 / CP-1
            with t.step(rec, "P2.S3", "Checkpoint-1: Show All") as st:
                changed, errors = cp.show_all(nodes)
                if errors:
                    st.warn(f"{changed} shown, {len(errors)} could not be shown")
                    self._manual(r, "Show All incomplete", "Solid Edge refused visibility change", errors,
                                 "Use Show All on these occurrences manually")
                else:
                    st.ok(f"{changed} hidden occurrence(s) made visible (all levels)")

            # ---------------------------------------------------------- STEP-4 / CP-2
            with t.step(rec, "P2.S4", "Checkpoint-2: Repair missing files", critical=False) as st:
                nodes = r["nodes"] = self._repair_missing(rec, r, doc, nodes, st)

            # ---------------------------------------------------------- STEP-5
            with t.step(rec, "P2.S5", "Read Occurrence Properties (Reports/Parts List)") as st:
                yes = sum(1 for n in nodes if n.include_in_bom is True)
                no = sum(1 for n in nodes if n.include_in_bom is False)
                unk = sum(1 for n in nodes if n.include_in_bom is None)
                msg = f"Reports/Parts List: Yes={yes}, No={no}, Unknown={unk}"
                st.warn(msg) if unk else st.ok(msg)

            # ---------------------------------------------------------- STEP-6 / CP-3
            with t.step(rec, "P2.S6", "Checkpoint-3: Z-axis up orientation", critical=False) as st:
                status, msg = cp.check_orientation(self.se, doc, float(self.scfg["orientation_tolerance"]))
                r["orientation"] = (status, msg)
                rec.set(orientation=status)
                if status == "PASS":
                    st.ok(msg)
                else:
                    st.warn(msg)
                    self._manual(r, "Orientation deflection", msg, [r["asm"].name],
                                 "Verify / correct orientation so that Z = up (not rotated automatically)")

            # ---------------------------------------------------------- STEP-7 / CP-4
            with t.step(rec, "P2.S7", "Checkpoint-4: Use Part Style + motors red", critical=False) as st:
                self.classifier.classify_motors(nodes)
                done, errs = cp.use_part_style(nodes, bool(self.scfg["apply_part_style_recursive"]))
                motors = [n for n in nodes if n.motor]
                for n in motors:
                    st.info(f"Motor: {n.path}  ({n.motor_reason})")
                m_done, m_errs = cp.color_motors(self.se, doc, nodes, self.scfg["motor_face_style"])
                rec.set(n_motors=len(motors), n_motors_red=m_done, n_motors_failed=len(motors) - m_done)
                msg = (f"Use Part Style on {done} occurrence(s); {len(motors)} motor(s) identified, "
                       f"{m_done} red (verified)")
                if errs:
                    self._manual(r, "Use Part Style incomplete", "Solid Edge style change failed", errs,
                                 "Select top-level assembly > View > Face Overrides > (Use Part Style)")
                if m_errs:
                    self._manual(r, "Motor not coloured red", "Face override could not be applied / verified",
                                 m_errs, "Select the motor > View > Face Overrides > Red")
                if errs or m_errs:
                    st.warn(msg + f"; {len(errs) + len(m_errs)} error(s)")
                else:
                    st.ok(msg)

            # ---------------------------------------------------------- STEP-8 / CP-5
            with t.step(rec, "P2.S8", "Checkpoint-5: Compare CAD vs BOM / 2D Draft / Occurrence") as st:
                bom_lines, tokens, notes = self._references(r)
                self.classifier.decide(nodes, bom_lines, tokens)
                sums = summarize(nodes)
                rec.set(**sums)
                lvl1 = [n for n in nodes if n.level == 1]
                msg = (f"Level-1: {sum(n.in_bom == 'Yes' for n in lvl1)} in BOM, "
                       f"{sum(n.in_bom == 'No' for n in lvl1)} not in BOM, "
                       f"{sum(n.in_bom == 'Ambiguous' for n in lvl1)} ambiguous; "
                       f"electrical {sums['n_electrical']}; review {sums['n_review']}")
                if notes:
                    msg += " | " + "; ".join(notes)
                st.warn(msg) if (notes or sums["n_review"]) else st.ok(msg)

            # ---------------------------------------------------------- STEP-9..11 / CP-5
            with t.step(rec, "P2.S9", "Checkpoint-5: Hide components not in standard BOM") as st:
                self._apply(st, r, nodes, {"bom"}, "standard BOM rule")
            with t.step(rec, "P2.S10", "Checkpoint-5: Hide components without valid occurrence/part") as st:
                self._apply(st, r, nodes, {"occurrence", "missing"}, "occurrence/part rule")
            with t.step(rec, "P2.S11", "Checkpoint-5: Identify & preserve electrical components") as st:
                self._apply(st, r, nodes, {"electrical"}, "electrical rule", quiet=True)
                sums = summarize(nodes)
                rec.set(**sums)
                possible = sum(1 for n in nodes if n.electrical == "Possible")
                msg = (f"{sums['n_electrical']} electrical identified ({possible} possible), "
                       f"{sums['n_electrical_unhidden']} preserved against hide rules")
                st.warn(msg) if possible else st.ok(msg)

            with t.step(rec, "P2.S11A", "Checkpoint-5: Keep protected components "
                                        "(safety / labels / fasteners / enclosures / OI / encoders)") as st:
                self._apply(st, r, nodes, {"protected"}, "protected rule")
                sums = summarize(nodes)
                rec.set(**sums)
                hidden_prot = [n.path for n in nodes if n.protected and n.expected_visible is False
                               and n.action != "Hidden via parent" and not n.file_missing and not n.laser_beam]
                msg = (f"{sums['n_protected']} protected component(s) identified, {sums['n_protected_kept']} kept "
                       f"visible against hide rules, {sums['n_hide_all']} assembly(ies) hidden with Hide All")
                if hidden_prot:
                    st.warn(msg + f"; {len(hidden_prot)} protected still hidden")
                    self._manual(r, "Protected component hidden", "Rule conflict", hidden_prot,
                                 "Show these components manually")
                else:
                    st.ok(msg)

            with t.step(rec, "P2.S11B", "Laser sensors: remove laser beam", critical=False) as st:
                self._lasers(rec, r, doc, nodes, work, st)

            # ---------------------------------------------------------- STEP-12 / CP-6
            with t.step(rec, "P2.S12", "Checkpoint-6: Final model validation", critical=False) as st:
                self._validate(rec, r, nodes, st)

            # ---------------------------------------------------------- STEP-13 / CP-6
            with t.step(rec, "P2.S13", "Checkpoint-6: Hide planes / sketches / coord. systems / PMI",
                        critical=False) as st:
                res = cp.hide_reference_items(doc, nodes)
                for item in res["ok"]:
                    r["checkpoint_rows"].append(["Checkpoint-6", item, "PASS", "hidden"])
                for item in res["failed"]:
                    r["checkpoint_rows"].append(["Checkpoint-6", item, "WARN", "could not be hidden by API"])
                if res["failed"]:
                    st.warn("Not hidden: " + "; ".join(res["failed"]))
                    self._manual(r, "Reference elements not hidden", "Not exposed / refused by Solid Edge API",
                                 res["failed"], "Right click viewport > Show/Hide All Components > Hide All for "
                                                "Reference Planes, Sketches, Coordinate Systems, PMI")
                else:
                    st.ok("Reference planes, sketches, coordinate systems and PMI hidden")

            # ---------------------------------------------------------- STEP-14 / CP-7
            with t.step(rec, "P2.S14", "Checkpoint-7: Export STEP") as st:
                if self.scfg["save_cleaned_assembly"]:
                    try:
                        doc.Save()
                        st.info("Cleaned assembly saved in working folder.")
                    except Exception as e:
                        st.info(f"Cleaned assembly could not be saved ({e}); continuing with STEP export.")
                step = work / f"{sn}{self.scfg['step_file_extension']}"
                good, msg = cp.export_step(self.se, doc, step)
                if not good:
                    rec.set(step_available="NO")
                    st.fail(msg)
                r["step"] = step
                rec.set(step_available="YES", step_path=str(step))
                st.ok(msg)
            ok = True

        except CycleAbort:
            raise
        except StepFailed as e:
            rec.set(error=str(e))
            self.tracker.skip_remaining(rec, PROCESS2_STEPS[:-1], "Previous step failed")
        finally:
            if "step_available" not in rec.data:
                rec.set(step_available="NO")
            self._finish_flags(rec)
            with t.step(rec, "P2.S15", "Write cleanup report", critical=False) as st:
                path = work / f"Cleanup_Report_{sn}.xlsx"
                self._write_report(rec, r, path)
                rec.set(cleanup_report=str(path))
                st.ok(str(path))
            if doc is not None and self.scfg["close_document_after_serial"]:
                self.se.close(doc, save=False)
        rec.set(p2_status="FAIL" if not ok else
                ("WARN" if any(f.status in (Status.WARN, Status.FAIL) for k, f in rec.steps.items()
                               if k.startswith("P2")) else "PASS"))
        return ok

    # -------------------------------------------------------------- helpers
    def _read_tree(self, doc):
        self.log.info("      Reading assembly structure (all levels) ...")
        return build_tree(doc, PropertyCache(), self.log)

    def _repair_missing(self, rec, r, doc, nodes, st):
        groups = cp.missing_groups(nodes)
        rec.set(missing_found=len(groups), missing_repaired=0, missing_not_in_bluestar=0)
        if not groups:
            st.ok("This assembly does not have any missing files")
            r["checkpoint_rows"].append(["Checkpoint-2", "Repair Missing Files", "PASS", "No missing files"])
            return nodes

        asm_dir = r["asm"].parent
        repaired_total, not_in_bs, manual = 0, 0, []
        for fname, group in groups.items():
            item = part_number(fname)
            include = any(n.include_in_bom is not False for n in group)
            dl = {"status": "not attempted", "file": None, "revision": "", "bluestar_status": "", "message": ""}
            new_file = None
            if self.scfg["download_missing_from_bluestar"] and self.bs_provider:
                st.info(f"Missing '{fname}' ({len(group)}x) -> searching Bluestar for {item} ...")
                bs = self.bs_provider()
                dl = download_item(bs, item, self.cfg["resolved"]["myloadpoint"])
                if dl["status"] == "Downloaded" and dl["file"]:
                    cp.copy_into(Path(dl["file"]).parent, asm_dir)
                    new_file = asm_dir / Path(dl["file"]).name
                elif dl["status"].startswith("Not Available"):
                    not_in_bs += 1
            fixed = cp.repair_missing(group, new_file)
            repaired_total += fixed
            if fixed == len(group):
                result, action = "Repaired", f"Replaced with {new_file.name if new_file else 'file found on disk'}"
            elif not include:
                result, action = "Ignored", "Not available / not repaired and Reports/Parts List = No (step 4.8)"
            else:
                result, action = "Not repaired", "Download from Bluestar and use Replace Part in Repair Missing Files"
                manual.append(f"{fname} ({dl['status']})")
            r["missing_rows"].append([fname, len(group), dl["status"], dl["revision"], dl["bluestar_status"],
                                      result, "Yes" if include else "No", action + (f" | {dl['message']}" if dl["message"] else "")])

        rec.set(missing_repaired=repaired_total, missing_not_in_bluestar=not_in_bs)
        if repaired_total:
            self.se.idle(2)
            nodes = self._read_tree(doc)           # structure changed -> read again
            cp.show_all(nodes)
        still = cp.missing_groups(nodes)
        detail = f"{len(groups)} missing file(s), {repaired_total} occurrence(s) repaired, {not_in_bs} not in Bluestar"
        if manual:
            self._manual(r, "Missing files not repaired", "Not available in Bluestar or replace failed", manual,
                         "Inspect > Repair Missing Files > Replace Part (suppress relations if asked)")
            st.warn(detail + f"; still missing: {', '.join(still)}")
            r["checkpoint_rows"].append(["Checkpoint-2", "Repair Missing Files", "WARN", detail])
        else:
            st.ok(detail)
            r["checkpoint_rows"].append(["Checkpoint-2", "Repair Missing Files", "PASS", detail])
        return nodes

    def _lasers(self, rec, r, doc, nodes, work: Path, st):
        """Known laser sensors (66135 / 602317) + discovered ones: delete the beam in each sensor part."""
        c = self.cfg["classification"]
        lasers = [n for n in nodes if n.laser in ("Known", "Yes")]
        possible = [n for n in nodes if n.laser == "Possible"]
        for n in possible:
            n.review = (n.review + "; " if n.review else "") + f"Possible laser sensor ({n.laser_reason}) - verify beam"

        changed, errs = cp.apply_visibility(nodes, {"laser"})       # beam occurrences inside sensor assemblies
        beam_occ = [n for n in nodes if n.laser_beam]
        if errs:
            self._manual(r, "Laser beam occurrence not hidden", "Visibility change failed", errs,
                         "Hide the laser beam occurrence manually")

        found_pns = {n.part_number.lstrip("0") for n in lasers if n.laser == "Known"}
        for pn in c.get("laser_part_numbers", []):
            if str(pn).lstrip("0").upper() not in found_pns:
                r["laser_rows"].append([f"{pn}.par", 0, "Known laser sensor", "-", "Not present in this assembly", ""])

        groups: dict[str, list] = {}
        for n in lasers:
            groups.setdefault((n.file or n.name).lower(), []).append(n)
        backup_dir = work / self.scfg["laser_backup_folder"]
        removed, review = 0, []
        for _key, grp in groups.items():
            rep = grp[0]
            path = Path(rep.file)
            visible = [n for n in grp if not n.effectively_hidden]
            res = {"status": "", "message": "", "features": []}
            if not self.scfg["remove_laser_beams"]:
                res["status"] = "Skipped - disabled in config"
            elif not visible:
                res["status"] = "Skipped - all occurrences hidden (not exported)"
            elif rep.is_assembly:
                inside = [b for b in beam_occ if rep in b.ancestors()]
                res["status"] = (f"Sensor assembly - {len(inside)} beam occurrence(s) hidden" if inside
                                 else "No beam identified")
                res["message"] = "; ".join(b.name for b in inside)
            elif not _inside(path, work):
                res["status"] = "Skipped - part not in working folder"
                res["message"] = f"{path} is outside {work}; C:\\myloadpoint files are never modified"
            elif not path.exists():
                res["status"] = "FAILED"
                res["message"] = f"Part file not found: {path}"
            else:
                st.info(f"Laser sensor {path.name} ({rep.laser_reason}) - opening part to remove the beam ...")
                res = cp.remove_laser_beam(self.se, path, self.classifier.is_beam_name, backup_dir)
            if res["status"] == "Beam removed":
                removed += 1
            elif res["status"] in ("No beam identified", "FAILED") or res["status"].startswith("Skipped - part"):
                review.append(f"{path.name}: {res['status']}")
                for n in visible:
                    n.review = (n.review + "; " if n.review else "") + f"Laser beam not removed ({res['status']})"
            for n in grp:
                n.laser_status = res["status"]
            extra = f" | features: {', '.join(res.get('features', [])[:15])}" if res["status"] == "No beam identified" else ""
            r["laser_rows"].append([path.name, len(grp), f"{rep.laser} ({rep.laser_reason})",
                                    rep.title, res["status"], (res.get("message") or "") + extra])
        for n in possible:
            r["laser_rows"].append([Path(n.file).name, 1, f"Possible ({n.laser_reason})", n.title,
                                    "Not modified - review", n.path])
        try:
            doc.Activate()
            call0(doc, "UpdateAll")
        except Exception:
            pass

        rec.set(n_laser=len(lasers), n_laser_files=len(groups), n_laser_beams_removed=removed,
                n_laser_review=len(review) + len(possible))
        msg = (f"{len(groups)} laser sensor part(s) ({len(lasers)} occurrence(s)), {removed} beam(s) removed, "
               f"{len(beam_occ)} beam occurrence(s) hidden, {len(possible)} possible laser sensor(s)")
        if review:
            self._manual(r, "Laser beam not removed", "Beam could not be identified safely / part not editable",
                         review, "Open the sensor part, delete the laser beam feature, save")
        if review or possible or errs:
            st.warn(msg)
        else:
            st.ok(msg)

    def _references(self, r):
        notes, bom_lines, tokens = [], None, None
        if r["bom"]:
            try:
                bom_lines = read_standard_bom(r["bom"])
                if not bom_lines:
                    notes.append("standard BOM is empty")
            except Exception as e:
                notes.append(f"standard BOM unreadable ({e})")
        else:
            notes.append("standard BOM missing - occurrence property used for level 1")
        r["bom_lines"] = bom_lines
        if r["pdf"] and self.scfg["use_2d_draft_crosscheck"]:
            try:
                tokens = read_draft_tokens(r["pdf"]) or None
            except Exception:
                tokens = None
        return bom_lines, tokens, notes

    def _apply(self, st, r, nodes, rules: set[str], label: str, quiet: bool = False):
        changed, errors = cp.apply_visibility(nodes, rules)
        hidden = sum(1 for n in nodes if n.rule in rules and n.action == "Hide")
        kept = sum(1 for n in nodes if n.rule in rules and n.action.startswith("Keep"))
        msg = f"{label}: {hidden} hidden, {kept} kept, {changed} visibility change(s)"
        if errors:
            st.warn(msg + f", {len(errors)} failed")
            self._manual(r, f"Visibility change failed ({label})", "Solid Edge refused visibility change",
                         errors, "Hide / show these occurrences manually")
        else:
            st.ok(msg)

    def _validate(self, rec, r, nodes, st):
        bad = cp.verify_visibility(nodes)
        style_flag = rec.steps.get("P2.S7")
        style_ok = style_flag is not None and style_flag.status == Status.PASS
        prot_hidden = [n.path for n in nodes if n.protected and not n.expected_visible
                       and n.action != "Hidden via parent" and not n.file_missing and not n.laser_beam]
        hide_all_bad = [n.path for n in nodes if n.action == "Hidden via parent" and n.path in bad]
        laser_bad = [n.path for n in nodes if n.laser in ("Known", "Yes") and not n.effectively_hidden
                     and n.laser_status not in ("Beam removed", "") and not n.laser_status.startswith("Sensor assembly")
                     and not n.laser_status.startswith("Skipped - all")]
        sums = summarize(nodes)
        orient = r["orientation"][0]
        missing_left = [n.path for n in nodes if n.file_missing and n.include_in_bom is not False]
        lvl1 = [n for n in nodes if n.level == 1]
        bom_kept = all(n.expected_visible for n in lvl1 if n.in_bom == "Yes")
        nonbom_hidden = all(not n.expected_visible or n.rule in ("electrical", "protected") or n.action.startswith("Partial")
                            for n in lvl1 if n.in_bom == "No")
        elec_ok = all(not n.effectively_hidden for n in nodes if n.electrical == "Yes" and not n.file_missing
                      and n.action != "Hidden via parent")
        checks = [
            ("Orientation", "Z-axis is pointing upward", orient or "REVIEW", r["orientation"][1]),
            ("Assembly structure", "Top-level assembly is correct", "PASS", r["asm"].name if r["asm"] else ""),
            ("Assembly structure", "Sub-level / nested assemblies inspected",
             "PASS", f"{sum(1 for n in nodes if n.is_assembly)} subassemblies, depth {max((n.level for n in nodes), default=0)}"),
            ("BOM", "Standard BOM components are retained", "PASS" if bom_kept else "FAIL", ""),
            ("BOM", "Non-standard BOM components are hidden", "PASS" if nonbom_hidden else "FAIL",
             f"{sums['n_hidden_bom']} hidden by BOM rule"),
            ("BOM", "Components without valid occurrence/part hidden", "PASS",
             f"{sums['n_hidden_missing']} hidden"),
            ("BOM", "Electrical components are preserved", "PASS" if elec_ok else "WARN",
             f"{sums['n_electrical_kept']} electrical visible"),
            ("Geometry", "No unintended geometry modifications", "PASS",
             "Only visibility / display style changed, no move"
             + (f"; laser beam geometry deleted in {rec.data.get('n_laser_beams_removed', 0)} sensor part(s) "
                "(originals backed up)" if rec.data.get("n_laser_beams_removed") else "")),
            ("Geometry", "No required components accidentally removed", "PASS" if not missing_left else "WARN",
             "No components deleted" + (f"; {len(missing_left)} required file(s) still missing" if missing_left else "")),
            ("Geometry", "Assembly relationships remain intact", "PASS", "No relationship edits performed"),
            ("Display", "Top-level part style applied", "PASS" if style_ok else "WARN",
             style_flag.message if style_flag else ""),
            ("Display", "Only intended components remain visible", "PASS" if not bad else "FAIL",
             f"{len(bad)} visibility mismatch(es)" if bad else "all visibility states verified"),
            ("Review", "Ambiguous components flagged for manual review", "WARN" if sums["n_review"] else "PASS",
             f"{sums['n_review']} component(s)"),
            ("Display", "Every identified motor is red (Face Override)",
             "PASS" if not sums["n_motors_failed"] else "FAIL",
             f"{sums['n_motors_red']}/{sums['n_motors']} motor occurrence(s) red"),
            ("BOM", "Protected components not hidden by cleanup rules",
             "PASS" if not prot_hidden else "FAIL",
             f"{sums['n_protected_kept']} kept against hide rules" + (f"; {len(prot_hidden)} hidden" if prot_hidden else "")),
            ("Assembly structure", "Hidden assemblies fully hidden (Hide All)",
             "PASS" if not hide_all_bad else "FAIL",
             f"{sums['n_hide_all']} assembly(ies)" + (f"; {len(hide_all_bad)} child(ren) still visible" if hide_all_bad else "")),
            ("Geometry", "Laser beams removed from laser sensors",
             "PASS" if not laser_bad else "WARN",
             f"{rec.data.get('n_laser_beams_removed', 0)} removed" + (f"; review: {len(laser_bad)}" if laser_bad else "")),
        ]
        r["validation_rows"] = [list(c) for c in checks]
        fails = [c for c in checks if c[2] == "FAIL"]
        warns = [c for c in checks if c[2] in ("WARN", "REVIEW")]
        if bad:
            self._manual(r, "Visibility mismatch after clean up", "State differs from decision", bad,
                         "Check these occurrences in PathFinder")
        if fails:
            st.warn("Checklist FAIL: " + "; ".join(c[1] for c in fails))
        elif warns:
            st.warn("Checklist needs review: " + "; ".join(c[1] for c in warns))
        else:
            st.ok("All checklist items passed")

    def _manual(self, r, what, why, affected, action):
        affected = list(affected)
        r["manual_rows"].append([what, why, "; ".join(affected[:50]) + (" ..." if len(affected) > 50 else ""), action])

    def _finish_flags(self, rec):
        s = rec.steps.get
        rec.set(cp1=_flag_to_cp(s("P2.S3")), cp2=_flag_to_cp(s("P2.S4")), cp3=_flag_to_cp(s("P2.S6")),
                cp4=_flag_to_cp(s("P2.S7")),
                cp5=_flag_to_cp(s("P2.S8"), s("P2.S9"), s("P2.S10"), s("P2.S11"), s("P2.S11A")),
                cp6=_flag_to_cp(s("P2.S12"), s("P2.S13")), cp7=_flag_to_cp(s("P2.S14")),
                laser_status=_flag_to_cp(s("P2.S11B")))

    def _write_report(self, rec, r, path: Path):
        nodes = r["nodes"]
        review_nodes = [n for n in nodes if n.review]
        groups = defaultdict(list)
        for n in review_nodes:
            groups[n.review.split(" - ")[0].split(":")[0].split("(")[0].strip()].append(n.path)
        for reason, paths in groups.items():
            r["manual_rows"].append([reason, "Rule could not decide safely", "; ".join(paths[:50]),
                                     "Review in Solid Edge and hide / show manually if needed"])
        for row in r["manual_rows"]:
            rec.add_manual(f"{row[0]}: {row[2][:120]}")

        for key, name in PROCESS2_STEPS[:-1]:
            f = rec.steps.get(key)
            if f is not None:
                r["checkpoint_rows"].append([name.split(":")[0] if name.startswith("Checkpoint") else "-",
                                             f"{key} {name}", f.status, f.message])
        d = rec.data
        orient = r["orientation"]
        pairs = [
            ("Serial number", rec.serial),
            ("Input CAD file", str(r["asm"] or "")),
            ("Engineer BOM / reference", str(r["bom"] or "NOT AVAILABLE")),
            ("2D Draft", str(r["pdf"] or "NOT AVAILABLE")),
            ("Top-level assembly", r["asm"].name if r["asm"] else ""),
            ("Revision in Bluestar", f"{d.get('revision', '')} ({d.get('bluestar_status', '')})"),
            ("Orientation  Z-up", orient[0] or "NOT CHECKED"),
            ("Orientation details", orient[1]),
            ("Components analyzed", d.get("n_analyzed", 0)),
            ("Components retained", d.get("n_retained", 0)),
            ("Components hidden", d.get("n_hidden", 0)),
            ("Components hidden - not in standard BOM", d.get("n_hidden_bom", 0)),
            ("Components hidden due to missing occurrence/part", d.get("n_hidden_missing", 0)),
            ("Electrical components identified", d.get("n_electrical", 0)),
            ("Electrical components retained/unhidden", f"{d.get('n_electrical_kept', 0)} retained, "
                                                        f"{d.get('n_electrical_unhidden', 0)} unhidden"),
            ("Motors identified / coloured red / failed",
             f"{d.get('n_motors', 0)} / {d.get('n_motors_red', 0)} / {d.get('n_motors_failed', 0)}"),
            ("Protected components identified / kept against hide rules",
             f"{d.get('n_protected', 0)} / {d.get('n_protected_kept', 0)}"),
            ("Assemblies hidden with Hide All", d.get("n_hide_all", 0)),
            ("Laser sensor parts / beams removed / need review",
             f"{d.get('n_laser_files', 0)} / {d.get('n_laser_beams_removed', 0)} / {d.get('n_laser_review', 0)}"),
            ("Ambiguous components requiring review", d.get("n_review", 0)),
            ("Missing files found / repaired / not in Bluestar",
             f"{d.get('missing_found', 0)} / {d.get('missing_repaired', 0)} / {d.get('missing_not_in_bluestar', 0)}"),
            ("Final cleaned STEP ready", d.get("step_available", "NO")),
            ("STEP file", d.get("step_path", "")),
            ("Report created", iso(now())),
        ]
        motor_rows = [[n.level, n.path, n.part_number, n.title, n.category, n.motor_reason,
                       n.motor_covered_by or "-", n.motor_status or "Not processed",
                       "No" if n.motor_status.startswith("Red") else "Yes"] for n in nodes if n.motor]
        bom_lines = r.get("bom_lines")
        if bom_lines is None and r.get("bom"):
            try:
                bom_lines = read_standard_bom(r["bom"])
            except Exception:
                bom_lines = None
        ctx = dict(r, nodes=nodes, summary_pairs=pairs, motor_rows=motor_rows, bom_lines=bom_lines,
                   bom_name=r["bom"].name if r.get("bom") else "")
        write_cleanup_report(path, ctx)
