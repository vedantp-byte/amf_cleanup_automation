# AMF Solid Edge Clean Up Automation – User Manual

The tool takes a list of machine serial numbers and, for each one:

1. **Process 1 (Bluestar PLM):** downloads the 3D CAD (.asm), the 2D Draft (PDF), and the Standard BOM (Excel). It then copies everything to your working folder.
2. **Process 2 (Solid Edge):** cleans up the assembly against Checkpoints 1–7 and exports a cleaned STEP file.
3. **Reports:** writes a cleanup report for the serial and adds a row to the cycle summary report.

Everything runs on screen in Chrome and Solid Edge, so you can watch each step. The black console window shows every step's flag as it runs.

---

## 1. Steps to run (every time)

1. Connect **GlobalProtect VPN**.
2. Replace `machine_ids.xlsx` in this folder with your new list. Use one serial per row in the column `SR_NO` (the headers `machine_serials`, `serial`, and `machine_id` also work).
3. Double-click **`run.bat`** (or open a terminal here and run `python main.py`).
4. When it finishes, open the summary report shown at the end of the console output.

Serials that already succeeded in an earlier cycle are skipped automatically. To re-run a cycle that failed half-way, just run it again: only the unfinished and failed serials are processed.

## 2. One-time setup (new PC)

| What | How |
|---|---|
| Python 3.11 or newer | python.org, tick "Add to PATH" |
| Packages | double-click `setup.bat` (installs the packages and runs the self-check) |
| Google Chrome, GlobalProtect, Solid Edge (with the Bluestar client) | already used for the manual process |
| First Bluestar login | On the first run a separate Chrome window ("automation profile") opens. Sign in to Bluestar once; the login is remembered in the `browser_profile` folder. If Chrome asks to open **Bluestar Client Services**, tick *Always allow* and accept. |

Run `python main.py --check` at any time to verify the setup. It checks the packages, the input file, the VPN, Chrome, Solid Edge, folder write access, and free disk space.

## 3. What happens for each serial

