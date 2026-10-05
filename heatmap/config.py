"""Defaults and config loading.

The scheduling defaults mirror the timekeeping project so both tools agree on
what "on time" means. Anything here can be overridden in a JSON file
(see ``heatmap_config.example.json``).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULT_ACCDB_PATH = r"C:\access\CMS593.accdb"

DEFAULT_OFFICE_PREFIX = "N3"  # any postcode starting with this counts as "at office"
DEFAULT_OFFICE_POSTCODE = None  # exact office postcode; None = centre of the prefix outcode
DEFAULT_MIN_STOP_MIN = 30
DEFAULT_TARGET_OFFICE = "06:00"
DEFAULT_TARGET_SITE = "07:50"
DEFAULT_TARGET_WORK_END_WEEKDAY = "15:00"
DEFAULT_TARGET_WORK_END_FRIDAY = "14:00"
DEFAULT_TARGET_HOME_WEEKDAY = "16:00"
DEFAULT_TARGET_HOME_FRIDAY = "15:00"
DEFAULT_LATE_TOLERANCE = 5

CONTRACT_HOURS_MON_THU = 10.0
CONTRACT_HOURS_FRI = 9.0

# Straight-line distance x road factor / average speed = estimated drive time.
DEFAULT_ROAD_FACTOR = 1.3
DEFAULT_AVG_SPEED_KMH = 30.0

DEFAULTS: dict = {
    "accdb_path": DEFAULT_ACCDB_PATH,
    "office_prefix": DEFAULT_OFFICE_PREFIX,
    "office_postcode": DEFAULT_OFFICE_POSTCODE,
    "planning": {
        "min_stop_min": DEFAULT_MIN_STOP_MIN,
        "target_office": DEFAULT_TARGET_OFFICE,
        "target_site": DEFAULT_TARGET_SITE,
        "target_work_end_weekday": DEFAULT_TARGET_WORK_END_WEEKDAY,
        "target_work_end_friday": DEFAULT_TARGET_WORK_END_FRIDAY,
        "target_home_weekday": DEFAULT_TARGET_HOME_WEEKDAY,
        "target_home_friday": DEFAULT_TARGET_HOME_FRIDAY,
        "late_tolerance": DEFAULT_LATE_TOLERANCE,
        "contract_hours_mon_thu": CONTRACT_HOURS_MON_THU,
        "contract_hours_fri": CONTRACT_HOURS_FRI,
        "road_factor": DEFAULT_ROAD_FACTOR,
        "avg_speed_kmh": DEFAULT_AVG_SPEED_KMH,
        # Used when the board has no "needs materials" column (or it is blank).
        "default_needs_materials": False,
    },
    # Table / column mapping. ``null`` means "auto-detect" - run
    # ``python -m heatmap inspect`` to see what was picked and fix it here.
    "engineers": {
        "table": None,
        "id": None,
        "name": None,
        "home_postcode": None,
        "home_address": None,  # used to pull a postcode out when there is no postcode column
        "active": None,  # optional yes/no column; inactive engineers are skipped
    },
    "board": {
        "table": None,
        "date": None,
        "end_date": None,  # optional: multi-day bookings are expanded over working days
        "engineer": None,  # engineer id or name - matched against the engineers table
        "site_postcode": None,
        "site_address": None,
        "job_ref": None,
        "needs_materials": None,  # optional yes/no column
    },
    "window": {
        "days_back": 3,  # so Monday can see the previous Friday's sites
        "days_ahead": 14,
    },
    "geocode_cache": "postcode_cache.json",
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict:
    """Return DEFAULTS merged with the JSON file at ``path`` (if it exists)."""
    if path is None:
        path = Path("heatmap_config.json")
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    with path.open(encoding="utf-8") as fh:
        override = json.load(fh)
    override = {k: v for k, v in override.items() if not k.startswith("_")}
    return _merge(DEFAULTS, override)
