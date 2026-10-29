"""Load config.yaml, merge with defaults and resolve all folders."""

import copy
from pathlib import Path

import yaml

from .utils import known_folder

PROJECT_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "input": {
        "machine_ids_file": "machine_ids.xlsx",
        "serial_column_candidates": ["machine_serials", "SR_NO", "serial_number", "serial", "machine_id"],
        "skip_already_completed": True,
    },
    "paths": {
        "myloadpoint": r"C:\myloadpoint",
        "working_folder_name": "AMF 3D Files - For Working",
        "documents_folder": "",
        "downloads_folder": "",
        "reports_folder": "reports",
        "browser_profile": "browser_profile",
    },
    "vpn": {
        "enabled": True,
        "adapter_description": "PANGP",
        "check_interval_seconds": 15,
        "auto_reconnect": True,
        "globalprotect_exe": r"C:\Program Files\Palo Alto Networks\GlobalProtect\PanGPA.exe",
        "reconnect_timeout_seconds": 120,
        "manual_wait_minutes": 15,
        "check_host": "",
    },
    "bluestar": {
        "url": "https://amf.operations.dynamics.com/?cmp=ausa&mi=BlueStar_ProductListPage",
        "client_exe": r"C:\Program Files\Bluestar5\Client\BlueStarClientServices.exe",
        "login_timeout_minutes": 10,
        "action_timeout_seconds": 60,
        "checkout": {
            "cad_system": "SolidEdge",
            "force_full_checkout": True,
            "download_timeout_minutes": 180,
            "stable_seconds": 90,
            "no_activity_minutes": 10,
            "retries": 1,
        },
        "download_2d_draft": True,
        "download_bom": True,
        "export_wait_minutes": 10,
    },
    "working_copy": {"clear_readonly": True, "backup_existing": True},
    "solidedge": {
        "enabled": True,
        "close_document_after_serial": True,
        "save_cleaned_assembly": True,
        "apply_part_style_recursive": False,
        "motor_face_style": "Red",
        "download_missing_from_bluestar": True,
        "use_2d_draft_crosscheck": True,
        "orientation_tolerance": 1.0,
        "step_file_extension": ".stp",
        "remove_laser_beams": True,
        "laser_backup_folder": "_laser_backup",
    },
    "classification": {
        "motor_keywords": ["MOTOR", "GEARMOTOR", "GMT", "GMT-DR", "SERVO MOTOR", "MTR"],
        "motor_exclude_words": ["MOUNT", "BRACKET", "KIT", "COVER", "SEAL", "HUB", "GUARD", "BASE", "PLATE"],
        "motor_categories": ["MOTORS"],
        "motor_part_numbers": [],
        "protected_categories": ["SAFETY", "STICKER", "DECAL", "NAMEPLATE", "LABEL", "ENCLOSURE",
                                 "OPERATOR INTERFACE", "ENCODER", "FASTENER", "HARDWARE"],
        "protected_keywords": ["SAFETY", "DECAL", "STICKER", "LABEL", "NAMEPLATE", "ENCLOSURE", "HMI", "OI",
                               "ENCODER", "RESOLVER", "BOLT", "SCREW", "NUT", "WASHER", "FASTENER"],
        "protected_title_words": ["MOUNT, PHOTOSWITCH", "PHOTOSWITCH", "PHOTOEYE", "PHOTO EYE", "SENSOR",
                                  "PROX", "PROXIMITY", "REFLECTOR"],
        "protected_exclude_words": ["BEARING", "GUIDE", "SHAFT"],
        "protect_inside_hidden_assemblies": False,
        "laser_part_numbers": ["66135", "602317"],
        "laser_keywords": ["LASER"],
        "laser_sensor_words": ["PE", "PHOTOEYE", "SENSOR", "DIFFUSE", "RETRO", "SCANNER", "DISTANCE"],
        "laser_sensor_categories": ["SENSOR", "PHOTO"],
        "laser_exclude_words": ["MOUNT", "BRACKET", "REFLECTOR", "GUARD", "CABLE", "COVER", "PLATE", "HOLDER"],
        "laser_beam_keywords": ["BEAM", "LASERBEAM", "LASER BEAM", "LASERSTRAHL", "STRAHL", "RAY"],
        "electrical_keywords_strong": ["ELECTRICAL", "CABLE", "WIRE", "HARNESS", "CONNECTOR", "SENSOR"],
        "electrical_keywords_weak": ["SWITCH", "LIGHT", "PANEL"],
        "electrical_categories": ["ELEC"],
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _abs(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_DIR / path


def load_config(path: str | Path | None = None) -> dict:
    cfg_path = _abs(str(path)) if path else PROJECT_DIR / "config.yaml"
    user = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, user)

    p = cfg["paths"]
    documents = Path(p["documents_folder"]) if p["documents_folder"] else known_folder("documents")
    downloads = Path(p["downloads_folder"]) if p["downloads_folder"] else known_folder("downloads")
    cfg["resolved"] = {
        "config_file": cfg_path,
        "project_dir": PROJECT_DIR,
        "input_file": _abs(cfg["input"]["machine_ids_file"]),
        "myloadpoint": Path(p["myloadpoint"]),
        "working_root": documents / p["working_folder_name"],
        "downloads": downloads,
        "reports": _abs(p["reports_folder"]),
        "browser_profile": _abs(p["browser_profile"]),
    }
    return cfg
