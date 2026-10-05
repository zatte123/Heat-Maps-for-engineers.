"""Pull engineers + board bookings out of the database into the map's dataset."""
from __future__ import annotations

import datetime as dt
from typing import Any

from . import postcodes
from .geocode import Geocoder
from .mapping import resolve
from .source import TableSource


def _cols(*values) -> list[str]:
    out: list[str] = []
    for v in values:
        for c in (v if isinstance(v, list) else [v]):
            if c and c not in out:
                out.append(c)
    return out


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _truthy(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("y", "yes", "true", "1", "-1", "x", "on")


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _text(value)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _working_days(first: dt.date, last: dt.date):
    day = first
    while day <= last:
        if day == first or day.weekday() < 5:
            yield day
        day += dt.timedelta(days=1)


def _postcode(row: dict, pc_col, addr_col) -> str | None:
    raw = row.get(pc_col) if pc_col else None
    return postcodes.normalise(raw) or postcodes.extract(raw) or (
        postcodes.extract(row.get(addr_col)) if addr_col else None)


def load_dataset(source: TableSource, cfg: dict, start: dt.date, end: dt.date,
                 geocoder: Geocoder, source_label: str = "") -> dict:
    """Bookings on dates start <= d < end, plus every engineer with their home location."""
    eng_map, board_map, notes = resolve(source, cfg)
    warnings: list[str] = []

    # ---- engineers ----
    name_cols = eng_map["name"] if isinstance(eng_map["name"], list) else [eng_map["name"]]
    eng_rows = source.rows(eng_map["table"], _cols(eng_map["id"], eng_map["name"], eng_map["home_postcode"],
                                                   eng_map["home_address"], eng_map["active"]))
    engineers: dict[str, dict] = {}
    by_key: dict[str, str] = {}
    for row in eng_rows:
        if eng_map["active"] and _truthy(row.get(eng_map["active"])) is False:
            continue
        name = " ".join(_text(row.get(c)) for c in name_cols if c and _text(row.get(c)))
        eid = _text(row.get(eng_map["id"])) if eng_map["id"] else name
        if not eid:
            continue
        engineers[eid] = {"id": eid, "name": name or eid,
                          "postcode": _postcode(row, eng_map["home_postcode"], eng_map["home_address"])}
        by_key.setdefault(eid.lower(), eid)
        if name:
            by_key.setdefault(name.lower(), eid)

    # ---- board ----
    eng_cols = board_map["engineer"] if isinstance(board_map["engineer"], list) else [board_map["engineer"]]
    lookback = dt.timedelta(days=90) if board_map["end_date"] else dt.timedelta(0)
    board_rows = source.rows(
        board_map["table"],
        _cols(board_map["date"], board_map["end_date"], eng_cols, board_map["site_postcode"],
              board_map["site_address"], board_map["job_ref"], board_map["needs_materials"]),
        date_col=board_map["date"],
        date_from=dt.datetime.combine(start - lookback, dt.time()),
        date_to=dt.datetime.combine(end, dt.time()),
    )

    bookings: list[dict] = []
    unmatched: set[str] = set()
    no_site_pc = 0
    for row in board_rows:
        first = _as_date(row.get(board_map["date"]))
        if not first:
            continue
        last = _as_date(row.get(board_map["end_date"])) if board_map["end_date"] else None
        days = [d for d in _working_days(first, max(first, last or first)) if start <= d < end]
        if not days:
            continue

        site_pc = _postcode(row, board_map["site_postcode"], board_map["site_address"])
        if not site_pc:
            no_site_pc += 1
        assigned: list[str | None] = []
        for col in eng_cols:
            value = _text(row.get(col))
            if not value:
                continue
            eid = by_key.get(value.lower())
            if eid is None:
                # Keep them so they show as busy, even though we don't know where they live.
                eid = f"?{value}"
                engineers.setdefault(eid, {"id": eid, "name": value, "postcode": None})
                unmatched.add(value)
            assigned.append(eid)

        base = {
            "postcode": site_pc,
            "ref": _text(row.get(board_map["job_ref"])) if board_map["job_ref"] else "",
            "address": _text(row.get(board_map["site_address"])) if board_map["site_address"] else "",
            "needsMaterials": _truthy(row.get(board_map["needs_materials"])) if board_map["needs_materials"] else None,
        }
        for day in days:
            for eid in assigned or [None]:
                bookings.append({**base, "id": f"b{len(bookings) + 1}", "date": day.isoformat(), "engineerId": eid})

    if unmatched:
        warnings.append(f"{len(unmatched)} board engineer value(s) didn't match the engineers table "
                        f"(shown as busy, no home location): {', '.join(sorted(unmatched)[:10])}")
    if no_site_pc:
        warnings.append(f"{no_site_pc} booking(s) have no recognisable site postcode and can't be mapped")

    # ---- geocode ----
    office_pc = postcodes.normalise(cfg.get("office_postcode")) or postcodes.normalise(cfg["office_prefix"])
    found = geocoder.lookup_many([office_pc] + [e["postcode"] for e in engineers.values()]
                                 + [b["postcode"] for b in bookings])
    geocoder.save()

    def place(item: dict) -> None:
        loc = found.get(item["postcode"]) if item["postcode"] else None
        item["lat"] = loc["lat"] if loc else None
        item["lon"] = loc["lon"] if loc else None
        item["approx"] = bool(loc and loc["approx"])

    for e in engineers.values():
        place(e)
    for b in bookings:
        place(b)

    no_home = sorted(e["name"] for e in engineers.values() if e["lat"] is None and not e["id"].startswith("?"))
    if no_home:
        warnings.append(f"{len(no_home)} engineer(s) have no usable home postcode and can't be ranked: "
                        f"{', '.join(no_home[:10])}{' ...' if len(no_home) > 10 else ''}")
    bad = sorted({b["postcode"] for b in bookings if b["postcode"] and b["lat"] is None}
                 | {e["postcode"] for e in engineers.values() if e["postcode"] and e["lat"] is None})
    if bad:
        warnings.append(f"Postcodes not found by postcodes.io: {', '.join(bad[:10])}")

    office_loc = found.get(office_pc)
    if not office_loc:
        raise SystemExit(f"Couldn't locate the office ({office_pc}). Set office_postcode in heatmap_config.json.")

    return {
        "generatedAt": dt.datetime.now().isoformat(timespec="minutes"),
        "source": source_label,
        "windowStart": start.isoformat(),
        "windowEnd": (end - dt.timedelta(days=1)).isoformat(),
        "office": {"prefix": cfg["office_prefix"], "postcode": office_pc,
                   "lat": office_loc["lat"], "lon": office_loc["lon"]},
        "settings": cfg["planning"],
        "engineers": sorted(engineers.values(), key=lambda e: e["name"].lower()),
        "bookings": bookings,
        "mapping": {"engineers": eng_map, "board": board_map},
        "notes": notes,
        "warnings": warnings,
    }