| Flag | Step | Done by the tool |
|---|---|---|
| P1.S1 | VPN | GlobalProtect is checked before every Bluestar action and every 15 s in the background. If the VPN drops, the tool clicks *Connect* automatically. If that fails it beeps and waits up to 15 min for you to connect. If the VPN drops during a download, the check-out is repeated so that no files are missing. |
| P1.S2 | Serial check | Flags duplicates, invalid values (spaces, symbols, too short), and serials already done. Only new, distinct serials continue. |
| P1.S3 | Open Bluestar | Opens a new tab with the Bluestar product list URL. |
| P1.S4 | Search & revision | Searches `*SN*` on Object ID and keeps exact matches only. Picks the revision in this order: latest revision if Approved → latest approved revision → latest revision with any status → "All" filter → otherwise *Not Available in Bluestar*. |
| P1.S5 | 3D CAD check-out | Check out to `C:\myloadpoint\SN\SN` with Force full check out on. Answers the pop-ups automatically (CAD system = SolidEdge, approved revision = Yes, *Wait*, *I'm here*, OK). The download is complete when the folder stops changing. |
| P1.S6 | 2D Draft | File info → the PDF with the same name → Export file → `C:\myloadpoint\SN`. |
| P1.S7 | Standard BOM | BOM Lines → Open in Microsoft Office → BOM lines → Download, saved as `BOM lines_SN.xlsx` in `C:\myloadpoint\SN`. |
| P1.S8 | Working copy | Zips `C:\myloadpoint\SN`, moves the zip to `Documents\AMF 3D Files - For Working`, unzips it, then deletes the zip. A previous copy is kept as `SN_backup_<date>`. |
| P2.S1 | Input files | Finds the ASM, BOM, and PDF in the working folder. |
| P2.S2 | Inspect | Opens the top-level assembly and reads every level: subassemblies, parts, hidden items, missing files, properties. |
| P2.S3 | Checkpoint‑1 | Show All at every level. |
| P2.S4 | Checkpoint‑2 | Finds missing files, downloads them from Bluestar using the same search and revision rules, and repairs or replaces them. A file that isn't in Bluestar and has *Reports/Parts List = No* is ignored (rule 4.8); any other unrepaired file is reported for manual action. |
| P2.S5 | Occurrence Properties | Reads *Reports/Parts List* for every occurrence. |
| P2.S6 | Checkpoint‑3 | Checks Z-up from the model's extents. **The model is never rotated automatically**; any deviation is reported. |
| P2.S7 | Checkpoint‑4 | Applies *Use Part Style* to the top-level occurrences and colours motors red. |
| P2.S8–S11 | Checkpoint‑5 | First level is compared with the Standard BOM: in the BOM → Keep, not in the BOM → Hide, uncertain match → Keep and flag for review. Nested levels follow *Reports/Parts List*: Yes → Keep, No → Hide. Missing files are hidden. Electrical components are identified first and kept even if a rule would hide them; "possible" electrical items are flagged instead of guessed. The 2D draft text is used as an extra cross-check. Nothing is deleted; components are only hidden. |
| P2.S12 | Checkpoint‑6 validation | Runs the full checklist (orientation, structure, BOM, geometry, display) and re-reads the visibility of every occurrence. |
| P2.S13 | Checkpoint‑6 clean up | Hides reference planes, sketches, coordinate systems, and PMI. |
| P2.S14 | Checkpoint‑7 | Saves the cleaned ASM (working copy only) and exports `SN.stp`. |
| P2.S15 | Report | Writes `Cleanup_Report_SN.xlsx`. |

If a critical step fails, the remaining steps for that serial are marked SKIPPED and the tool moves on to the next serial.

## 4. Outputs

| File | Where |
|---|---|
| Cleaned STEP file `SN.stp` | `Documents\AMF 3D Files - For Working\SN\` |
| Cleanup report `Cleanup_Report_SN.xlsx` | same folder |
| Cycle summary report `summary_report_CYCLE_<date>.xlsx` | `reports\CYCLE_<date>\` |
| Console log `run.log`, error screenshots, `state.json` | `reports\CYCLE_<date>\` |
| All cycles ever run (history) | `reports\history\summary_history.xlsx` and `history.jsonl` |

**Summary report sheets**
- *Summary*: one row per serial. It includes the Bluestar status and revision, download / 2D Draft / BOM / working-folder status, component counts, missing files, Checkpoint‑1…7, STEP available, manual actions, errors, and timestamps.
- *Step Flags*: every step with its status, message, and times.
- *Step Matrix*: one row per serial, one column per step.
- *Cycle Info*: totals for the cycle.

**Cleanup report sheets**
- *Summary*: the standard summary block (input CAD, BOM, Z-up, components analysed / retained / hidden, electrical, ambiguous, STEP ready).
- *Components*: BOM level depth, occurrence path, part number, title, category, included in Standard BOM, Reports/Parts List, electrical, action taken, reason, and whether review is needed.
- *Missing Files*, *Checkpoints*, *Validation Checklist*.
- *Manual Actions*: what could not be done, why, which components are affected, and what to do.

**Status values:** PASS = done · WARN = done but needs a look · FAIL = step failed · SKIPPED = not run.
Overall status: **SUCCESS**, **SUCCESS - REVIEW** (STEP exported, open the Manual Actions sheet), **FAILED**, **SKIPPED - …** (duplicate / invalid / already done), or **NOT PROCESSED** (the cycle was stopped).

## 5. Options

```
python main.py                         full run with machine_ids.xlsx
python main.py --check                 environment self-check only
python main.py --serials 22158-1000ME  only these serials from the input file
python main.py --only process1         Bluestar download only
python main.py --only process2         Solid Edge clean up of existing working folders only
python main.py --force                 re-run serials that already succeeded
python main.py --input other.xlsx      use another input workbook
```

All settings are in `config.yaml`: folders, timeouts, VPN behaviour, the motor face style, and the electrical and motor keyword lists. Normally nothing needs to change.

## 6. Basic troubleshooting

| Symptom | Fix |
|---|---|
| `ACTION NEEDED: Please connect GlobalProtect VPN` and beeping | Connect GlobalProtect. The run continues by itself. After 15 min without a VPN the cycle stops, and the remaining serials are marked NOT PROCESSED. Run again later. |
| `ACTION NEEDED: please sign in to Bluestar` | Sign in inside the automation Chrome window (you have 10 minutes). |
| "automation Chrome profile is already open" | Close the Chrome window left over from the previous run, then start again. |
| Check-out starts but no files arrive | Check that Bluestar Client Services is running (tray icon). If Chrome asked to open it, allow it. Increase `no_activity_minutes` if the server is slow. |
| Download stops at the timeout on very large machines | Increase `bluestar.checkout.download_timeout_minutes`. |
| A Bluestar button "not found" | Bluestar was probably updated and a label changed. Look at the screenshot in `reports\CYCLE_…\screenshots`, then fix the text in `amf_automation\bluestar\selectors.py` (all labels are in that one file). |
| Summary report saved as `…_new.xlsx` | The report was open in Excel during the run. Close it next time. |
| Solid Edge does not start or open the file | Open Solid Edge manually once (license or first-start dialogs), then run `python main.py --only process2 --serials <SN>`. |
| STEP still contains hidden parts | In Solid Edge go to *Save As → STEP → Options* and make sure hidden parts are not exported. The setting is remembered. |
| Many "possible electrical" review items | Adjust `classification` keywords in `config.yaml`. |
| Orientation REVIEW / FAIL | Check the model in Solid Edge. The tool only reports; it never rotates. |

## 7. Notes

- Do not use the mouse or keyboard in the automation Chrome window or in Solid Edge while the run is active.
- `C:\myloadpoint` is only used for Bluestar downloads. All Solid Edge changes happen in the working copy, which is not synced to Bluestar.
- Ambiguous cases are never guessed. They are kept visible and listed under *Need review or manual intervention*.
