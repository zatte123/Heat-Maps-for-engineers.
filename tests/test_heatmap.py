import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engineer_heatmap as E  # noqa: E402

BASE = {"Holiday": False, "Sick": False, "BHoliday": False, "Unpaid": False, "ProjectMgr": "Ali"}
DAY = dt.datetime(2026, 10, 6)


def test_postcodes():
    assert E.normalise_postcode("n31ab") == "N3 1AB"
    assert E.normalise_postcode("N3") == "N3"
    assert E.normalise_postcode("rubbish") is None
    assert E.find_postcode(None, "Flat 6, 22 Example Way,\nLondon,\nN12 8AB,\nUK") == "N12 8AB"


def test_name_matching():
    people = [{"name": "Tom R J Baker"}, {"name": "Sam Green"}, {"name": "Sam Brown"}, {"name": "Dan Price"}]
    idx = E.NameIndex(people)
    assert idx.get("Tom Baker")["name"] == "Tom R J Baker"
    assert idx.get("tom")["name"] == "Tom R J Baker"
    assert idx.get("Sam G")["name"] == "Sam Green"
    assert idx.get("Sam") is None  # two Sams - don't guess
    assert idx.get("Dan  PRICE")["name"] == "Dan Price"
    assert idx.get("Nobody") is None


def test_access_time_and_numbers():
    assert E._as_time(dt.datetime(1899, 12, 30, 8, 30)) == "08:30"
    assert E._as_time(dt.datetime(1899, 12, 30, 0, 0)) is None
    assert E._key(12.0) == E._key(12) == E._key("12") == "12"


def _run(tables, tmp_path, homes_csv=None):
    E.FITTER_HOMES_CSV = tmp_path / "fitter_homes.csv"
    if homes_csv:
        E.FITTER_HOMES_CSV.write_text(homes_csv)
    return E.load_dataset(E.MemoryDB(tables), dt.date(2026, 10, 5), E.Geocoder(None, E.demo_fetch), "test")


def test_board_with_homes_csv(tmp_path):
    board = [
        {**BASE, "IDNo": 1, "FitRef": 5, "FitterName": "Tom Baker", "JobDate": DAY, "JobPostCode": "SE1 2AB",
         "JobNo": 101, "StartTime": dt.datetime(1899, 12, 30, 9, 0)},
        {**BASE, "IDNo": 2, "FitRef": 7, "FitterName": "Dan Price", "JobDate": DAY, "Holiday": True},
        {**BASE, "IDNo": 3, "FitRef": 0, "FitterName": None, "JobDate": DAY, "JobPostCode": "w2 1ab", "JobNo": 102},
        {**BASE, "IDNo": 4, "FitRef": 5, "FitterName": "Tom Baker", "JobDate": dt.datetime(2026, 10, 1),
         "JobPostCode": "SE1 2AB"},
        {**BASE, "IDNo": 5, "FitRef": 5, "FitterName": "Tom Baker", "JobDate": dt.datetime(2026, 10, 2),
         "JobPostCode": "SE1 2AB"},
        {**BASE, "IDNo": 6, "FitRef": 5, "FitterName": "Tom Baker", "JobDate": dt.datetime(2027, 3, 1),
         "JobPostCode": "SE1 2AB"},
    ]
    csv = "FitRef,FitterName,HomePostcode\n,Tom R J Baker,N12 8AB\n,Dan Price,E4 7AB\n,Rob Hill,SE9 4XY\n"
    data = _run({"Board": board}, tmp_path, csv)

    eng = {e["name"]: e for e in data["engineers"]}
    assert eng["Tom Baker"]["postcode"] == "N12 8AB" and eng["Tom Baker"]["lat"] is not None
    assert eng["Rob Hill"]["postcode"] == "SE9 4XY"  # in the CSV but not on the board yet: still a candidate
    by_id = {b["id"].split("-")[0]: b for b in data["bookings"]}
    assert by_id["b1"]["startTime"] == "09:00" and by_id["b1"]["engineerId"] == "5"
    assert by_id["b2"]["off"] == "Holiday" and by_id["b2"]["postcode"] is None
    assert by_id["b3"]["engineerId"] is None and by_id["b3"]["postcode"] == "W2 1AB"
    assert "b4" not in by_id  # the past is left out ...
    assert by_id["b5"]["date"] == "2026-10-02"  # ... except the previous working day, for day-before pickups
    assert by_id["b6"]["date"] == "2027-03-01"  # no limit on how far ahead


def test_finds_fitters_table_by_names(tmp_path):
    board = [{**BASE, "IDNo": 1, "FitRef": 5, "FitterName": "Sam Green", "JobDate": DAY, "JobPostCode": "SE1 2AB"}]
    tables = {
        "Board": board,
        "Customers": [{"ID": 5, "Name": "Big Client", "PostCode": "EC1A 1BB"}],  # same id, wrong table
        "Staff": [{"StaffID": 99, "FullName": "Sam Green", "HomeAddress": "1 Example Close, London, HA8 7AB"}],
    }
    data = _run(tables, tmp_path)
    assert data["engineers"][0]["postcode"] == "HA8 7AB"


def test_template_csv_when_no_homes(tmp_path):
    board = [{**BASE, "IDNo": 1, "FitRef": 5, "FitterName": "Sam Green", "JobDate": DAY, "JobPostCode": "SE1 2AB"}]
    data = _run({"Board": board}, tmp_path)
    assert "Sam Green" in E.FITTER_HOMES_CSV.read_text()
    assert any("fitter_homes.csv" in w for w in data["warnings"])


def test_demo_renders(tmp_path):
    E.FITTER_HOMES_CSV = tmp_path / "none.csv"
    data = E.load_dataset(E.MemoryDB(E.demo_tables(dt.date(2026, 10, 5))), dt.date(2026, 10, 5),
                          E.Geocoder(None, E.demo_fetch), "demo")
    data["engineers"][0]["name"] = "</script><b>x"
    html = E.render_html(data)
    assert "__DATA__" not in html and "/*__APP__*/" not in html
    assert "</script><b>x" not in html
    assert sum(e["lat"] is not None for e in data["engineers"]) == len(data["engineers"])


def test_still_map_tiles_are_cached(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return b"png"

    pts = [(51.60, -0.19), (51.50, -0.10)]
    t = E.prepare_tiles(pts, tmp_path / "tiles", fetch)
    assert t["url"] == "tiles/{z}/{x}/{y}.png" and t["minZoom"] == 8
    assert 0 < len(calls) <= E.TILE_MAX
    assert all(u.startswith("https://tile.openstreetmap.org/") for u in calls)
    first = len(calls)
    E.prepare_tiles(pts, tmp_path / "tiles", fetch)
    assert len(calls) == first  # second run reuses what's on disk


def test_still_map_gives_up_quickly_when_offline(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        raise OSError("no network")

    assert E.prepare_tiles([(51.6, -0.19), (51.0, 0.5)], tmp_path / "tiles", fetch) is None
    assert len(calls) == 5


def test_far_away_job_lowers_detail_instead_of_huge_download(tmp_path):
    calls = []
    t = E.prepare_tiles([(51.6, -0.19), (53.48, -2.24)], tmp_path / "t", lambda u: calls.append(u) or b"x")
    assert len(calls) <= E.TILE_MAX and t["maxNativeZoom"] < 12
