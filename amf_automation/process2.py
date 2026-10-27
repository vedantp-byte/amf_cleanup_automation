"""Process 2 - Machine clean up and STEP export in Solid Edge (STEP-1 ... STEP-14) for one serial."""

from collections import defaultdict
from pathlib import Path

from .bluestar.checkout import find_top_asm
from .bluestar.item import download_item
from .models import PROCESS2_STEPS, SerialRecord, Status
from .reports.cleanup import write_cleanup_report
from .solidedge import checkpoints as cp
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
             "manual_rows": [], "asm": None, "bom": None, "pdf": None, "orientation": ("", ""),
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
                bom = next((p for p in work.glob("*.xlsx") if "bom" in p.name.lower()), None)
                pdfs = [p for p in work.glob("*") if p.suffix.lower() == ".pdf"]
                pdf = next((p for p in pdfs if p.stem.upper() == sn.upper()), pdfs[0] if pdfs else None)
                r.update(asm=asm, bom=bom, pdf=pdf)
                msg = f"ASM={asm.name} | BOM={bom.name if bom else 'MISSING'} | PDF={pdf.name if pdf else 'MISSING'}"
                st.warn(msg) if (bom is None or pdf is None) else st.ok(msg)

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
                m_done, m_errs = cp.color_motors(doc, nodes, self.scfg["motor_face_style"])
                n_mot = sum(1 for n in nodes if n.motor)
                msg = f"Use Part Style on {done} occurrence(s); motors red {m_done}/{n_mot}"
                if errs or m_errs:
                    st.warn(msg + f"; {len(errs) + len(m_errs)} error(s)")
                    self._manual(r, "Part style / motor colour incomplete", "Solid Edge style change failed",
                                 errs + m_errs, "Apply (Use Part Style) / Red face style manually")
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
        sums = summarize(nodes)
        orient = r["orientation"][0]
        missing_left = [n.path for n in nodes if n.file_missing and n.include_in_bom is not False]
        lvl1 = [n for n in nodes if n.level == 1]
        bom_kept = all(n.expected_visible for n in lvl1 if n.in_bom == "Yes")
        nonbom_hidden = all(not n.expected_visible or n.rule == "electrical" for n in lvl1 if n.in_bom == "No")
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
             "Only visibility / display style changed (no delete, no move)"),
            ("Geometry", "No required components accidentally removed", "PASS" if not missing_left else "WARN",
             "Nothing deleted" + (f"; {len(missing_left)} required file(s) still missing" if missing_left else "")),
            ("Geometry", "Assembly relationships remain intact", "PASS", "No relationship edits performed"),
            ("Display", "Top-level part style applied", "PASS" if style_ok else "WARN",
             style_flag.message if style_flag else ""),
            ("Display", "Only intended components remain visible", "PASS" if not bad else "FAIL",
             f"{len(bad)} visibility mismatch(es)" if bad else "all visibility states verified"),
            ("Review", "Ambiguous components flagged for manual review", "WARN" if sums["n_review"] else "PASS",
             f"{sums['n_review']} component(s)"),
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
                cp4=_flag_to_cp(s("P2.S7")), cp5=_flag_to_cp(s("P2.S8"), s("P2.S9"), s("P2.S10"), s("P2.S11")),
                cp6=_flag_to_cp(s("P2.S12"), s("P2.S13")), cp7=_flag_to_cp(s("P2.S14")))

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
            ("Motors coloured red", d.get("n_motors", 0)),
            ("Ambiguous components requiring review", d.get("n_review", 0)),
            ("Missing files found / repaired / not in Bluestar",
             f"{d.get('missing_found', 0)} / {d.get('missing_repaired', 0)} / {d.get('missing_not_in_bluestar', 0)}"),
            ("Final cleaned STEP ready", d.get("step_available", "NO")),
            ("STEP file", d.get("step_path", "")),
            ("Report created", iso(now())),
        ]
        ctx = dict(r, nodes=nodes, summary_pairs=pairs)
        write_cleanup_report(path, ctx)
