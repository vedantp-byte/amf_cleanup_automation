"""Step flags, run folder, state persistence and history.

Every main step runs inside ``tracker.step(...)``. A step only counts as done
when it finishes without error (the "confirmation flag"). A failing critical
step raises StepFailed so the remaining steps of that serial are skipped.
"""

import json
import logging
import traceback
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import SerialRecord, Status, StepFlag
from .utils import iso, now


class StepFailed(Exception):
    """A main step failed -> stop the current serial."""


class CycleAbort(Exception):
    """Unrecoverable for the whole cycle (e.g. VPN cannot be restored)."""


class StepContext:
    def __init__(self, flag: StepFlag, log: logging.Logger):
        self.flag = flag
        self.log = log

    def info(self, msg: str):
        self.log.info(f"      {msg}")

    def ok(self, msg: str = ""):
        self.flag.status, self.flag.message = Status.PASS, msg

    def warn(self, msg: str):
        self.flag.status, self.flag.message = Status.WARN, msg

    def skip(self, msg: str):
        self.flag.status, self.flag.message = Status.SKIP, msg

    def na(self, msg: str):
        self.flag.status, self.flag.message = Status.NA, msg

    def fail(self, msg: str):
        raise StepFailed(msg)


class RunContext:
    """One automation cycle (one run of main.py)."""

    def __init__(self, reports_root: Path, cycle_id: str | None = None):
        self.cycle_id = cycle_id or datetime.now().strftime("CYCLE_%Y%m%d_%H%M%S")
        self.reports_root = reports_root
        self.run_dir = reports_root / self.cycle_id
        self.screenshots = self.run_dir / "screenshots"
        self.history_dir = reports_root / "history"
        for d in (self.run_dir, self.screenshots, self.history_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.records: list[SerialRecord] = []
        self.started = now()
        self.finished = None
        self.input_file = ""
        self.aborted_reason = ""

    @property
    def state_file(self) -> Path:
        return self.run_dir / "state.json"

    def save_state(self):
        def ser(o):
            if isinstance(o, datetime):
                return iso(o)
            if isinstance(o, Path):
                return str(o)
            return str(o)

        state = {
            "cycle_id": self.cycle_id,
            "input_file": str(self.input_file),
            "started": iso(self.started),
            "finished": iso(self.finished),
            "aborted_reason": self.aborted_reason,
            "records": [asdict(r) for r in self.records],
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=ser, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def append_history(self, rec: SerialRecord):
        """history.jsonl keeps every processed serial of every cycle forever."""
        line = {"cycle_id": self.cycle_id, "serial": rec.serial, "finished": iso(rec.finished),
                "overall_status": rec.data.get("overall_status", ""), **{k: _plain(v) for k, v in rec.data.items()},
                "steps": {k: {"status": f.status, "message": f.message} for k, f in rec.steps.items()}}
        with open(self.history_dir / "history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")


def _plain(v):
    return str(v) if isinstance(v, (Path, datetime)) else v


def load_history(history_dir: Path) -> list[dict]:
    path = history_dir / "history.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


class Tracker:
    def __init__(self, run: RunContext, log: logging.Logger):
        self.run = run
        self.log = log
        self.screenshot_hook = None      # set by orchestrator: callable(name) -> path

    @contextmanager
    def step(self, rec: SerialRecord, key: str, name: str, critical: bool = True):
        flag = StepFlag(key=key, name=name, status=Status.RUNNING, started=now())
        rec.steps[key] = flag
        self.log.info(f"  >> [{rec.serial}] {key} {name} ...")
        ctx = StepContext(flag, self.log)
        try:
            yield ctx
            if flag.status == Status.RUNNING:
                flag.status = Status.PASS
        except CycleAbort as e:
            flag.status, flag.message = Status.FAIL, str(e)
            raise
        except StepFailed as e:
            flag.status, flag.message = Status.FAIL, str(e)
            self._evidence(rec, key)
            if critical:
                raise
        except Exception as e:  # unexpected -> treat as step failure
            flag.status, flag.message = Status.FAIL, f"Unexpected error: {e}"
            self.log.debug(traceback.format_exc())
            self._evidence(rec, key)
            if critical:
                raise StepFailed(flag.message) from e
        finally:
            flag.finished = now()
            secs = (flag.finished - flag.started).total_seconds()
            tag = flag.status
            text = f"     [{flag.status}] {key} {name} ({secs:.0f}s)"
            if flag.message:
                text += f" - {flag.message}"
            self.log.info(text, extra={"tag": tag})
            try:
                self.run.save_state()
            except Exception:
                self.log.debug("state save failed", exc_info=True)

    def skip_remaining(self, rec: SerialRecord, steps: list[tuple[str, str]], reason: str):
        for key, name in steps:
            if key not in rec.steps:
                rec.steps[key] = StepFlag(key=key, name=name, status=Status.SKIP, message=reason)

    def _evidence(self, rec: SerialRecord, key: str):
        if self.screenshot_hook:
            try:
                path = self.screenshot_hook(f"{rec.serial}_{key}")
                if path:
                    self.log.info(f"      screenshot saved: {path}")
            except Exception:
                pass
