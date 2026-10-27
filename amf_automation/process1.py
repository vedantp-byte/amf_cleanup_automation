"""Process 1 - Download CAD and relevant files from Bluestar PLM (STEP-1 ... STEP-8) for one serial."""

from .bluestar.bom import download_bom
from .bluestar.checkout import checkout_selected
from .bluestar.client import ensure_running as ensure_client_running
from .bluestar.draft import download_2d_draft
from .bluestar.search import choose_revision, reselect
from .models import PROCESS1_STEPS, SerialRecord
from .tracker import StepFailed
from .utils import human_size
from .working_copy import make_working_copy


def run_process1(rec: SerialRecord, cfg: dict, tracker, vpn, bs, log) -> bool:
    sn = rec.serial
    res = cfg["resolved"]
    serial_root = res["myloadpoint"] / sn          # "second last" folder: 2D draft + BOM
    cad_folder = serial_root / sn                  # last folder: 3D CAD
    row = label = None
    try:
        with tracker.step(rec, "P1.S1", "Check GlobalProtect VPN connected") as st:
            status = vpn.ensure_connected(f"serial {sn}")
            rec.set(vpn_status=status)
            st.ok(status)

        with tracker.step(rec, "P1.S3", "Open Bluestar PLM") as st:
            ensure_client_running(cfg, log)
            if not bs.started:
                bs.start()
            bs.open_home(new_tab=True)
            st.ok(bs.bcfg["url"])

        with tracker.step(rec, "P1.S4", "Search serial & select revision") as st:
            row, label = choose_revision(bs, sn)
            if row is None:
                rec.set(bluestar_status="Not Available in Bluestar", download_status="Not Available in Bluestar",
                        cad_status="Not Available in Bluestar")
                st.fail("Not Available in Bluestar (no entry under Latest / All approved / All)")
            rec.set(object_id=row.object_id, object_name=row.name, revision=row.revision,
                    bluestar_status=row.status, revision_filter=label)
            if row.approved:
                st.ok(f"{row.object_id} rev {row.revision} ({row.status}) via '{label}'")
            else:
                st.warn(f"No approved revision - fallback rev {row.revision} with status '{row.status}' via '{label}'")
                rec.add_manual(f"No approved revision in Bluestar - downloaded rev {row.revision} ({row.status})")

        with tracker.step(rec, "P1.S5", "Check out (download) Solid Edge 3D CAD") as st:
            vpn.ensure_connected("before check out")
            reselect(bs, sn, row, label)
            result = checkout_selected(bs, cad_folder, sn)
            rec.set(download_status="Downloaded", cad_status="success", cad_file=str(result["top_file"]),
                    files_downloaded=result["files"], download_size=human_size(result["size"]))
            msg = f"{result['files']} files, {human_size(result['size'])} -> {cad_folder}"
            if result["vpn_interrupted"]:
                st.warn(msg + " (VPN was interrupted during download - verify missing files in Process 2)")
            else:
                st.ok(msg)

        with tracker.step(rec, "P1.S6", "Download 2D Draft (PDF)", critical=False) as st:
            if not bs.bcfg["download_2d_draft"]:
                rec.set(pdf_status="disabled")
                st.skip("disabled in config")
            else:
                vpn.ensure_connected("before 2D draft export")
                reselect(bs, sn, row, label)
                status, path, msg = download_2d_draft(bs, sn, serial_root)
                rec.set(pdf_status=status, pdf_path=str(path or ""))
                if status == "Downloaded":
                    st.ok(f"{path}" + (f" ({msg})" if msg else ""))
                elif status == "Not available":
                    st.warn("2D draft (PDF) is not available in Bluestar")
                else:
                    st.fail(msg)

        with tracker.step(rec, "P1.S7", "Download Standard BOM (Excel)", critical=False) as st:
            if not bs.bcfg["download_bom"]:
                rec.set(bom_status="disabled")
                st.skip("disabled in config")
            else:
                vpn.ensure_connected("before BOM export")
                reselect(bs, sn, row, label)
                status, path, msg = download_bom(bs, sn, serial_root, res["downloads"])
                rec.set(bom_status=status, bom_path=str(path or ""))
                if status == "Downloaded":
                    st.ok(msg)
                elif status in ("Not available", "Downloaded (empty)"):
                    st.warn(msg)
                    rec.add_manual(f"Standard BOM {status.lower()} - Checkpoint-5 uses occurrence properties")
                else:
                    st.fail(msg)
        if rec.steps["P1.S6"].status == "FAIL":
            rec.set(pdf_status="Failed")
        if rec.steps["P1.S7"].status == "FAIL":
            rec.set(bom_status="Failed")

        with tracker.step(rec, "P1.S8", "Copy to working folder (zip / move / unzip / delete zip)") as st:
            wc = make_working_copy(sn, res["myloadpoint"], res["working_root"],
                                   cfg["working_copy"]["clear_readonly"], cfg["working_copy"]["backup_existing"], log)
            rec.set(working_folder=str(wc["working_folder"]), working_status="Done")
            st.ok(f"{wc['files']} files -> {wc['working_folder']}"
                  + (f" (previous copy kept as {wc['backup']})" if wc["backup"] else ""))

        warn = any(rec.steps[k].status in ("WARN", "FAIL") for k, _ in PROCESS1_STEPS if k in rec.steps)
        rec.set(p1_status="WARN" if warn else "PASS")
        return True

    except StepFailed as e:
        rec.set(p1_status="FAIL", error=str(e))
        if not rec.data.get("download_status"):
            rec.set(download_status="Failed")
        if "working_status" not in rec.data:
            rec.set(working_status="Not done")
        tracker.skip_remaining(rec, PROCESS1_STEPS, "Previous step failed")
        try:
            bs.page and bs.page.keyboard.press("Escape")
        except Exception:
            pass
        return False
