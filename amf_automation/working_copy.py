"""Process-1 STEP-8: copy c:\\myloadpoint\\SN to Documents\\AMF 3D Files - For Working\\SN.

8.2 zip the serial folder in myloadpoint, move the zip to the working folder
8.3 unzip it there, 8.4 delete the zip. The copy is not synced to Bluestar.
"""

import os
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .tracker import StepFailed
from .utils import folder_snapshot


def make_working_copy(serial: str, myloadpoint: Path, working_root: Path, clear_readonly: bool,
                      backup_existing: bool, log) -> dict:
    src = myloadpoint / serial
    if not src.exists():
        raise StepFailed(f"Source folder not found: {src}")
    working_root.mkdir(parents=True, exist_ok=True)
    target = working_root / serial

    backup = ""
    if target.exists():
        if backup_existing:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = working_root / f"{serial}_backup_{stamp}"
            target.rename(bak)
            backup = str(bak)
            log.info(f"      Existing working folder renamed to {bak.name}")
        else:
            shutil.rmtree(target, onexc=_force_remove)

    src_count, src_size, _ = folder_snapshot(src)
    with tempfile.TemporaryDirectory(prefix="amf_zip_") as tmp:
        zip_tmp = Path(tmp) / f"{serial}.zip"
        log.info(f"      Zipping {src} ({src_count} files) ...")
        with zipfile.ZipFile(zip_tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for root, _dirs, files in os.walk(src):
                for f in files:
                    full = Path(root) / f
                    zf.write(full, arcname=str(full.relative_to(myloadpoint)))
        zip_final = working_root / f"{serial}.zip"
        if zip_final.exists():
            zip_final.unlink()
        shutil.move(str(zip_tmp), str(zip_final))                       # 8.2
    log.info(f"      Unzipping into {working_root} ...")
    with zipfile.ZipFile(zip_final) as zf:                               # 8.3
        zf.extractall(working_root)
    zip_final.unlink()                                                   # 8.4

    if clear_readonly:
        for root, _dirs, files in os.walk(target):
            for f in files:
                p = os.path.join(root, f)
                try:
                    os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass

    dst_count, dst_size, _ = folder_snapshot(target)
    if dst_count != src_count:
        raise StepFailed(f"Working copy incomplete: {dst_count} of {src_count} files copied")
    return {"working_folder": target, "files": dst_count, "size": dst_size, "backup": backup}


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)
