import datetime as dt
import json

import pytest

from heatmap import postcodes
from heatmap.build import render_html
from heatmap.config import load_config
from heatmap.demo import demo_geocoder, demo_tables, fake_fetch
from heatmap.geocode import Geocoder
from heatmap.loader import load_dataset
from heatmap.mapping import guess_board, guess_engineers, resolve
from heatmap.source import MemorySource


def test_normalise_and_extract():
    assert postcodes.normalise("n31ab") == "N3 1AB"
    assert postcodes.normalise(" ec1a 1bb ") == "EC1A 1BB"
    assert postcodes.normalise("N3") == "N3"
    assert postcodes.normalise("not a postcode") is None
    assert postcodes.extract("12 High Rd, Finchley, London N3 2AB") == "N3 2AB"
    assert postcodes.extract("no postcode here") is None


def test_office_prefix():
    assert postcodes.is_office("N3 1AB", "N3")
    assert not postcodes.is_office("N4 1AB", "N3")
    assert not postcodes.is_office(None, "N3")


def test_guess_columns():
    eng = guess_engineers(["StaffID", "FirstName", "Surname", "Address1", "PostCode", "Active"])
    assert eng["name"] == ["FirstName", "Surname"]
    assert eng["home_postcode"] == "PostCode"
    assert eng["active"] == "Active"
    board = guess_board(["ID", "DateCreated", "WorkDate", "Engineer1", "Engineer2", "SitePostcode", "JobNo"])
    assert board["date"] == "WorkDate"
    assert board["engineer"] == ["Engineer1", "Engineer2"]
    assert board["job_ref"] == "JobNo"


def test_config_override_beats_guess(tmp_path):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"board": {"table": "tblBoard", "site_address": None, "job_ref": "BoardID"}}))
    cfg = load_config(cfg_file)
    _, board, _ = resolve(MemorySource(demo_tables(dt.date(2026, 10, 5))), cfg)
    assert board["job_ref"] == "BoardID"


def test_bad_column_in_config_is_reported():
    cfg = load_config("does-not-exist.json")
    cfg["board"]["date"] = "Nope"
    with pytest.raises(SystemExit, match="Nope"):
        resolve(MemorySource(demo_tables(dt.date(2026, 10, 5))), cfg)


def _tables():
    d = dt.datetime
    return {
        "Engineers": [
            {"ID": 1, "Name": "Ann", "HomeAddress": "1 Road, London N12 8AB", "Postcode": None, "Active": True},
            {"ID": 2, "Name": "Bob", "HomeAddress": "", "Postcode": "e17 4ab", "Active": True},
            {"ID": 3, "Name": "Old", "HomeAddress": "", "Postcode": "E4 1AB", "Active": False},
            {"ID": 4, "Name": "Nohome", "HomeAddress": "", "Postcode": "", "Active": True},
        ],
        "Board": [
            {"JobDate": d(2026, 10, 2), "EndDate": d(2026, 10, 6), "Engineer": "Ann", "SitePostcode": "SE1 2AB", "JobNo": "J1"},
            {"JobDate": d(2026, 10, 5), "EndDate": None, "Engineer": "2", "SitePostcode": "W2 1AB", "JobNo": "J2"},
            {"JobDate": d(2026, 10, 6), "EndDate": None, "Engineer": None, "SitePostcode": "EC1A 1AB", "JobNo": "J3"},
            {"JobDate": d(2026, 10, 6), "EndDate": None, "Engineer": "Zed", "SitePostcode": "XX", "JobNo": "J4"},
            {"JobDate": d(2026, 11, 30), "EndDate": None, "Engineer": "Ann", "SitePostcode": "SE1 2AB", "JobNo": "late"},
        ],
    }


def test_load_dataset_end_to_end():
    cfg = load_config("does-not-exist.json")
    data = load_dataset(MemorySource(_tables()), cfg, dt.date(2026, 10, 5), dt.date(2026, 10, 8), demo_geocoder())

    names = {e["name"]: e for e in data["engineers"]}
    assert "Old" not in names  # inactive
    assert names["Ann"]["postcode"] == "N12 8AB"  # pulled out of the address
    assert names["Bob"]["postcode"] == "E17 4AB"
    assert names["Ann"]["lat"] is not None
    assert names["Zed"]["id"] == "?Zed"  # unmatched board value stays visible as busy

    by_ref = {}
    for b in data["bookings"]:
        by_ref.setdefault(b["ref"], []).append(b)
    # Multi-day J1 (Fri-Tue) clipped to the window and skipping the weekend: Mon, Tue.
    assert [b["date"] for b in by_ref["J1"]] == ["2026-10-05", "2026-10-06"]
    assert by_ref["J2"][0]["engineerId"] == "2"  # matched by id
    assert by_ref["J3"][0]["engineerId"] is None  # unassigned
    assert by_ref["J4"][0]["lat"] is None
    assert "late" not in by_ref
    assert any("Zed" in w for w in data["warnings"])
    assert any("Nohome" in w for w in data["warnings"])
    assert data["office"]["postcode"] == "N3"


def test_geocoder_caches(tmp_path):
    calls = []

    def fetch(method, url, body):
        calls.append(url)
        return fake_fetch(method, url, body)

    cache = tmp_path / "cache.json"
    g = Geocoder(cache, fetch)
    out = g.lookup_many(["n3 1aa", "N3 1AA", "SE1 1AA", "ZZ9 9ZZ"])
    g.save()
    assert set(out) == {"N3 1AA", "SE1 1AA"}
    calls.clear()
    out2 = Geocoder(cache, fetch).lookup_many(["N3 1AA", "SE1 1AA"])
    assert out2 == out and calls == []


def test_render_html_embeds_data_safely():
    cfg = load_config("does-not-exist.json")
    data = load_dataset(MemorySource(_tables()), cfg, dt.date(2026, 10, 5), dt.date(2026, 10, 8), demo_geocoder())
    data["engineers"][0]["name"] = "</script><b>x"
    html = render_html(data)
    assert "__DATA__" not in html and "/*__APP__*/" not in html
    assert "</script><b>x" not in html
