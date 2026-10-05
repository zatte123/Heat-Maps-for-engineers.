"""Command line.

    python -m heatmap inspect           show the database schema + which tables/columns will be used
    python -m heatmap build [--open]    read the board, geocode, write the HTML map
    python -m heatmap demo  [--open]    same, with made-up data (no database needed)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import webbrowser
from pathlib import Path

from .build import write_html
from .config import load_config
from .geocode import Geocoder
from .loader import load_dataset
from .mapping import resolve, schema_report


def _window(args, cfg) -> tuple[dt.date, dt.date]:
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    back = args.days_back if args.days_back is not None else cfg["window"]["days_back"]
    ahead = args.days_ahead if args.days_ahead is not None else cfg["window"]["days_ahead"]
    return start - dt.timedelta(days=back), start + dt.timedelta(days=ahead + 1)


def _finish(dataset: dict, out: str, open_it: bool) -> None:
    path = write_html(dataset, out)
    for note in dataset["notes"]:
        print("note:", note)
    for warning in dataset["warnings"]:
        print("warning:", warning)
    placed = sum(e["lat"] is not None for e in dataset["engineers"])
    open_jobs = sum(b["engineerId"] is None for b in dataset["bookings"])
    print(f"{placed}/{len(dataset['engineers'])} engineers placed, {len(dataset['bookings'])} bookings "
          f"({open_jobs} unassigned), {dataset['windowStart']} to {dataset['windowEnd']}")
    print(f"Wrote {path.resolve()}")
    if open_it:
        webbrowser.open(path.resolve().as_uri())


def cmd_inspect(args) -> None:
    from .source import AccessSource

    cfg = load_config(args.config)
    db = args.db or cfg["accdb_path"]
    source = AccessSource(db)
    report = [f"Database: {db}", "", schema_report(source), ""]
    try:
        eng, board, notes = resolve(source, cfg)
        report += ["Mapping that `build` will use:", json.dumps({"engineers": eng, "board": board}, indent=2), ""]
        report += notes
    except SystemExit as exc:
        report += [f"Mapping problem: {exc}"]
    text = "\n".join(report)
    print(text)
    Path("schema_report.txt").write_text(text, encoding="utf-8")
    print("\n(saved to schema_report.txt)")


def cmd_build(args) -> None:
    from .source import AccessSource

    cfg = load_config(args.config)
    db = args.db or cfg["accdb_path"]
    start, end = _window(args, cfg)
    geocoder = Geocoder(cfg["geocode_cache"])
    dataset = load_dataset(AccessSource(db), cfg, start, end, geocoder, source_label=db)
    _finish(dataset, args.out, args.open)


def cmd_demo(args) -> None:
    from .demo import demo_geocoder, demo_tables
    from .source import MemorySource

    cfg = load_config(args.config)
    cfg["office_postcode"] = "N3 1AA"
    start, end = _window(args, cfg)
    today = dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    dataset = load_dataset(MemorySource(demo_tables(today)), cfg, start, end, demo_geocoder(),
                           source_label="demo data")
    _finish(dataset, args.out, args.open)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m heatmap", description="Engineer location heat map + job planner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, out):
        p.add_argument("--config", default="heatmap_config.json", help="JSON overrides (default: heatmap_config.json)")
        p.add_argument("--start", help="centre date YYYY-MM-DD (default: today)")
        p.add_argument("--days-back", type=int, help="days before start to load (default 3)")
        p.add_argument("--days-ahead", type=int, help="days after start to load (default 14)")
        p.add_argument("--out", default=out, help=f"output HTML (default: {out})")
        p.add_argument("--open", action="store_true", help="open the map in the browser when done")

    p = sub.add_parser("inspect", help="list tables/columns and the mapping that will be used")
    p.add_argument("--config", default="heatmap_config.json")
    p.add_argument("--db", help="path to the .accdb (default from config)")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("build", help="build the map from the Access database")
    p.add_argument("--db", help="path to the .accdb (default from config)")
    common(p, "output/engineer_heatmap.html")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("demo", help="build the map from made-up data")
    common(p, "output/demo_heatmap.html")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # show a readable message to non-developers
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
