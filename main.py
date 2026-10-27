"""AMF Solid Edge Clean Up Automation - entry point.

Normal use (put a fresh machine_ids.xlsx next to this file, then):
    python main.py

Options:
    python main.py --check                  verify VPN, Chrome, Solid Edge, folders, input file
    python main.py --input other.xlsx       use another input workbook
    python main.py --serials 22158-1000ME   only these serials from the input file
    python main.py --only process1          only download from Bluestar (STEP-1..8)
    python main.py --only process2          only Solid Edge clean up of existing working folders
    python main.py --force                  also re-run serials that already succeeded earlier
    python main.py --reuse-download         skip the 3D check-out if the CAD is already in C:\\myloadpoint
"""

import argparse
import sys
from pathlib import Path

from amf_automation.config import load_config
from amf_automation.logger import setup_logging


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="AMF Solid Edge Clean Up Automation")
    p.add_argument("--config", help="config file (default: config.yaml)")
    p.add_argument("--input", help="machine ids workbook (default from config: machine_ids.xlsx)")
    p.add_argument("--serials", nargs="+", help="process only these serial numbers")
    p.add_argument("--only", choices=["process1", "process2"], help="run only one process")
    p.add_argument("--force", action="store_true", help="re-run serials already completed in earlier cycles")
    p.add_argument("--check", action="store_true", help="environment self-check only")
    p.add_argument("--reuse-download", action="store_true",
                   help="skip the 3D CAD check-out when C:\\myloadpoint\\SN\\SN\\SN.asm already exists")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log = setup_logging()
    try:
        cfg = load_config(args.config)
    except Exception as e:
        log.error(f"Config error: {e}")
        return 2

    if args.reuse_download:
        cfg["bluestar"]["checkout"]["reuse_existing"] = True

    if args.check:
        from amf_automation.selfcheck import run_selfcheck
        return run_selfcheck(cfg, log)

    from amf_automation.orchestrator import Orchestrator

    input_file = Path(args.input).resolve() if args.input else None
    try:
        return Orchestrator(cfg, log, only=args.only, serial_filter=args.serials, force=args.force,
                            input_file=input_file).execute()
    except FileNotFoundError as e:
        log.error(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
