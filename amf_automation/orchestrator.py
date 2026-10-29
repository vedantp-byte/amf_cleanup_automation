"""Runs one complete cycle: every serial in machine_ids.xlsx through Process 1 -> Process 2."""

from pathlib import Path

from .bluestar import BluestarSession
from .input_reader import completed_serials, read_serials, validate_serials
from .logger import add_file_handler, banner
from .models import PROCESS1_STEPS, PROCESS2_STEPS, SerialRecord, Status, StepFlag
from .process1 import run_process1
from .process2 import Process2, interlock
from .reports.summary import write_history, write_summary
from .solidedge.app import SolidEdgeApp
from .tracker import CycleAbort, RunContext, Tracker, load_history
from .utils import iso, now
from .vpn import VpnMonitor


class Orchestrator:
    def __init__(self, cfg: dict, log, only: str | None = None, serial_filter: list[str] | None = None,
                 force: bool = False, input_file: Path | None = None):
        self.cfg = cfg
        self.log = log
        self.only = only                     # None | "process1" | "process2"
        self.serial_filter = [s.upper() for s in (serial_filter or [])]
        self.force = force
        self.input_file = input_file or cfg["resolved"]["input_file"]
        self.run = RunContext(cfg["resolved"]["reports"])
        self.tracker = Tracker(self.run, log)
        self.vpn = VpnMonitor(cfg, log)
        self.bs = BluestarSession(cfg, log, self.vpn, self.run.screenshots)
        self.se = SolidEdgeApp(log)
        self.summary_path = self.run.run_dir / f"summary_report_{self.run.cycle_id}.xlsx"

    # ------------------------------------------------------------------ run
    def execute(self) -> int:
        add_file_handler(self.log, self.run.run_dir / "run.log")
        self.run.input_file = self.input_file
        banner(self.log, f"AMF Solid Edge Clean Up Automation  |  {self.run.cycle_id}")
        self.log.info(f"  Input file     : {self.input_file}")
        self.log.info(f"  myloadpoint    : {self.cfg['resolved']['myloadpoint']}")
        self.log.info(f"  Working folder : {self.cfg['resolved']['working_root']}")
        self.log.info(f"  Reports        : {self.run.run_dir}")
        if self.only:
            self.log.info(f"  Mode           : {self.only} only")

        records = self._load_input()
        self.run.records = records
        self._save_reports()
        todo = [r for r in records if r.is_valid]
        if not todo:
            self.log.warning("  No new valid serial numbers to process.")
            return self._finish()

        self.tracker.screenshot_hook = lambda name: self.bs.screenshot(name) if self.bs.started else ""
        process2 = Process2(self.cfg, self.tracker, self.se, self._bluestar, self.log)
        try:
            if self.only != "process2":
                self.vpn.ensure_connected("cycle start")
                self.vpn.start_monitor()
            for i, rec in enumerate(todo, start=1):
                banner(self.log, f"[{i}/{len(todo)}]  Serial {rec.serial}", "-")
                self._process_one(rec, process2)
        except CycleAbort as e:
            self.run.aborted_reason = str(e)
            self.log.error(f"  CYCLE ABORTED: {e}")
        except KeyboardInterrupt:
            self.run.aborted_reason = "Stopped by user (Ctrl+C)"
            self.log.error("  CYCLE STOPPED BY USER")
        finally:
            self.vpn.stop_monitor()
            self.bs.close()
            for rec in todo:
                if not rec.data.get("overall_status"):
                    rec.set(overall_status="NOT PROCESSED",
                            error=rec.data.get("error") or self.run.aborted_reason or "Not processed")
                    self.tracker.skip_remaining(rec, PROCESS1_STEPS + PROCESS2_STEPS,
                                                self.run.aborted_reason or "Cycle stopped")
        return self._finish()

    def _process_one(self, rec: SerialRecord, process2: Process2):
        rec.started = now()
        rec.set(start_time=iso(rec.started))
        try:
            if self.only == "process2":
                ok1 = self._prepare_process2_only(rec)
            else:
                ok1 = run_process1(rec, self.cfg, self.tracker, self.vpn, self.bs, self.log)

            if ok1 and self.only != "process1" and self.cfg["solidedge"]["enabled"]:
                work = Path(rec.data.get("working_folder") or (self.cfg["resolved"]["working_root"] / rec.serial))
                problems = interlock(rec, work, process1_ran=self.only != "process2")
                if problems:                     # interlock: no clean-up without Standard BOM + 3D CAD
                    reason = "; ".join(problems)
                    self.log.error(f"  !! Process 2 NOT started for {rec.serial} (interlock): {reason}")
                    rec.set(p2_status="BLOCKED", step_available="NO",
                            error=f"Process 2 not started (interlock): {reason}")
                    rec.add_manual(f"Process 2 not started: {reason} - download it in Bluestar and run again")
                    self.tracker.skip_remaining(rec, PROCESS2_STEPS, f"Interlock: {reason}")
                else:
                    banner(self.log, f"Process 2 - Solid Edge clean up  |  {rec.serial}", "-")
                    process2.run(rec)
            elif not ok1:
                self.tracker.skip_remaining(rec, PROCESS2_STEPS, "Process 1 not completed")
        except CycleAbort as e:
            rec.set(error=f"Cycle aborted: {e}")
            self.tracker.skip_remaining(rec, PROCESS1_STEPS + PROCESS2_STEPS, "Cycle aborted")
            raise
        finally:
            rec.finished = now()
            self._overall(rec)
            self.run.append_history(rec)
            self._save_reports()
            self.log.info(f"  ==> {rec.serial}: {rec.data['overall_status']}"
                          + (f"  ({rec.data.get('error')})" if rec.data.get("error") else ""),
                          extra={"tag": "PASS" if rec.data["overall_status"].startswith("SUCCESS")
                                 or rec.data["overall_status"] == "PROCESS 1 DONE" else "FAIL"})

    def _prepare_process2_only(self, rec: SerialRecord) -> bool:
        work = self.cfg["resolved"]["working_root"] / rec.serial
        self.tracker.skip_remaining(rec, PROCESS1_STEPS, "Process 2 only run")
        if not work.exists():
            rec.set(p1_status="SKIPPED", error=f"Working folder not found: {work}")
            return False
        rec.set(working_folder=str(work), working_status="Existing", p1_status="SKIPPED")
        return True

    def _bluestar(self):
        """Started Bluestar session on demand (Process-2 missing file downloads)."""
        if not self.bs.started:
            self.vpn.ensure_connected("missing file download")
            self.bs.start()
            self.bs.open_home(new_tab=False)
        return self.bs

    # --------------------------------------------------------------- input
    def _load_input(self) -> list[SerialRecord]:
        raw, column = read_serials(self.input_file, self.cfg["input"]["serial_column_candidates"])
        self.log.info(f"  Serial column  : '{column}'  ({len(raw)} value(s))")
        if self.serial_filter:
            raw = [(row, s) for row, s in raw if s.upper() in self.serial_filter]
        skip_done = self.cfg["input"]["skip_already_completed"] and not self.force
        done = completed_serials(load_history(self.run.history_dir)) if skip_done else {}
        records = validate_serials(raw, done, skip_done)

        for rec in records:          # P1.S2 flag for every input line
            flag = StepFlag("P1.S2", dict(PROCESS1_STEPS)["P1.S2"], started=now(), finished=now())
            if rec.is_valid:
                flag.status, flag.message = Status.PASS, "New distinct valid serial"
            else:
                flag.status, flag.message = Status.SKIP, rec.validation
                rec.set(overall_status=f"SKIPPED - {rec.validation}", download_status="Not processed")
                self.tracker.skip_remaining(rec, PROCESS1_STEPS + PROCESS2_STEPS, rec.validation)
            rec.steps["P1.S2"] = flag
            rec.set(serial=rec.serial, row=rec.row, validation=rec.validation)

        valid = sum(r.is_valid for r in records)
        self.log.info(f"  STEP-2 check   : {valid} to process, {len(records) - valid} skipped "
                      "(duplicate / invalid / already processed)")
        for r in records:
            if not r.is_valid:
                self.log.info(f"      row {r.row}: {r.serial} -> {r.validation}", extra={"tag": "SKIPPED"})
        return records

    # ------------------------------------------------------------- results
    def _overall(self, rec: SerialRecord):
        d = rec.data
        dur = (rec.finished - rec.started).total_seconds() / 60 if rec.started and rec.finished else 0
        d.update(timestamp=iso(rec.finished), duration_min=round(dur, 1))
        if rec.manual_actions:
            d["manual_action"] = " | ".join(rec.manual_actions[:15])
        if d.get("p2_status") == "BLOCKED":
            d["overall_status"] = "STOPPED - INTERLOCK"
        elif d.get("p1_status") == "FAIL" or d.get("p2_status") == "FAIL":
            d["overall_status"] = "FAILED"
        elif self.only == "process1" or not self.cfg["solidedge"]["enabled"]:
            d["overall_status"] = "PROCESS 1 DONE" if d.get("p1_status") in ("PASS", "WARN") else "FAILED"
        elif d.get("step_available") == "YES":
            review = any(f.status == Status.WARN for f in rec.steps.values()) or rec.manual_actions
            d["overall_status"] = "SUCCESS - REVIEW" if review else "SUCCESS"
        else:
            d["overall_status"] = "FAILED"

    def _save_reports(self):
        try:
            write_summary(self.summary_path, self.run, self.run.records)
        except Exception as e:
            self.log.warning(f"  Summary report could not be written: {e}")

    def _finish(self) -> int:
        self.run.finished = now()
        self.run.save_state()
        self._save_reports()
        try:
            write_history(self.run.history_dir / "summary_history.xlsx", load_history(self.run.history_dir))
        except Exception as e:
            self.log.warning(f"  History workbook could not be written: {e}")

        banner(self.log, "CYCLE RESULT")
        for r in self.run.records:
            d = r.data
            self.log.info(f"  {r.serial:<22} {d.get('overall_status', ''):<34} STEP: {d.get('step_available', '-'):<4}"
                          f" review: {d.get('n_review', '-')}")
        self.log.info(f"  Summary report : {self.summary_path}")
        self.log.info(f"  History        : {self.run.history_dir / 'summary_history.xlsx'}")
        self.log.info(f"  Log            : {self.run.run_dir / 'run.log'}")
        failed = [r for r in self.run.records
                  if r.data.get("overall_status") in ("FAILED", "NOT PROCESSED", "STOPPED - INTERLOCK")]
        return 1 if (failed or self.run.aborted_reason) else 0
