#!/usr/bin/env python3
"""
Engineer heat map + job planner - one file.

Reads the board in CMS593.accdb, finds each fitter's home postcode, and writes
an interactive map (output/engineer_heatmap.html) showing where engineers live,
where they're booked, and who's the best pick for each job - including whether
they should collect materials from the office in the morning or the day before.

    python engineer_heatmap.py            build the map from CMS593 and open it
    python engineer_heatmap.py --list     list the database's tables and columns
    python engineer_heatmap.py --demo     made-up data, no database needed

Needs: pip install pyodbc   (+ Microsoft Access Database Engine, same 32/64-bit as Python)
Everything you might want to change is in SETTINGS below.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import sys
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# =============================================================================
# SETTINGS
# =============================================================================
HERE = Path(__file__).resolve().parent

DEFAULT_ACCDB_PATH = r"C:\access\CMS593.accdb"

# The board table (the one with IDNo, FitRef, FitterName, JobDate, JobPostCode ...).
# None = find it automatically.
BOARD_TABLE = None
BOARD_COLUMNS = {
    "id": "IDNo",
    "fit_ref": "FitRef",
    "fitter_name": "FitterName",
    "pm": "ProjectMgr",
    "job_no": "JobNo",
    "date": "JobDate",
    "postcode": "JobPostCode",
    "job_type": "JobType",
    "job_value": "JobValue",
    "start_time": "StartTime",
    "provisional": "Provisional",
    "completed": "JobCompleted",
}
# Yes/No columns that mean the fitter is off that day rather than on a job.
ABSENCE_COLUMNS = {"Holiday": "Holiday", "Sick": "Sick", "BHoliday": "Bank holiday", "Unpaid": "Unpaid leave"}

# Where fitters' home addresses live. None = search the database for a table
# with fitter names + a postcode/address column. Set these if it picks wrong.
FITTERS_TABLE = None
FITTERS_ID_COLUMN = None        # matches the board's FitRef
FITTERS_NAME_COLUMN = None      # matches the board's FitterName
FITTERS_POSTCODE_COLUMN = None  # home postcode; if None the postcode is pulled from the address columns

# Extra/override home postcodes: FitRef,FitterName,HomePostcode. If no fitters
# table is found, a template with every current fitter is written for you to fill in.
FITTER_HOMES_CSV = HERE / "fitter_homes.csv"

# How far back the board is read. Older rows (back to 2012) are never fetched -
# the date filter runs inside Access. Fitters on the board in this period, plus
# everyone in fitter_homes.csv, are offered as candidates.
ACTIVE_LOOKBACK_DAYS = 14

DEFAULT_OFFICE_PREFIX = "N3"  # any postcode starting with this counts as "at office"
OFFICE_POSTCODE = None        # exact office postcode, e.g. "N3 1AB"; None = centre of N3
DEFAULT_MIN_STOP_MIN = 30
DEFAULT_TARGET_OFFICE = "06:00"
DEFAULT_TARGET_SITE = "07:50"   # used when the board's StartTime is blank
DEFAULT_TARGET_WORK_END_WEEKDAY = "15:00"
DEFAULT_TARGET_WORK_END_FRIDAY = "14:00"
DEFAULT_TARGET_HOME_WEEKDAY = "16:00"
DEFAULT_TARGET_HOME_FRIDAY = "15:00"
DEFAULT_LATE_TOLERANCE = 5
CONTRACT_HOURS_MON_THU = 10.0
CONTRACT_HOURS_FRI = 9.0

# Drive time estimate = straight-line distance x ROAD_FACTOR / AVG_SPEED_KMH.
ROAD_FACTOR = 1.3
AVG_SPEED_KMH = 30.0
# No materials column on the board, so this is the starting value (tick per job on the map).
DEFAULT_NEEDS_MATERIALS = False

# The map shows today and everything after it. Past days are never shown; the
# previous working day is still read so "collect materials the day before" works.
DAYS_AHEAD = None  # None = every upcoming booking; or a number of days
# Map background: a still picture of the area, saved next to the map in
# output/tiles. The pieces are downloaded once from OpenStreetMap (free, no key),
# kept for TILE_CACHE_DAYS, and only new areas are fetched after that. The map
# then opens straight from the file - no server, no account.
TILE_SOURCE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
TILE_USER_AGENT = "EngineerHeatmap/1.0 (internal job-planning map; python urllib)"
TILE_ZOOMS = range(8, 13)  # 8 = whole South East ... 12 = main roads and towns
TILE_MAX = 400             # cap per area, so a far-away job can't trigger a big download
TILE_CACHE_DAYS = 30

OUTPUT_DIR = HERE / "output"
GEOCODE_CACHE = HERE / "postcode_cache.json"


def planning_settings() -> dict:
    return {
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
        "road_factor": ROAD_FACTOR,
        "avg_speed_kmh": AVG_SPEED_KMH,
        "default_needs_materials": DEFAULT_NEEDS_MATERIALS,
    }


# =============================================================================
# Postcodes
# =============================================================================
_FULL_PC = re.compile(
    r"\b(GIR ?0AA|[A-PR-UWYZ][A-HK-Y]?[0-9][0-9A-HJKMNPR-Y]? ?[0-9][ABD-HJLNP-UW-Z]{2})\b", re.IGNORECASE)
_OUTCODE = re.compile(r"^[A-PR-UWYZ][A-HK-Y]?[0-9][0-9A-HJKMNPR-Y]?$", re.IGNORECASE)


def normalise_postcode(value) -> str | None:
    """'n31ab' -> 'N3 1AB'; outcodes ('N3') pass through; anything else -> None."""
    if value is None:
        return None
    compact = re.sub(r"\s+", "", str(value)).upper()
    if _OUTCODE.match(compact):
        return compact
    if len(compact) >= 5 and _FULL_PC.fullmatch(compact):
        return f"{compact[:-3]} {compact[-3:]}"
    return None


def find_postcode(*texts) -> str | None:
    """First postcode in any of the texts (a postcode field or a free-text address)."""
    for text in texts:
        pc = normalise_postcode(text)
        if pc:
            return pc
        match = _FULL_PC.search(str(text or "").upper())
        if match:
            return normalise_postcode(match.group(1))
    return None


def outcode(pc: str) -> str:
    return pc.split(" ")[0]


# =============================================================================
# Database
# =============================================================================
def _q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _decode_utf16(raw):
    # The Access driver occasionally hands back odd bytes; don't let one bad
    # character stop the whole run.
    return None if raw is None else raw.decode("utf-16-le", errors="replace")


class AccessDB:
    """Read-only access to the .accdb.

    Deliberately avoids pyodbc's cursor.columns(): with the Access driver it
    can fail with "'utf-16-le' codec can't decode bytes ... illegal encoding".
    Column names come from an empty SELECT instead.
    """

    def __init__(self, path: str):
        try:
            import pyodbc
        except ImportError:
            raise SystemExit("pyodbc isn't installed - run:  pip install pyodbc") from None
        if not Path(path).exists():
            raise SystemExit(f"Database not found: {path}  (change DEFAULT_ACCDB_PATH at the top of this file)")
        drivers = [d for d in pyodbc.drivers() if "Access Driver" in d]
        if not drivers:
            raise SystemExit(
                "No Microsoft Access ODBC driver found. Install the 'Microsoft Access Database Engine' "
                f"({'64' if sys.maxsize > 2**32 else '32'}-bit, to match your Python).")
        self.pyodbc = pyodbc
        self.conn = pyodbc.connect(f"DRIVER={{{drivers[0]}}};DBQ={path};ReadOnly=1;", autocommit=True)
        for sql_type in (pyodbc.SQL_WVARCHAR, pyodbc.SQL_WCHAR, pyodbc.SQL_WLONGVARCHAR):
            self.conn.add_output_converter(sql_type, _decode_utf16)

    def tables(self) -> list[str]:
        try:
            names = [r[2] for r in self.conn.cursor().tables(tableType="TABLE")]
        except Exception:
            try:  # fallback: Access's own catalogue (may be locked down)
                cur = self.conn.cursor()
                cur.execute("SELECT Name, Type, Flags FROM MSysObjects")
                names = [r[0] for r in cur.fetchall() if r[1] in (1, 4, 6) and not str(r[0]).startswith("MSys")]
            except Exception:
                names = []
        return sorted(n for n in names if n and not str(n).startswith("MSys") and not str(n).startswith("~"))

    def columns(self, table: str) -> list[str]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT * FROM {_q(table)} WHERE 1=0")
        return [d[0] for d in cur.description]

    def rows(self, table, columns, date_col=None, date_from=None, date_to=None) -> list[dict]:
        sql = f"SELECT {', '.join(_q(c) for c in columns)} FROM {_q(table)}"
        params = []
        if date_col:
            sql += f" WHERE {_q(date_col)} >= ? AND {_q(date_col)} < ?"
            params = [date_from, date_to]
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return [dict(zip(columns, r)) for r in cur.fetchall()]


class MemoryDB:
    """Same interface over {"table": [row dicts]} - for --demo and tests."""

    def __init__(self, data: dict):
        self.data = data

    def tables(self):
        return sorted(self.data)

    def columns(self, table):
        return list(self.data[table][0]) if self.data[table] else []

    def rows(self, table, columns, date_col=None, date_from=None, date_to=None):
        out = []
        for row in self.data[table]:
            if date_col and not (row.get(date_col) and date_from <= row[date_col] < date_to):
                continue
            out.append({c: row.get(c) for c in columns})
        return out


def _k(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _key(value) -> str:
    """Comparable id/name: 12, 12.0 and '12' all become '12'; text is lower-cased."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    try:
        if not isinstance(value, (int, str)) and float(value).is_integer():  # Decimal
            value = int(value)
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _yes(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("y", "yes", "true", "1", "-1")
    return bool(value)


def _as_date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None


def _as_time(value) -> str | None:
    """Access stores times as 1899-12-30 HH:MM. Midnight means 'not set'."""
    if isinstance(value, dt.datetime) and (value.hour or value.minute):
        return f"{value.hour:02d}:{value.minute:02d}"
    if isinstance(value, dt.time) and (value.hour or value.minute):
        return f"{value.hour:02d}:{value.minute:02d}"
    return None


def find_board_table(db) -> str:
    if BOARD_TABLE:
        return BOARD_TABLE
    need = {_k(BOARD_COLUMNS[c]) for c in ("date", "fit_ref", "postcode")}
    for table in db.tables():
        try:
            if need <= {_k(c) for c in db.columns(table)}:
                return table
        except Exception:
            continue  # linked tables that can't be opened, etc.
    raise SystemExit("Couldn't find the board table (one with JobDate, FitRef and JobPostCode). "
                     "Set BOARD_TABLE at the top of this file - run with --list to see the tables.")


def find_fitters_table(db, board_table: str, board_names: set[str], board_refs: set[str]):
    """Pick the table holding fitters' home addresses by checking which one actually
    contains the board's fitter names. Returns (table, id_col, name_cols, postcode_col, address_cols) or None."""
    candidates = [FITTERS_TABLE] if FITTERS_TABLE else [t for t in db.tables() if t != board_table]
    best, best_score = None, 0
    for table in candidates:
        try:
            cols = db.columns(table)
        except Exception:
            continue
        keys = {c: _k(c) for c in cols}
        pc_col = FITTERS_POSTCODE_COLUMN or next(
            (c for c in sorted(cols, key=lambda c: "home" not in keys[c])
             if any(w in keys[c] for w in ("postcode", "pcode", "postalcode")) or keys[c] == "pc"), None)
        addr_cols = [c for c in cols if any(w in keys[c] for w in ("address", "addr", "street", "town", "city"))]
        if not (pc_col or addr_cols):
            continue
        if FITTERS_NAME_COLUMN:
            name_cols = [FITTERS_NAME_COLUMN]
        else:
            first = next((c for c in cols if keys[c] in ("firstname", "forename", "fname")), None)
            last = next((c for c in cols if keys[c] in ("surname", "lastname", "lname")), None)
            name_cols = [c for c in cols if "name" in keys[c] and c not in (first, last)
                         and not any(w in keys[c] for w in ("user", "company", "customer", "client", "file"))]
            name_cols = sorted(name_cols, key=lambda c: ("fit" not in keys[c], len(c)))
            if first and last:
                name_cols.append(first + "+" + last)
        id_col = FITTERS_ID_COLUMN or next(
            (c for c in sorted(cols, key=lambda c: (keys[c] not in ("fitref", "fitterref", "fitterid", "fitid"), len(c)))
             if keys[c] in ("fitref", "fitterref", "fitterid", "fitid", "id", "idno", "staffid", "employeeid")), None)
        read = sorted({c for c in [id_col, pc_col, *addr_cols] if c}
                      | {p for n in name_cols for p in n.split("+")})
        try:
            rows = db.rows(table, read)
        except Exception:
            continue
        if not rows or len(rows) > 20000:
            continue
        index = NameIndex({"name": n} for n in board_names)
        name_hits = {n: sum(1 for r in rows if _name(r, n) and index.get(_name(r, n))) for n in name_cols}
        name_col = max(name_hits, key=name_hits.get) if name_hits else None
        hits = name_hits.get(name_col, 0)
        id_hits = sum(1 for r in rows if _key(r.get(id_col)) in board_refs) if id_col else 0
        fitterish = any(w in _k(table) for w in ("fit", "engineer", "staff", "employee", "operative"))
        score = hits * 10 + (id_hits if fitterish or hits else 0)
        if score > best_score:
            best, best_score = (table, id_col, name_col, pc_col, addr_cols, rows), score
    return best


def _name(row: dict, name_col: str | None) -> str:
    if not name_col:
        return ""
    return _key(" ".join(_text(row.get(p)) for p in name_col.split("+") if _text(row.get(p))))


def _tokens(name) -> list[str]:
    return re.sub(r"[^a-z ]", " ", _key(name)).split()


class NameIndex:
    """Match names loosely: 'Jane A Smith' = 'Jane Smith' = 'Jane S' = 'Jane'
    (the last two only when no other fitter shares that first name)."""

    def __init__(self, people):
        self.people = list(people)

    def get(self, name):
        want = _tokens(name)
        if not want:
            return None
        hits = []
        for p in self.people:
            have = _tokens(p["name"])
            if not have:
                continue
            if have == want or (have[0] == want[0] and have[-1] == want[-1]):
                return p
            if have[0] != want[0]:
                continue
            if len(have) == 1 or len(want) == 1:  # first name only
                hits.append(p)
            elif min(len(have[-1]), len(want[-1])) == 1 and have[-1][0] == want[-1][0]:  # surname initial
                hits.append(p)
        return hits[0] if len(hits) == 1 else None


# =============================================================================
# Geocoding (postcodes.io - free, no key), cached in postcode_cache.json
# =============================================================================
API = "https://api.postcodes.io"


def _http(method: str, url: str, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "engineer-heatmap"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


class Geocoder:
    def __init__(self, cache_path: Path | None, fetch=_http):
        self.cache_path, self.fetch = cache_path, fetch
        self.cache = {}
        if cache_path and cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def lookup(self, raw) -> dict:
        wanted = {pc for pc in (normalise_postcode(r) for r in raw) if pc}
        missing = sorted(pc for pc in wanted if pc not in self.cache)
        full = [pc for pc in missing if " " in pc]
        try:
            for i in range(0, len(full), 100):
                resp = self.fetch("POST", f"{API}/postcodes", {"postcodes": full[i:i + 100]}) or {}
                for item in resp.get("result") or []:
                    res = item.get("result")
                    if res and res.get("latitude") is not None:
                        self.cache[normalise_postcode(item["query"])] = {
                            "lat": res["latitude"], "lon": res["longitude"], "approx": False}
            for pc in missing:  # retired postcodes, then the outcode centre
                if pc in self.cache:
                    continue
                res = None
                if " " in pc:
                    res = (self.fetch("GET", f"{API}/terminated_postcodes/{pc.replace(' ', '')}") or {}).get("result")
                approx = not res
                if not res:
                    res = (self.fetch("GET", f"{API}/outcodes/{outcode(pc)}") or {}).get("result")
                if res and res.get("latitude") is not None:
                    self.cache[pc] = {"lat": res["latitude"], "lon": res["longitude"], "approx": approx}
        except (urllib.error.URLError, OSError) as exc:
            print(f"  ! couldn't reach postcodes.io ({exc}); using cached postcodes only")
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")
        return {pc: self.cache[pc] for pc in wanted if pc in self.cache}


# =============================================================================
# Build the dataset
# =============================================================================
def _working_days(first: dt.date, last: dt.date):
    d = first
    while d <= last:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def load_homes_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh)]


