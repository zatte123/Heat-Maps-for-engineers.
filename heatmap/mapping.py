"""Work out which tables/columns hold engineers and board bookings.

We don't know CMS593's exact schema, so anything not set in the config is
guessed from table/column names. ``python -m heatmap inspect`` prints the
guesses next to the full schema so they can be confirmed or corrected in
``heatmap_config.json``.
"""
from __future__ import annotations

import re

from .source import TableSource

ENGINEER_WORDS = ("engineer", "operative", "fitter", "installer", "technician", "staff", "employee", "worker")
BOARD_WORDS = ("board", "schedule", "booking", "diary", "alloc", "rota", "planner", "job")
IGNORE_DATE_WORDS = ("created", "modified", "updated", "entered", "edited", "birth", "dob", "invoice", "paid")


def _k(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _first(cols: list[str], *tests, prefer: tuple[str, ...] = ()) -> str | None:
    """First column passing any test, ranking columns that contain a ``prefer`` word first."""
    hits = [c for c in cols if any(t(_k(c)) for t in tests)]
    hits.sort(key=lambda c: (not any(p in _k(c) for p in prefer), len(c)))
    return hits[0] if hits else None


def _is_postcode(k): return "postcode" in k or "postcod" in k or k in ("pcode", "postalcode", "pc", "zip")
def _is_address(k): return "address" in k or "addr" in k
def _is_date(k): return ("date" in k or k.endswith("day")) and not any(w in k for w in IGNORE_DATE_WORDS)
def _is_end_date(k): return _is_date(k) and any(w in k for w in ("end", "finish", "until", "to"))
def _is_engineerish(k): return any(w in k for w in ENGINEER_WORDS)


def guess_engineers(cols: list[str]) -> dict:
    name = _first(cols, lambda k: k in ("name", "fullname", "engineername", "staffname", "employeename"),
                  lambda k: k.endswith("name") and "first" not in k and "sur" not in k and "last" not in k
                  and "user" not in k and "company" not in k)
    if not name:
        first = _first(cols, lambda k: k in ("firstname", "forename", "fname"))
        last = _first(cols, lambda k: k in ("surname", "lastname", "lname"))
        if first and last:
            name = [first, last]
    return {
        "id": _first(cols, lambda k: k == "id", lambda k: k.endswith("id") and _is_engineerish(k),
                     lambda k: k in ("staffno", "employeeno", "payrollno", "code")),
        "name": name,
        "home_postcode": _first(cols, _is_postcode, prefer=("home",)),
        "home_address": _first(cols, _is_address, prefer=("home",)),
        "active": _first(cols, lambda k: k in ("active", "isactive", "current", "employed")),
    }


def guess_board(cols: list[str]) -> dict:
    numbered = sorted(c for c in cols if _is_engineerish(_k(c)) and re.search(r"\d$", _k(c)))
    engineer = numbered or _first(cols, _is_engineerish, prefer=("id",))
    date = _first(cols, lambda k: _is_date(k) and not _is_end_date(k),
                  prefer=("job", "work", "start", "sched", "book", "board"))
    return {
        "date": date,
        "end_date": _first(cols, _is_end_date),
        "engineer": engineer,
        "site_postcode": _first(cols, _is_postcode, prefer=("site", "job", "delivery", "install")),
        "site_address": _first(cols, _is_address, prefer=("site", "job", "delivery", "install")),
        "job_ref": _first(cols, lambda k: k in ("jobno", "jobnumber", "jobref", "ref", "reference", "jobid",
                                                "contractno", "orderno", "ordernumber", "job")),
        "needs_materials": _first(cols, lambda k: "material" in k or "pickup" in k or "collect" in k),
    }


def _score_engineers(table: str, guess: dict) -> int:
    score = 3 * any(w in _k(table) for w in ENGINEER_WORDS)
    score += 2 * bool(guess["home_postcode"] or guess["home_address"])
    score += 2 * bool(guess["name"])
    score += bool(guess["id"])
    return score


def _score_board(table: str, guess: dict) -> int:
    score = 3 * any(w in _k(table) for w in BOARD_WORDS)
    score += 2 * bool(guess["date"])
    score += 2 * bool(guess["engineer"])
    score += 2 * bool(guess["site_postcode"] or guess["site_address"])
    return score


def resolve(source: TableSource, cfg: dict) -> tuple[dict, dict, list[str]]:
    """Return (engineers mapping, board mapping, notes). Config values win over guesses."""
    notes: list[str] = []
    tables = source.tables()
    cols_by_table = {t: source.columns(t) for t in tables}

    def pick(section: str, guesser, scorer) -> dict:
        want = cfg[section]
        table = want.get("table")
        if table and table not in cols_by_table:
            raise SystemExit(f"[{section}] table '{table}' not found. Tables: {', '.join(tables)}")
        if not table:
            ranked = sorted(tables, key=lambda t: scorer(t, guesser(cols_by_table[t])), reverse=True)
            if not ranked:
                raise SystemExit("The database has no tables.")
            table = ranked[0]
            notes.append(f"[{section}] guessed table '{table}' - set \"{section}.table\" in the config if wrong")
        guess = guesser(cols_by_table[table])
        mapping = {"table": table}
        for field in guess:
            if want.get(field):
                mapping[field] = want[field]
            else:
                mapping[field] = guess[field]
                if guess[field]:
                    notes.append(f"[{section}] guessed {field} = {guess[field]!r}")
        missing = [c for field, v in mapping.items() if field != "table"
                   for c in (v if isinstance(v, list) else [v]) if c and c not in cols_by_table[table]]
        if missing:
            raise SystemExit(f"[{section}] column(s) {missing} not in table '{table}'. "
                             f"Columns: {', '.join(cols_by_table[table])}")
        return mapping

    eng = pick("engineers", guess_engineers, _score_engineers)
    board = pick("board", guess_board, _score_board)

    if not (eng.get("home_postcode") or eng.get("home_address")):
        raise SystemExit(f"Couldn't find a home postcode/address column in '{eng['table']}'. "
                         "Set engineers.home_postcode in heatmap_config.json.")
    if not eng.get("id") and not eng.get("name"):
        raise SystemExit(f"Couldn't find an id or name column in '{eng['table']}'.")
    for field in ("date", "engineer"):
        if not board.get(field):
            raise SystemExit(f"Couldn't find the board {field} column in '{board['table']}'. "
                             f"Set board.{field} in heatmap_config.json.")
    if not (board.get("site_postcode") or board.get("site_address")):
        raise SystemExit(f"Couldn't find a site postcode/address column in '{board['table']}'. "
                         "Set board.site_postcode in heatmap_config.json.")
    return eng, board, notes


def schema_report(source: TableSource) -> str:
    lines = []
    for table in source.tables():
        lines.append(f"{table}")
        for col in source.columns(table):
            lines.append(f"    {col}")
    return "\n".join(lines)
