"""Console + file logging. Console output is the on-screen progress monitor."""

import logging
import os
import sys
from pathlib import Path

_COLORS = {"PASS": "\033[92m", "FAIL": "\033[91m", "WARN": "\033[93m", "SKIPPED": "\033[90m", "INFO": "\033[96m"}
_RESET = "\033[0m"


class _ConsoleFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        tag = getattr(record, "tag", None)
        if tag in _COLORS:
            return f"{_COLORS[tag]}{msg}{_RESET}"
        if record.levelno >= logging.ERROR:
            return f"{_COLORS['FAIL']}{msg}{_RESET}"
        if record.levelno == logging.WARNING:
            return f"{_COLORS['WARN']}{msg}{_RESET}"
        return msg


def setup_logging(log_file: Path | None = None) -> logging.Logger:
    os.system("")  # enables ANSI colours in the Windows console
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    log = logging.getLogger("amf")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(_ConsoleFormatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    log.addHandler(console)

    if log_file:
        add_file_handler(log, log_file)
    return log


def add_file_handler(log: logging.Logger, log_file: Path):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)


def banner(log: logging.Logger, text: str, char: str = "="):
    line = char * 78
    log.info(line, extra={"tag": "INFO"})
    log.info(f"  {text}", extra={"tag": "INFO"})
    log.info(line, extra={"tag": "INFO"})