def write_homes_template(path: Path, fitters: dict) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["FitRef", "FitterName", "HomePostcode"])
        for f in sorted(fitters.values(), key=lambda f: f["name"].lower()):
            w.writerow([f["ref"], f["name"], f.get("postcode") or ""])


def previous_working_day(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def load_dataset(db, today: dt.date, geocoder: Geocoder, label: str, days_ahead: int | None = None) -> dict:
    """Bookings from today onwards (plus the previous working day, for planning only)."""
    start = previous_working_day(today)
    end = today + dt.timedelta(days=days_ahead + 1) if days_ahead is not None else dt.date(2100, 1, 1)
    warnings: list[str] = []
    C = BOARD_COLUMNS

    print("Finding the board table ...")
    board = find_board_table(db)
    board_cols = {_k(c): c for c in db.columns(board)}
    wanted = {k: board_cols.get(_k(v)) for k, v in C.items()}
    absence = {board_cols[_k(c)]: label_ for c, label_ in ABSENCE_COLUMNS.items() if _k(c) in board_cols}
    for k in ("date", "postcode"):
        if not wanted[k]:
            raise SystemExit(f"Board table '{board}' has no {C[k]} column.")
    print(f"  board = {board}")

    lookback = today - dt.timedelta(days=ACTIVE_LOOKBACK_DAYS)
    print(f"Reading board rows from {lookback} onwards ...")
    rows = db.rows(board, [c for c in {*wanted.values(), *absence} if c], wanted["date"],
                   dt.datetime.combine(lookback, dt.time()), dt.datetime.combine(end, dt.time()))
    print(f"  {len(rows)} rows")

    def fitter_of(row):
        ref = row.get(wanted["fit_ref"]) if wanted["fit_ref"] else None
        name = _text(row.get(wanted["fitter_name"])) if wanted["fitter_name"] else ""
        ref_key = _key(ref) if ref not in (None, 0, "0", "") else ""
        if not ref_key and not name:
            return None
        return ref_key or "name:" + _key(name), ref_key, name

    # Fitters seen on the board recently = the people we can plan with.
    fitters: dict[str, dict] = {}
    for row in rows:
        f = fitter_of(row)
        if f:
            eid, ref, name = f
            fitters.setdefault(eid, {"id": eid, "ref": ref, "name": name or f"Fitter {ref}", "postcode": None})

    # ---- home postcodes ----
    print("Looking for fitters' home addresses ...")
    names = {_key(f["name"]) for f in fitters.values()}
    refs = {f["ref"] for f in fitters.values() if f["ref"]}
    found = find_fitters_table(db, board, names, refs)
    by_ref = {f["ref"]: f for f in fitters.values() if f["ref"]}
    by_name = NameIndex(fitters.values())
    if found:
        table, id_col, name_col, pc_col, addr_cols, frows = found
        print(f"  using table '{table}' (id={id_col}, name={name_col}, postcode={pc_col}, address={addr_cols})")
        for r in frows:
            f = by_ref.get(_key(r.get(id_col))) if id_col else None
            f = f or by_name.get(_name(r, name_col))
            if f and not f["postcode"]:
                f["postcode"] = find_postcode(r.get(pc_col) if pc_col else None, *(r.get(c) for c in addr_cols))
    else:
        print("  no table with fitters' home addresses found")

    homes = load_homes_csv(FITTER_HOMES_CSV)
    for r in homes:
        pc = find_postcode(r.get("HomePostcode"))
        name = _text(r.get("FitterName"))
        f = by_ref.get(_key(r.get("FitRef"))) or by_name.get(name)
        if not f and name:  # not on the board lately, but still someone we can send
            f = fitters.setdefault("name:" + _key(name), {"id": "name:" + _key(name), "ref": "", "name": name,
                                                           "postcode": None})
        if f and pc:
            f["postcode"] = pc  # the CSV wins, so it can correct the database
    if homes:
        print(f"  + {FITTER_HOMES_CSV.name}: {len(homes)} rows")
    if not found and not homes and fitters:
        write_homes_template(FITTER_HOMES_CSV, fitters)
        warnings.append(f"No home addresses found in the database. Fill in HomePostcode in "
                        f"{FITTER_HOMES_CSV} (or set FITTERS_TABLE) and run again.")

    # ---- bookings in the window ----
    bookings = []
    for row in rows:
        day = _as_date(row.get(wanted["date"]))
        if not day or not (start <= day < end):
            continue
        f = fitter_of(row)
        off = [lbl for col, lbl in absence.items() if _yes(row.get(col))]
        get = lambda k: row.get(wanted[k]) if wanted[k] else None  # noqa: E731
        job_no = get("job_no")
        bookings.append({
            "id": f"b{_text(get('id')) or len(bookings)}-{day.isoformat()}",
            "date": day.isoformat(),
            "engineerId": f[0] if f else None,
            "off": ", ".join(off) if off else None,
            "postcode": None if off else find_postcode(get("postcode")),
            "ref": "" if job_no in (None, 0) else f"Job {_key(job_no)}",
            "pm": _text(get("pm")),
            "jobType": _text(get("job_type")),
            "jobValue": float(get("job_value")) if get("job_value") not in (None, "") else None,
            "startTime": _as_time(get("start_time")),
            "provisional": _yes(get("provisional")),
            "completed": _text(get("completed")),
            "needsMaterials": None,
            "address": "",
        })

    # ---- geocode ----
    print("Looking up postcodes ...")
    office_pc = normalise_postcode(OFFICE_POSTCODE) or normalise_postcode(DEFAULT_OFFICE_PREFIX)
    locs = geocoder.lookup([office_pc] + [f["postcode"] for f in fitters.values()] + [b["postcode"] for b in bookings])

    def place(item):
        loc = locs.get(item["postcode"]) if item["postcode"] else None
        item.update(lat=loc and loc["lat"], lon=loc and loc["lon"], approx=bool(loc and loc["approx"]))

    for f in fitters.values():
        place(f)
    for b in bookings:
        place(b)

    no_home = sorted(f["name"] for f in fitters.values() if f["lat"] is None)
    if no_home:
        warnings.append(f"{len(no_home)} fitter(s) have no usable home postcode, so can't be ranked: "
                        + ", ".join(no_home[:12]) + (" ..." if len(no_home) > 12 else "")
                        + f". Add them to {FITTER_HOMES_CSV.name}.")
    unmapped = sum(1 for b in bookings if not b["off"] and b["lat"] is None)
    if unmapped:
        warnings.append(f"{unmapped} booking(s) have a missing or unknown JobPostCode and aren't on the map.")
    if office_pc not in locs:
        raise SystemExit(f"Couldn't locate the office ({office_pc}) - check the internet connection or set OFFICE_POSTCODE.")

    return {
        "generatedAt": dt.datetime.now().isoformat(timespec="minutes"),
        "source": label,
        "today": today.isoformat(),
        "lastDate": max([b["date"] for b in bookings] + [today.isoformat()]),
        "office": {"prefix": DEFAULT_OFFICE_PREFIX, "postcode": office_pc, **locs[office_pc]},
        "settings": planning_settings(),
        "engineers": sorted(fitters.values(), key=lambda f: f["name"].lower()),
        "bookings": bookings,
        "warnings": warnings,
    }


def _tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    lat = max(min(lat, 85.0), -85.0)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def _http_tile(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": TILE_USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def prepare_tiles(points, folder: Path, fetch=_http_tile) -> dict | None:
    """Make sure the map pictures covering ``points`` exist in ``folder``.

    Returns what the page needs to show them, or None if there are none.
    """
    points = [(lat, lon) for lat, lon in points if lat is not None]
    if not points:
        return None
    pad = 0.2  # a margin so the edges of the area aren't blank
    south, north = min(p[0] for p in points) - pad, max(p[0] for p in points) + pad
    west, east = min(p[1] for p in points) - pad, max(p[1] for p in points) + pad

    plan, max_zoom = [], None
    for z in TILE_ZOOMS:
        x0, y0 = _tile_xy(north, west, z)
        x1, y1 = _tile_xy(south, east, z)
        tiles = [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        if plan and len(plan) + len(tiles) > TILE_MAX:
            break
        plan += tiles
        max_zoom = z

    fresh = dt.datetime.now().timestamp() - TILE_CACHE_DAYS * 86400
    todo = [t for t in plan if not (folder / f"{t[0]}/{t[1]}/{t[2]}.png").exists()
            or (folder / f"{t[0]}/{t[1]}/{t[2]}.png").stat().st_mtime < fresh]
    if todo:
        print(f"Downloading {len(todo)} map pieces (one-off; reused for {TILE_CACHE_DAYS} days) ...")
    failures = 0
    for i, (z, x, y) in enumerate(todo, 1):
        path = folder / f"{z}/{x}/{y}.png"
        try:
            data = fetch(TILE_SOURCE.format(z=z, x=x, y=y))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            failures = 0
        except Exception as exc:  # keep any older copy; give up if the server is unreachable
            failures += 1
            if failures >= 5:
                print(f"  ! couldn't download the map background ({exc}); the map will show without it")
                break
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}")

    have = [t for t in plan if (folder / f"{t[0]}/{t[1]}/{t[2]}.png").exists()]
    if not have:
        return None
    return {
        "url": f"{folder.name}/{{z}}/{{x}}/{{y}}.png",
        "minZoom": TILE_ZOOMS[0],
        "maxNativeZoom": max_zoom,
        "bounds": [[south, west], [north, east]],
        "attribution": TILE_ATTRIBUTION,
    }


def render_html(dataset: dict) -> str:
    payload = json.dumps(dataset, default=str, separators=(",", ":")).replace("</", "<\\/")
    page = PAGE_HTML
    for marker, content in (("/*__CSS__*/", PAGE_CSS), ("/*__PLANNER__*/", PLANNER_JS),
                            ("/*__APP__*/", APP_JS), ("__DATA__", payload)):
        page = page.replace(marker, content, 1)
    return page


# =============================================================================
# Demo data (--demo): made-up fitters around London, no database needed
# =============================================================================
DEMO_OUTCODES = {
    "N3": (51.6007, -0.1925), "N12": (51.6146, -0.1765), "EN5": (51.6514, -0.2003), "HA8": (51.6133, -0.2750),
    "NW7": (51.6148, -0.2449), "NW4": (51.5899, -0.2234), "N14": (51.6327, -0.1293), "E17": (51.5862, -0.0198),
    "E4": (51.6281, -0.0040), "IG8": (51.6082, 0.0306), "RM7": (51.5770, 0.1720), "DA1": (51.4440, 0.2140),
    "SE9": (51.4440, 0.0580), "BR1": (51.4060, 0.0150), "CR0": (51.3760, -0.0900), "SM1": (51.3650, -0.1900),
    "KT1": (51.4100, -0.3000), "TW3": (51.4680, -0.3610), "UB3": (51.5040, -0.4180), "HA4": (51.5720, -0.4200),
    "WD6": (51.6560, -0.2730), "AL1": (51.7500, -0.3360), "CM1": (51.7350, 0.4690), "RH1": (51.2400, -0.1700),
    "EC1A": (51.5200, -0.0990), "EC2A": (51.5240, -0.0820), "W1T": (51.5200, -0.1370), "SW1A": (51.5010, -0.1410),
    "SE1": (51.4990, -0.0890), "E14": (51.5050, -0.0200), "NW1": (51.5320, -0.1430), "W2": (51.5150, -0.1800),
    "N1": (51.5380, -0.1000), "E1": (51.5170, -0.0590), "SW11": (51.4660, -0.1650), "W6": (51.4930, -0.2290),
}
DEMO_SITES = ["EC1A", "EC2A", "W1T", "SW1A", "SE1", "E14", "NW1", "W2", "N1", "E1", "SW11", "W6", "KT1", "N3", "AL1"]


def demo_fetch(method, url, body=None):
    """Offline stand-in for postcodes.io."""
    def locate(pc):
        if outcode(pc) not in DEMO_OUTCODES:
            return None
        lat, lon = DEMO_OUTCODES[outcode(pc)]
        h = hashlib.md5(pc.encode()).digest()
        return {"latitude": lat + (h[0] - 128) / 128 * 0.012, "longitude": lon + (h[1] - 128) / 128 * 0.018}
    if method == "POST":
        return {"result": [{"query": pc, "result": locate(pc)} for pc in body["postcodes"]]}
    tail = url.rsplit("/", 1)[-1]
    return {"result": locate(tail + " 1AA")} if "/outcodes/" in url and tail in DEMO_OUTCODES else None


def demo_tables(today: dt.date, seed: int = 7) -> dict:
    rng = random.Random(seed)
    pc = lambda out: f"{out} {rng.randint(1, 9)}{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}"  # noqa: E731
    first = ["Adam", "Ben", "Callum", "Dan", "Eli", "Femi", "George", "Harry", "Imran", "Jack", "Kofi", "Liam",
             "Marek", "Nathan", "Owen", "Paul", "Rory", "Sam", "Tom", "Vik", "Will", "Yusuf"]
    last = ["Ahmed", "Brown", "Clarke", "Davies", "Evans", "Fisher", "Green", "Hughes", "Iqbal", "Jones", "Khan",
            "Lewis", "Morgan", "Novak", "Okafor", "Patel", "Quinn", "Reid", "Smith", "Taylor", "Usman", "Walsh"]
    homes = list(DEMO_OUTCODES)[:24]
    fitters = [{"FitterID": 10 + i, "FitterName": f"{first[i]} {last[i]}",
                "Address": f"{rng.randint(1, 99)} Example Road, London {pc(homes[i % len(homes)])}"}
               for i in range(len(first))]
    board, n = [], 0
    for d in range(-ACTIVE_LOOKBACK_DAYS // 2, 20):
        day = today + dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        crew = rng.sample(fitters, 18)
        for f in crew[:2]:  # someone's always off
            n += 1
            board.append({"IDNo": n, "FitRef": f["FitterID"], "FitterName": f["FitterName"], "JobDate": dt.datetime.combine(day, dt.time()),
                          "Holiday": True, "Sick": False, "BHoliday": False, "Unpaid": False, "JobPostCode": None})
        crew = crew[2:]
        while crew:
            team, crew = crew[:rng.choice((1, 1, 2))], crew[len(crew[:1]):]
            team = [team[0]] + ([crew.pop(0)] if len(team) > 1 and crew else [])
            site, job = pc(rng.choice(DEMO_SITES)), rng.randint(40000, 49999)
            for f in team:
                n += 1
                board.append({"IDNo": n, "FitRef": f["FitterID"], "FitterName": f["FitterName"], "JobNo": job,
                              "JobDate": dt.datetime.combine(day, dt.time()), "ProjectMgr": rng.choice(["Ali", "Sarah", "Dev"]),
                              "JobPostCode": site, "JobType": rng.choice(["Install", "Survey", "Remedial"]),
                              "JobValue": rng.randint(5, 80) * 100, "Provisional": rng.random() < 0.2,
                              "StartTime": dt.datetime(1899, 12, 30, 8, 30) if rng.random() < 0.15 else None,
                              "Holiday": False, "Sick": False, "BHoliday": False, "Unpaid": False})
        for _ in range(rng.randint(2, 4)):
            n += 1
            board.append({"IDNo": n, "FitRef": 0, "FitterName": None, "JobNo": rng.randint(40000, 49999),
                          "JobDate": dt.datetime.combine(day, dt.time()), "ProjectMgr": rng.choice(["Ali", "Sarah", "Dev"]),
                          "JobPostCode": pc(rng.choice(DEMO_SITES)), "JobType": "Install", "JobValue": 2500,
                          "Holiday": False, "Sick": False, "BHoliday": False, "Unpaid": False})
    cols = ["IDNo", "FitRef", "FitterName", "ProjectMgr", "JobNo", "JobDate", "Hours", "Holiday", "Sick", "Notes",
            "Provisional", "Definite", "WRSREQ", "OfficeID", "JobValue", "JobPostCode", "QC", "QCDetails", "BHoliday",
            "Unpaid", "JobType", "ConfirmVisit", "QuoteNo", "StartTime", "JobCompleted"]
    return {"Board": [{c: r.get(c) for c in cols} for r in board], "Fitters": fitters,
            "Customers": [{"ID": 1, "Name": "Acme Ltd", "PostCode": "EC1A 1BB"}]}


# =============================================================================
# The map page (HTML / CSS / JavaScript), written out with the data inside
# =============================================================================
PAGE_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Engineer Heat Map</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
/*__CSS__*/
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <header class="head">
      <h1>Engineer heat map</h1>
      <p class="meta" id="meta"></p>
      <details class="warn" id="warnings" hidden><summary></summary><ul></ul></details>
    </header>

    <section class="block">
      <div class="daybar">
        <button type="button" id="prevDay" aria-label="Previous day">&#8249;</button>
        <select id="daySelect" aria-label="Date"></select>
        <button type="button" id="nextDay" aria-label="Next day">&#8250;</button>
      </div>
      <div class="layers" role="group" aria-label="Map layers">
        <label><input type="checkbox" id="lySitesHeat" checked><span class="sw sw-site"></span>Jobs heat (all upcoming)</label>
        <label><input type="checkbox" id="lyHomes" checked><span class="dot dot-home"></span>Engineer homes</label>
        <label><input type="checkbox" id="lyLinks" checked><span class="line"></span>Home &rarr; site</label>
      </div>
    </section>

    <section class="block" id="listView">
      <div class="row-between"><h2 id="listTitle">Jobs</h2><span class="muted" id="listCount"></span></div>
      <label class="pmfilter">Project manager <select id="pmSelect"><option value="">Everyone</option></select></label>
      <ul class="jobs" id="jobList"></ul>

      <details class="plan" id="planBox">
        <summary>Plan a job that isn't on the board</summary>
        <form id="planForm" autocomplete="off">
          <label>Site postcode<input name="postcode" required placeholder="e.g. EC1A 1BB"></label>
          <label>Date<input name="date" type="date" required></label>
          <label>Job ref<input name="ref" placeholder="optional"></label>
          <label class="check"><input type="checkbox" name="needsMaterials"> Needs materials from the office</label>
          <button type="submit">Find best engineers</button>
          <p class="muted" id="planMsg" role="status"></p>
        </form>
      </details>

      <details class="plan">
        <summary>Travel &amp; timing settings</summary>
        <form id="settingsForm" class="grid2"></form>
      </details>
    </section>

    <section class="block" id="jobView" hidden>
      <button type="button" class="back" id="backBtn">&larr; All jobs</button>
      <div id="jobHead"></div>
      <label class="check"><input type="checkbox" id="jobMaterials"> Needs materials from the office</label>
      <ol class="cands" id="candList"></ol>
      <button type="button" class="link" id="moreBtn" hidden></button>
      <details class="plan" id="busyBox"><summary></summary><ul class="busy"></ul></details>
    </section>
  </aside>
  <main id="map" aria-label="Map"></main>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
/*__PLANNER__*/
</script>
<script>
/*__APP__*/
</script>
</body>
</html>
'''

PAGE_CSS = r''':root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --muted: #898781;
  --hair: #e1e0d9;
  --border: rgba(11, 11, 11, 0.10);
  --home: #2a78d6;      /* series 1: where engineers live */
  --site: #eb6834;      /* series 2: booked sites */
  --open: #1baf7a;      /* series 3: unassigned jobs */
  --office: #0b0b0b;
  --good: #0ca30c;
  --good-ink: #006300;
  --serious: #ec835a;
  --critical: #d03b3b;
  --accent-wash: rgba(42, 120, 214, 0.10);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(0.85) contrast(0.9); }
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --hair: #2c2c2a;
    --border: rgba(255, 255, 255, 0.10);
    --home: #3987e5;
    --site: #d95926;
    --open: #199e70;
    --office: #ffffff;
    --good-ink: #0ca30c;
    --accent-wash: rgba(57, 135, 229, 0.18);
  }
}
:root[data-theme="dark"] .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(0.85) contrast(0.9); }
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --hair: #2c2c2a;
  --border: rgba(255, 255, 255, 0.10); --home: #3987e5; --site: #d95926; --open: #199e70;
  --office: #ffffff; --good-ink: #0ca30c; --accent-wash: rgba(57, 135, 229, 0.18);
}

* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--page);
  color: var(--ink);
  font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.app { display: grid; grid-template-columns: 380px 1fr; height: 100vh; }
.side { background: var(--surface); border-right: 1px solid var(--border); overflow-y: auto; }
#map { height: 100%; min-height: 320px; background: var(--page); }

.head { padding: 16px 16px 8px; }
h1 { font-size: 18px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0; }
.meta, .muted { color: var(--ink-2); font-size: 12px; margin: 0; }
.block { padding: 12px 16px; border-top: 1px solid var(--hair); }
.row-between { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }

button, select, input { font: inherit; color: inherit; }
button, select, input:not([type="checkbox"]) {
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px;
}
button { cursor: pointer; }
button:hover { background: var(--accent-wash); }
button[type="submit"] { background: var(--home); color: #fff; border-color: transparent; }
button.link, button.back { border: 0; background: none; color: var(--home); padding: 4px 0; }
:focus-visible { outline: 2px solid var(--home); outline-offset: 2px; }

.daybar { display: grid; grid-template-columns: auto 1fr auto; gap: 6px; }
.layers { display: grid; grid-template-columns: 1fr; gap: 6px 12px; margin-top: 10px; font-size: 13px; }
.layers label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.sw { width: 18px; height: 10px; border-radius: 3px; }
.sw-home { background: linear-gradient(90deg, #cde2fb, var(--home)); }
.sw-site { background: linear-gradient(90deg, #fbd9c9, var(--site)); }
.dot { width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--surface); box-shadow: 0 0 0 1px var(--border); }
.dot-home { background: var(--home); }
.dot-site { background: var(--site); }
.dot-open { background: var(--open); }
.line { width: 18px; height: 0; border-top: 2px solid var(--muted); }

.jobs, .cands, .busy { list-style: none; margin: 8px 0 0; padding: 0; }
.jobs li {
  display: grid; grid-template-columns: 12px 1fr auto; gap: 8px; align-items: start;
  padding: 8px; border-radius: 6px; cursor: pointer;
}
.jobs li:hover, .jobs li:focus-visible { background: var(--accent-wash); }
.jobs li .dot { margin-top: 4px; }
.jobs .who { color: var(--ink-2); font-size: 12px; }
.jobs .empty { cursor: default; color: var(--ink-2); display: block; }
.tag { font-size: 11px; padding: 1px 6px; border-radius: 10px; border: 1px solid var(--border); color: var(--ink-2); white-space: nowrap; }
.tag.open { border-color: var(--open); }

.pmfilter { display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: 12px; color: var(--ink-2); }
.pmfilter select { flex: 1; }
.pmfilter[hidden] { display: none; }
.plan { margin-top: 12px; }
.plan summary { cursor: pointer; color: var(--home); }
.plan form { display: grid; gap: 8px; margin-top: 8px; }
.plan label { display: grid; gap: 2px; font-size: 12px; color: var(--ink-2); }
.check { display: flex !important; align-items: center; gap: 6px; font-size: 13px; color: var(--ink) !important; margin: 8px 0; }
.grid2 { grid-template-columns: 1fr 1fr; }
.grid2 input { width: 100%; }

#jobHead h2 { margin-top: 4px; }
.cands li {
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin-bottom: 6px; cursor: pointer;
  display: grid; grid-template-columns: 22px 1fr auto; gap: 4px 8px;
}
.cands li:hover, .cands li.sel { background: var(--accent-wash); border-color: var(--home); }
.cands .rank { font-weight: 600; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.cands .name { font-weight: 600; }
.cands .how { grid-column: 2 / 4; color: var(--ink-2); font-size: 12px; }
.cands .times { grid-column: 2 / 4; font-size: 12px; font-variant-numeric: tabular-nums; }
.cands .drive { text-align: right; font-variant-numeric: tabular-nums; }
.flags { grid-column: 2 / 4; display: flex; flex-wrap: wrap; gap: 4px; }
.st { font-size: 11px; padding: 1px 6px; border-radius: 10px; border: 1px solid currentColor; }
.st.good { color: var(--good-ink); }
.st.serious { color: var(--ink); border-color: var(--serious); }
.st.critical { color: var(--critical); }
.busy li { font-size: 12px; color: var(--ink-2); padding: 2px 0; }

.warn { margin-top: 8px; font-size: 12px; }
.warn summary { cursor: pointer; color: var(--ink); }
.warn summary::before { content: "\26A0\FE0E  "; color: var(--critical); }
.warn ul { padding-left: 18px; color: var(--ink-2); }

/* map marks */
.pin { display: grid; place-items: center; font: 600 11px/1 system-ui, sans-serif; color: #fff; border-radius: 50%;
  border: 2px solid var(--surface); box-shadow: 0 0 0 1px rgba(0,0,0,.25); }
.pin-office { background: var(--office); color: var(--surface); border-radius: 4px; }
.pin-cand { background: var(--home); }
.pin-home { width: 26px; height: 26px; background: var(--home); font-size: 10px; letter-spacing: 0.02em; }
.leaflet-tooltip { font: 12px/1.3 system-ui, sans-serif; }

@media (max-width: 760px) {
  .app { grid-template-columns: 1fr; grid-template-rows: 55vh auto; height: auto; }
  #map { grid-row: 1; height: 55vh; }
  .side { grid-row: 2; border-right: 0; overflow: visible; }
}
'''

PLANNER_JS = r'''/* Candidate ranking for a job: who should go, and how do they get the materials?
 *
 * For each free engineer we cost three ways of doing the day and keep the best:
 *   direct          home -> site -> home (no materials needed, or the site is the office)
 *   office_morning  home -> office (06:00, min stop) -> site (07:50) -> home
 *   collect_before  on the previous working day: last site -> office -> home,
 *                   then straight home -> site on the job day
 * Lateness against the targets (beyond the tolerance) is penalised, so an
 * on-time option always beats a late one of similar mileage.
 *
 * Plain functions, no DOM - shared by the page and the node tests.
 */
(function (root) {
  "use strict";

  const LATE_SITE_WEIGHT = 3; // a minute late on site costs as much as 3 minutes of driving
  const LATE_HOME_WEIGHT = 1;

  function toMin(hhmm) {
    const [h, m] = String(hhmm).split(":").map(Number);
    return h * 60 + (m || 0);
  }

  function fmt(min) {
    const m = Math.round(min);
    const h = Math.floor(m / 60);
    return `${String(((h % 24) + 24) % 24).padStart(2, "0")}:${String(((m % 60) + 60) % 60).padStart(2, "0")}`;
  }

  function haversineKm(a, b) {
    const R = 6371;
    const rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad;
    const dLon = (b.lon - a.lon) * rad;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  }

  function travelMin(a, b, s) {
    return (haversineKm(a, b) * s.road_factor / s.avg_speed_kmh) * 60;
  }

  function normPc(pc) {
    return String(pc || "").replace(/\s+/g, "").toUpperCase();
  }

  function isOffice(pc, prefix) {
    return !!pc && !!prefix && normPc(pc).startsWith(normPc(prefix));
  }

  function parseDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }

  function isoDate(d) {
    return d.toISOString().slice(0, 10);
  }

  function previousWorkingDay(iso) {
    const d = parseDate(iso);
    do d.setUTCDate(d.getUTCDate() - 1); while (d.getUTCDay() === 0 || d.getUTCDay() === 6);
    return isoDate(d);
  }

  /** Targets for a date: Friday has its own finish and home times + contract hours. */
  function dayTargets(iso, s) {
    const fri = parseDate(iso).getUTCDay() === 5;
    return {
      friday: fri,
      office: toMin(s.target_office),
      site: toMin(s.target_site),
      workEnd: toMin(fri ? s.target_work_end_friday : s.target_work_end_weekday),
      home: toMin(fri ? s.target_home_friday : s.target_home_weekday),
      contractMin: (fri ? s.contract_hours_fri : s.contract_hours_mon_thu) * 60,
      tol: Number(s.late_tolerance),
      minStop: Number(s.min_stop_min),
    };
  }

  function over(actual, target, tol) {
    const late = actual - target;
    return late > tol ? late : 0;
  }

  /**
   * Cost every way this engineer could do the job and return them, best first.
   * ctx: { office:{lat,lon,prefix}, settings, prevSite: booking|null }
   */
  function planOptions(job, engineer, ctx) {
    const s = ctx.settings;
    const t = dayTargets(job.date, s);
    if (job.startTime) t.site = toMin(job.startTime); // the board's StartTime beats the default 07:50
    const home = engineer, site = job, office = ctx.office;
    const hs = travelMin(home, site, s);
    const sh = hs;
    const needs = !!job.needsMaterials && !isOffice(job.postcode, office.prefix);
    const opts = [];

    // Job-day afternoon is the same in every option.
    const arriveHome = t.workEnd + sh;
    const homeLate = over(arriveHome, t.home, t.tol);

    const direct = (kind, extra) => {
      const leaveHome = t.site - hs;
      const o = {
        kind,
        legs: [["home", "site", hs], ["site", "home", sh]],
        leaveHome, arriveOffice: null, arriveSite: t.site, arriveHome,
        siteLate: 0, homeLate,
        driveMin: hs + sh,
        dayMin: arriveHome - leaveHome,
        prevDay: null,
      };
      return Object.assign(o, extra || {});
    };

    if (!needs) {
      opts.push(direct("direct"));
    } else {
      // 1. Morning pickup at the office.
      const ho = travelMin(home, office, s);
      const os = travelMin(office, site, s);
      const leaveOffice = Math.max(t.office + t.minStop, t.site - os);
      const arriveSite = leaveOffice + os;
      opts.push({
        kind: "office_morning",
        legs: [["home", "office", ho], ["office", "site", os], ["site", "home", sh]],
        leaveHome: t.office - ho, arriveOffice: t.office, arriveSite, arriveHome,
        siteLate: over(arriveSite, t.site, t.tol), homeLate,
        driveMin: ho + os + sh,
        dayMin: arriveHome - (t.office - ho),
        prevDay: null,
      });

      // 2. Collect on the way home the working day before, then go straight to site.
      const prev = ctx.prevSite;
      if (prev && prev.lat != null) {
        const pt = dayTargets(prev.date, s);
        const prevAtOffice = isOffice(prev.postcode, office.prefix);
        const ph = travelMin(prev, home, s);
        const po = prevAtOffice ? 0 : travelMin(prev, office, s);
        const oh = travelMin(office, home, s);
        const prevHome = prevAtOffice ? pt.workEnd + oh : pt.workEnd + po + t.minStop + oh;
        // Only the lateness the detour adds counts against this option.
        const addedLate = prevAtOffice ? 0 : Math.max(0, over(prevHome, pt.home, pt.tol) - over(pt.workEnd + ph, pt.home, pt.tol));
        opts.push(direct("collect_before", {
          driveMin: hs + sh + (prevAtOffice ? 0 : po + oh - ph),
          prevDay: {
            date: prev.date, postcode: prev.postcode, atOffice: prevAtOffice,
            arriveHome: prevHome, homeTarget: pt.home, addedLate,
            legs: prevAtOffice ? [] : [["prev", "office", po], ["office", "home", oh]],
          },
        }));
      }
    }

    for (const o of opts) {
      const prevLate = o.prevDay ? o.prevDay.addedLate : 0;
      o.contractMin = t.contractMin;
      o.overtimeMin = Math.max(0, o.dayMin - t.contractMin);
      o.extraMin = Math.max(0, o.driveMin - (hs + sh)); // driving added by the materials pickup
      o.onTime = o.siteLate === 0 && o.homeLate === 0 && prevLate === 0;
      o.score = o.driveMin + LATE_SITE_WEIGHT * o.siteLate + LATE_HOME_WEIGHT * (o.homeLate + prevLate);
    }
    opts.sort((a, b) => a.score - b.score);
    return opts;
  }

  /** Index bookings by date -> engineerId -> [bookings]. */
  function indexBookings(bookings) {
    const idx = new Map();
    for (const b of bookings) {
      if (!b.engineerId) continue;
      if (!idx.has(b.date)) idx.set(b.date, new Map());
      const day = idx.get(b.date);
      if (!day.has(b.engineerId)) day.set(b.engineerId, []);
      day.get(b.engineerId).push(b);
    }
    return idx;
  }

  /**
   * Rank engineers for a job. Engineers already booked elsewhere that day are
   * returned separately as `busy` (the job's own assignee is always ranked).
   */
  function rankCandidates(job, engineers, bookings, ctx) {
    const idx = ctx.index || indexBookings(bookings);
    const today = idx.get(job.date) || new Map();
    const prevDate = previousWorkingDay(job.date);
    const prevDay = idx.get(prevDate) || new Map();
    const ranked = [], busy = [], unplaceable = [];

    for (const e of engineers) {
      const own = (today.get(e.id) || []);
      const elsewhere = own.filter((b) => b.id !== job.id && normPc(b.postcode) !== normPc(job.postcode));
      const assigned = job.engineerId === e.id;
      if (elsewhere.length && !assigned) {
        busy.push({ engineer: e, bookings: elsewhere });
        continue;
      }
      if (e.lat == null) {
        unplaceable.push({ engineer: e });
        continue;
      }
      const prevBookings = (prevDay.get(e.id) || []).filter((b) => b.lat != null);
      const prevSite = prevBookings.length ? prevBookings[prevBookings.length - 1] : null;
      const options = planOptions(job, e, Object.assign({}, ctx, { prevSite }));
      ranked.push({
        engineer: e,
        assigned,
        sameSiteYesterday: !!prevSite && normPc(prevSite.postcode) === normPc(job.postcode),
        prevSite,
        best: options[0],
        options,
      });
    }
    ranked.sort((a, b) => a.best.score - b.best.score || a.engineer.name.localeCompare(b.engineer.name));
    return { ranked, busy, unplaceable, prevDate };
  }

  const api = {
    toMin, fmt, haversineKm, travelMin, isOffice, previousWorkingDay, dayTargets,
    planOptions, indexBookings, rankCandidates,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Planner = api;
})(this);
'''

APP_JS = r'''(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const P = window.Planner;
  const $ = (id) => document.getElementById(id);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* storage unavailable */ } },
  };

  const office = DATA.office;
  const engineers = DATA.engineers;
  const engById = new Map(engineers.map((e) => [e.id, e]));
  const settings = Object.assign({}, DATA.settings, store.get("heatmap.settings", {}));
  // Only today and later is shown - whichever is later of build day and the day the page is opened.
  const now = new Date();
  const localToday = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const today = localToday > DATA.today ? localToday : DATA.today;
  let adhoc = store.get("heatmap.adhoc", []).filter((j) => j.date >= today);
  const materials = new Map(); // job key -> needs materials (PM override for this session)

  const allBookings = () => DATA.bookings.concat(adhoc);
  let index = P.indexBookings(allBookings());
  const ctx = () => ({ office, settings, index });

  // ---------- header ----------
  $("meta").textContent = `${engineers.filter((e) => e.lat != null).length} engineers mapped · board from ${fmtDate(today)} · generated ${DATA.generatedAt.replace("T", " ")}`;
  if (DATA.warnings.length) {
    const box = $("warnings");
    box.hidden = false;
    box.querySelector("summary").textContent = `${DATA.warnings.length} data warning${DATA.warnings.length > 1 ? "s" : ""}`;
    box.querySelector("ul").innerHTML = DATA.warnings.map((w) => `<li>${esc(w)}</li>`).join("");
  }

  function fmtDate(iso, long) {
    const d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString("en-GB", long ? { weekday: "long", day: "numeric", month: "long" } : { weekday: "short", day: "numeric", month: "short" });
  }
  function fmtDur(min) {
    const m = Math.round(min);
    return m >= 60 ? `+${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `+${m} min`;
  }

  // ---------- dates ----------
  // Today, every weekday of the next fortnight, and any later day with something booked.
  function workingDates() {
    const out = new Set(allBookings().map((b) => b.date).filter((d) => d >= today));
    const d = new Date(today + "T12:00:00Z");
    for (let i = 0; i < 14; i++, d.setUTCDate(d.getUTCDate() + 1)) {
      if (d.getUTCDay() % 6 !== 0 || i === 0) out.add(d.toISOString().slice(0, 10));
    }
    return [...out].sort();
  }
  let dates = workingDates();
  let day = dates[0];

  function fillPMs() {
    const pms = [...new Set(DATA.bookings.map((b) => b.pm).filter(Boolean))].sort();
    if (pmFilter && !pms.includes(pmFilter)) pmFilter = "";
    $("pmSelect").innerHTML = '<option value="">Everyone</option>' + pms.map((p) => `<option>${esc(p)}</option>`).join("");
    $("pmSelect").value = pmFilter;
    $("pmSelect").parentElement.hidden = !pms.length;
  }

  function fillDates() {
    dates = workingDates();
    $("daySelect").innerHTML = dates.map((d) => {
      const n = allBookings().filter((b) => b.date === d && !b.engineerId && !b.off).length;
      return `<option value="${d}">${esc(fmtDate(d))}${n ? ` · ${n} unassigned` : ""}</option>`;
    }).join("");
    $("daySelect").value = day;
  }

  // ---------- map ----------
  // The background is a still picture saved in the "tiles" folder next to this file.
  const T = DATA.tiles;
  const map = L.map("map", { zoomControl: true, preferCanvas: true, minZoom: T ? T.minZoom : 3, maxZoom: T ? T.maxNativeZoom + 3 : 18 });
  if (T) {
    L.tileLayer(T.url, {
      minZoom: T.minZoom, maxNativeZoom: T.maxNativeZoom, maxZoom: T.maxNativeZoom + 3,
      bounds: T.bounds, attribution: T.attribution,
      errorTileUrl: "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==",
    }).addTo(map);
  }

  const hasHeat = typeof L.heatLayer === "function";
  const homePts = engineers.filter((e) => e.lat != null).map((e) => [e.lat, e.lon]);
  const sitesHeat = hasHeat ? L.heatLayer([], {
    radius: 28, blur: 22, minOpacity: 0.2, maxZoom: 10, max: 2.5,
    gradient: { 0.2: "#fbd9c9", 0.45: "#f39a73", 0.7: "#eb6834", 1: "#a8401a" },
  }) : L.layerGroup();
  const homesLayer = L.layerGroup();
  const linksLayer = L.layerGroup();
  const sitesLayer = L.layerGroup().addTo(map);
  const selLayer = L.layerGroup().addTo(map);

  const initials = (name) => String(name).split(/\s+/).filter(Boolean).map((w) => w[0]).filter((c, i, a) => i === 0 || i === a.length - 1).join("").toUpperCase();
  for (const e of engineers) {
    if (e.lat == null) continue;
    L.marker([e.lat, e.lon], {
      icon: L.divIcon({ className: "", html: `<div class="pin pin-home">${esc(initials(e.name))}</div>`, iconSize: [26, 26] }),
      zIndexOffset: 500,
    }).bindTooltip(`${esc(e.name)} – home<br>${esc(e.postcode)}${e.approx ? " (approx.)" : ""}`).addTo(homesLayer);
  }
  L.marker([office.lat, office.lon], {
    icon: L.divIcon({ className: "", html: '<div class="pin pin-office" style="width:22px;height:22px">HQ</div>', iconSize: [22, 22] }),
    zIndexOffset: 1000,
  }).bindTooltip(`Head office (${esc(office.postcode)})`).addTo(map);

  let pmFilter = store.get("heatmap.pm", "");
  let currentJob = null;
  let selectedCand = 0;
  let showAll = false;

  const toggles = [["lySitesHeat", sitesHeat], ["lyHomes", homesLayer], ["lyLinks", linksLayer]];
  for (const [id, layer] of toggles) {
    const box = $(id);
    // The day's home->site lines would bury the selected job's routes, so they hide while one is open.
    const sync = () => (box.checked && !(layer === linksLayer && currentJob) ? layer.addTo(map) : map.removeLayer(layer));
    box.addEventListener("change", sync);
    box.addEventListener("sync", sync);
    sync();
  }
  const syncLayers = () => toggles.forEach(([id]) => $(id).dispatchEvent(new Event("sync")));

  const fitPts = homePts.concat([[office.lat, office.lon]]);
  if (fitPts.length > 1) map.fitBounds(fitPts, { padding: [30, 30] });
  else map.setView([office.lat, office.lon], 10);

  // ---------- jobs for the day ----------
  function jobsForDay(date) {
    const groups = new Map();
    for (const b of allBookings()) {
      if (b.date !== date || b.off) continue;
      if (pmFilter && !b.adhoc && b.pm !== pmFilter) continue;
      const key = b.engineerId ? `${b.postcode}|${b.ref}` : b.id; // crews share a site; open jobs stay separate
      if (!groups.has(key)) groups.set(key, Object.assign({}, b, { key, crew: [], ids: [] }));
      const g = groups.get(key);
      g.ids.push(b.id);
      if (b.engineerId) g.crew.push(b.engineerId);
      if (!g.engineerId && b.engineerId) g.engineerId = b.engineerId;
    }
    for (const g of groups.values()) {
      if (materials.has(g.key)) g.needsMaterials = materials.get(g.key);
      else if (g.needsMaterials == null) g.needsMaterials = !!settings.default_needs_materials;
    }
    return [...groups.values()].sort((a, b) => (!!a.engineerId - !!b.engineerId) || String(a.postcode).localeCompare(String(b.postcode)));
  }

  function siteMarker(job, selected) {
    const open = !job.engineerId;
    return L.circleMarker([job.lat, job.lon], {
      radius: selected ? 10 : open ? 7 : 6,
      weight: 2,
      color: cssVar("--surface"),
      fillColor: cssVar(open ? "--open" : "--site"),
      fillOpacity: 1,
    });
  }

  function crewNames(job) {
    return job.crew.map((id) => (engById.get(id) || { name: id }).name).join(", ");
  }

  function renderDay() {
    $("daySelect").value = day;
    const jobs = jobsForDay(day);
    const placed = jobs.filter((j) => j.lat != null);

    if (hasHeat) {
      // Heat covers every upcoming job, so it shows where the work is overall; pins show the chosen day.
      const upcoming = allBookings().filter((b) => b.date >= today && !b.off && b.lat != null);
      sitesHeat.setOptions({ max: Math.max(2.5, Math.sqrt(upcoming.length)) }); // scale so busy areas stand out, not everything
      sitesHeat.setLatLngs(upcoming.map((b) => [b.lat, b.lon, 1]));
    }
    sitesLayer.clearLayers();
    linksLayer.clearLayers();
    for (const j of placed) {
      siteMarker(j, false)
        .bindTooltip(`<b>${esc(j.ref || j.postcode)}</b><br>${esc(j.postcode)}${j.address ? "<br>" + esc(j.address) : ""}<br>${j.engineerId ? esc(crewNames(j)) : "Unassigned"}`)
        .on("click", () => selectJob(j))
        .addTo(sitesLayer);
      for (const id of j.crew) {
        const e = engById.get(id);
        if (e && e.lat != null) {
          L.polyline([[e.lat, e.lon], [j.lat, j.lon]], { color: cssVar("--muted") || "#898781", weight: 1.5, opacity: 0.7 }).addTo(linksLayer);
        }
      }
    }

    $("listTitle").textContent = fmtDate(day, true);
    const open = jobs.filter((j) => !j.engineerId).length;
    $("listCount").textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}${open ? ` · ${open} unassigned` : ""}`;
    const list = $("jobList");
    list.innerHTML = jobs.length ? "" : '<li class="empty">Nothing on the board for this day.</li>';
    for (const j of jobs) {
      const li = document.createElement("li");
      li.tabIndex = 0;
      li.innerHTML = `<span class="dot ${j.engineerId ? "dot-site" : "dot-open"}"></span>
        <span><b>${esc(j.ref || j.postcode || "No postcode")}</b>${j.ref ? ` <span class="muted">${esc(j.postcode || "")}</span>` : ""}
        <div class="who">${j.engineerId ? esc(crewNames(j)) : "Unassigned"}${j.jobType ? " · " + esc(j.jobType) : ""}${j.lat == null ? " · can't be mapped" : ""}</div></span>
        <span>${j.adhoc ? '<span class="tag">planned here</span>' : !j.engineerId ? '<span class="tag open">pick engineer</span>' : j.provisional ? '<span class="tag">provisional</span>' : ""}</span>`;
      if (j.lat != null) {
        li.addEventListener("click", () => selectJob(j));
        li.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectJob(j); } });
      }
      list.appendChild(li);
    }
    if (currentJob) {
      const again = jobs.find((j) => j.key === currentJob.key);
      if (again) selectJob(again, true); else closeJob();
    }
  }

  // ---------- candidates ----------
  const HOW = {
    direct: () => "Straight from home to site",
    office_morning: () => `Office pickup at ${settings.target_office}, then to site`,
    collect_before: (o) => o.prevDay.atOffice
      ? `Already at the office ${fmtDate(o.prevDay.date)} – takes materials home, straight to site`
      : `Collects materials after ${fmtDate(o.prevDay.date)} job (${o.prevDay.postcode}), straight to site`,
  };

  function flagsHtml(c, o) {
    const f = [];
    const late = o.siteLate, homeLate = o.homeLate, prevLate = o.prevDay ? o.prevDay.addedLate : 0;
    if (o.onTime) f.push('<span class="st good">&#10003; on time</span>');
    if (late) f.push(`<span class="st critical">&#9888;&#xFE0E; ${Math.round(late)} min late on site</span>`);
    if (homeLate) f.push(`<span class="st serious">&#9888;&#xFE0E; home ${Math.round(homeLate)} min after target</span>`);
    if (prevLate) f.push(`<span class="st serious">&#9888;&#xFE0E; pickup makes ${fmtDate(o.prevDay.date)} ${Math.round(prevLate)} min late home</span>`);
    if (o.overtimeMin >= 1) f.push(`<span class="st serious">${fmtDur(o.overtimeMin)} over ${o.contractMin / 60}h contract day</span>`);
    if (c.assigned) f.push('<span class="tag">booked on this job</span>');
    if (c.sameSiteYesterday) f.push('<span class="tag">on this site the day before</span>');
    if (c.engineer.approx) f.push('<span class="tag">home postcode approx.</span>');
    return f.join("");
  }

  function timesHtml(o) {
    const parts = [`Leave home ${P.fmt(o.leaveHome)}`];
    if (o.arriveOffice != null) parts.push(`office ${P.fmt(o.arriveOffice)}`);
    parts.push(`on site ${P.fmt(o.arriveSite)}`, `home ${P.fmt(o.arriveHome)}`);
    return parts.join(" · ");
  }

  function selectJob(job, keepSelection) {
    if (!keepSelection || !currentJob || currentJob.key !== job.key) { selectedCand = 0; showAll = false; }
    currentJob = job;
    $("listView").hidden = true;
    $("jobView").hidden = false;
    const atOffice = P.isOffice(job.postcode, office.prefix);
    $("jobMaterials").checked = !!job.needsMaterials && !atOffice;
    $("jobMaterials").disabled = atOffice;
    $("jobMaterials").parentElement.lastChild.textContent = atOffice
      ? ` Site is at the office (${office.prefix}) – no pickup needed` : " Needs materials from the office";
    $("jobHead").innerHTML = `<h2>${esc(job.ref || job.postcode)}</h2>
      <p class="muted">${esc(fmtDate(job.date, true))} · ${esc(job.postcode)}${job.approx ? " (approx.)" : ""}${job.address ? " · " + esc(job.address) : ""}</p>
      <p class="muted">${[job.jobType, job.pm && "PM " + job.pm, job.jobValue != null && "£" + Number(job.jobValue).toLocaleString("en-GB"),
        job.startTime && "start " + job.startTime, job.provisional && "provisional", job.completed && "completed: " + job.completed]
        .filter(Boolean).map(esc).join(" · ")}</p>
      <p class="muted">${job.engineerId ? "Booked: " + esc(crewNames(job)) : "Unassigned"}
      ${job.adhoc ? ' · <button type="button" class="link" id="removeAdhoc">remove</button>' : ""}</p>`;
    if (job.adhoc) $("removeAdhoc").addEventListener("click", () => removeAdhoc(job));

    const res = P.rankCandidates(job, engineers, null, ctx());
    for (const c of res.ranked) c.assigned = job.crew.includes(c.engineer.id);
    renderCandidates(job, res);
    syncLayers();
  }

  function renderCandidates(job, res) {
    const list = $("candList");
    const shown = showAll ? res.ranked : res.ranked.slice(0, 8);
    list.innerHTML = res.ranked.length ? "" : '<li class="empty">No free engineers with a known home location.</li>';
    shown.forEach((c, i) => {
      const o = c.best;
      const li = document.createElement("li");
      li.tabIndex = 0;
      if (i === selectedCand) li.classList.add("sel");
      li.innerHTML = `<span class="rank">${i + 1}</span><span class="name">${esc(c.engineer.name)}</span>
        <span class="drive"><b>${Math.round(o.driveMin)}</b> <span class="muted">min driving</span></span>
        <span class="how">${esc(HOW[o.kind](o))}${o.extraMin >= 1 ? ` <span class="muted">(+${Math.round(o.extraMin)} min for pickup)</span>` : ""}</span>
        <span class="times">${timesHtml(o)}</span>
        <span class="flags">${flagsHtml(c, o)}</span>`;
      const pick = () => { selectedCand = i; renderCandidates(job, res); };
      li.addEventListener("click", pick);
      li.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
      list.appendChild(li);
    });
    const more = $("moreBtn");
    more.hidden = res.ranked.length <= 8;
    more.textContent = showAll ? "Show top 8" : `Show all ${res.ranked.length}`;

    const busy = $("busyBox");
    const others = res.busy.length + res.unplaceable.length;
    busy.hidden = !others;
    busy.querySelector("summary").textContent = `${res.busy.length} booked elsewhere or off${res.unplaceable.length ? `, ${res.unplaceable.length} without a home postcode` : ""}`;
    busy.querySelector("ul").innerHTML =
      res.busy.map((b) => `<li>${esc(b.engineer.name)} – ${esc(b.bookings.map((x) => x.off || x.ref || x.postcode).join(", "))}</li>`).join("") +
      res.unplaceable.map((u) => `<li>${esc(u.engineer.name)} – no home postcode (add it to fitter_homes.csv)</li>`).join("");

    drawSelection(job, shown, shown[selectedCand]);
  }

  function drawSelection(job, shown, chosen) {
    selLayer.clearLayers();
    const site = [job.lat, job.lon];
    siteMarker(job, true).bindTooltip(esc(job.ref || job.postcode)).addTo(selLayer);
    const pts = [site];
    shown.forEach((c, i) => {
      const e = c.engineer;
      if (c !== chosen) L.polyline([[e.lat, e.lon], site], { color: cssVar("--home"), weight: 1, opacity: 0.35, dashArray: "2 6" }).addTo(selLayer);
      L.marker([e.lat, e.lon], {
        icon: L.divIcon({ className: "", html: `<div class="pin pin-cand" style="width:20px;height:20px">${i + 1}</div>`, iconSize: [20, 20] }),
        zIndexOffset: 900 - i,
      }).bindTooltip(`${i + 1}. ${esc(e.name)} – ${Math.round(c.best.driveMin)} min driving`).on("click", () => { selectedCand = i; selectJob(job, true); }).addTo(selLayer);
      pts.push([e.lat, e.lon]);
    });
    if (chosen) {
      const o = chosen.best, e = chosen.engineer, home = [e.lat, e.lon], hq = [office.lat, office.lon];
      const route = o.kind === "office_morning" ? [home, hq, site] : [home, site];
      L.polyline(route, { color: cssVar("--home"), weight: 3, opacity: 0.95 }).addTo(selLayer);
      if (o.kind === "collect_before" && !o.prevDay.atOffice && chosen.prevSite) {
        const prev = [chosen.prevSite.lat, chosen.prevSite.lon];
        L.polyline([prev, hq, home], { color: cssVar("--home"), weight: 2, opacity: 0.8, dashArray: "6 6" })
          .bindTooltip(`Day before: ${esc(chosen.prevSite.postcode)} → office → home`).addTo(selLayer);
        pts.push(prev);
      }
      if (o.kind !== "direct") pts.push(hq);
    }
    map.fitBounds(pts, { padding: [40, 40], maxZoom: 13 });
  }

  function closeJob() {
    currentJob = null;
    selLayer.clearLayers();
    $("jobView").hidden = true;
    $("listView").hidden = false;
    syncLayers();
  }

  $("backBtn").addEventListener("click", closeJob);
  $("moreBtn").addEventListener("click", () => { showAll = !showAll; selectJob(currentJob, true); });
  $("jobMaterials").addEventListener("change", (ev) => {
    materials.set(currentJob.key, ev.target.checked);
    currentJob.needsMaterials = ev.target.checked;
    selectJob(currentJob, true);
  });

  function setDay(d) {
    if (!d) return;
    day = d;
    closeJob();
    renderDay();
  }
  $("pmSelect").addEventListener("change", (ev) => { pmFilter = ev.target.value; store.set("heatmap.pm", pmFilter); closeJob(); renderDay(); });
  $("daySelect").addEventListener("change", (ev) => setDay(ev.target.value));
  $("prevDay").addEventListener("click", () => setDay(dates[dates.indexOf(day) - 1]));
  $("nextDay").addEventListener("click", () => setDay(dates[dates.indexOf(day) + 1]));

  // ---------- plan an ad-hoc job ----------
  async function geocode(pc) {
    const clean = pc.replace(/\s+/g, "").toUpperCase();
    const r = await fetch(`https://api.postcodes.io/postcodes/${encodeURIComponent(clean)}`);
    if (r.ok) { const j = await r.json(); return { postcode: j.result.postcode, lat: j.result.latitude, lon: j.result.longitude, approx: false }; }
    const out = clean.length > 4 ? clean.slice(0, -3) : clean;
    const r2 = await fetch(`https://api.postcodes.io/outcodes/${encodeURIComponent(out)}`);
    if (r2.ok) { const j = await r2.json(); return { postcode: pc.toUpperCase(), lat: j.result.latitude, lon: j.result.longitude, approx: true }; }
    return null;
  }

  function refreshBookings() {
    store.set("heatmap.adhoc", adhoc);
    index = P.indexBookings(allBookings());
    fillDates();
  }

  function removeAdhoc(job) {
    adhoc = adhoc.filter((a) => a.id !== job.id);
    refreshBookings();
    closeJob();
    renderDay();
  }

  const planForm = $("planForm");
  planForm.date.value = day;
  planForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("planMsg");
    msg.textContent = "Looking up postcode…";
    let loc = null;
    try { loc = await geocode(planForm.postcode.value.trim()); } catch (e) { loc = null; }
    if (!loc) { msg.textContent = "Couldn't find that postcode (postcodes.io)."; return; }
    const job = {
      id: `a${Date.now()}`, adhoc: true, date: planForm.date.value, engineerId: null,
      ref: planForm.ref.value.trim(), address: "", needsMaterials: planForm.needsMaterials.checked, ...loc,
    };
    adhoc.push(job);
    refreshBookings();
    msg.textContent = "";
    planForm.reset();
    day = job.date;
    planForm.date.value = day;
    renderDay();
    const j = jobsForDay(day).find((g) => g.id === job.id);
    if (j) selectJob(j);
  });

  // ---------- settings ----------
  const FIELDS = [
    ["avg_speed_kmh", "Avg speed (km/h)", "number"], ["road_factor", "Road factor", "number"],
    ["target_office", "At office by", "time"], ["target_site", "On site by", "time"],
    ["target_work_end_weekday", "Finish Mon–Thu", "time"], ["target_work_end_friday", "Finish Fri", "time"],
    ["target_home_weekday", "Home Mon–Thu", "time"], ["target_home_friday", "Home Fri", "time"],
    ["min_stop_min", "Office stop (min)", "number"], ["late_tolerance", "Late tolerance (min)", "number"],
    ["contract_hours_mon_thu", "Contract h Mon–Thu", "number"], ["contract_hours_fri", "Contract h Fri", "number"],
  ];
  const sf = $("settingsForm");
  sf.innerHTML = FIELDS.map(([k, label, type]) =>
    `<label>${label}<input name="${k}" type="${type}" step="${type === "number" ? "any" : "60"}" value="${esc(settings[k])}"></label>`).join("") +
    '<button type="button" id="resetSettings" class="link">Reset to defaults</button>';
  sf.addEventListener("input", (ev) => {
    const { name, type, value } = ev.target;
    if (!name || value === "") return;
    settings[name] = type === "number" ? Number(value) : value;
    store.set("heatmap.settings", Object.fromEntries(FIELDS.map(([k]) => [k, settings[k]])));
    if (currentJob) selectJob(currentJob, true);
  });
  $("resetSettings").addEventListener("click", () => {
    Object.assign(settings, DATA.settings);
    store.set("heatmap.settings", {});
    for (const [k] of FIELDS) sf[k].value = settings[k];
    if (currentJob) selectJob(currentJob, true);
  });

  fillPMs();
  fillDates();
  renderDay();
})();
'''

# =============================================================================
# Command line
# =============================================================================
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Engineer heat map + job planner")
    ap.add_argument("--db", default=DEFAULT_ACCDB_PATH, help="path to the .accdb")
    ap.add_argument("--list", action="store_true", help="list tables and columns, then stop")
    ap.add_argument("--demo", action="store_true", help="use made-up data instead of the database")
    ap.add_argument("--start", help="treat this date YYYY-MM-DD as today")
    ap.add_argument("--days-ahead", type=int, default=DAYS_AHEAD, help="limit how far ahead (default: everything)")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = ap.parse_args(argv)

    if args.demo:
        db, geocoder, label = MemoryDB(demo_tables(dt.date.today())), Geocoder(None, demo_fetch), "demo data"
    else:
        print(f"Opening {args.db} ...")
        db, geocoder, label = AccessDB(args.db), Geocoder(GEOCODE_CACHE), args.db

    if args.list:
        for table in db.tables():
            try:
                print(f"{table}: {', '.join(db.columns(table))}")
            except Exception as exc:
                print(f"{table}: (can't read: {exc})")
        return

    today = dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    data = load_dataset(db, today, geocoder, label, args.days_ahead)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pts = [(data["office"]["lat"], data["office"]["lon"])] + [(e["lat"], e["lon"]) for e in data["engineers"]] + [
        (b["lat"], b["lon"]) for b in data["bookings"] if b["date"] >= data["today"]]
    data["tiles"] = prepare_tiles(pts, OUTPUT_DIR / "tiles")
    out = OUTPUT_DIR / ("demo_heatmap.html" if args.demo else "engineer_heatmap.html")
    out.write_text(render_html(data), encoding="utf-8")
    placed = sum(f["lat"] is not None for f in data["engineers"])
    jobs = [b for b in data["bookings"] if not b["off"] and b["date"] >= data["today"]]
    print(f"\n{placed}/{len(data['engineers'])} fitters placed, {len(jobs)} bookings from today "
          f"({sum(not b['engineerId'] for b in jobs)} unassigned), up to {data['lastDate']}")
    for w in data["warnings"]:
        print("  !", w)
    print(f"Map: {out}")
    print("  (keep the 'tiles' folder next to it - that's the map background)")
    if not args.no_open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(f"\n{exc.code}" if isinstance(exc.code, str) else "")
            if os.name == "nt":
                input("Press Enter to close ...")
        raise
    except Exception:
        traceback.print_exc()
        if os.name == "nt":
            input("\nSomething went wrong (details above). Press Enter to close ...")
        sys.exit(1)
    else:
        if os.name == "nt" and len(sys.argv) == 1:
            input("\nDone. Press Enter to close ...")
