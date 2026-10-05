"""Made-up engineers and board bookings around London, for trying the map without the database.

Runs the full pipeline (auto-mapping, loader, geocoder) against an in-memory
copy of a CMS-like schema, with an offline stand-in for postcodes.io.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random

from . import postcodes
from .geocode import Geocoder

# Approximate outcode centres.
OUTCODES = {
    "N3": (51.6007, -0.1925), "N12": (51.6146, -0.1765), "EN5": (51.6514, -0.2003), "HA8": (51.6133, -0.2750),
    "NW7": (51.6148, -0.2449), "NW4": (51.5899, -0.2234), "N14": (51.6327, -0.1293), "E17": (51.5862, -0.0198),
    "E4": (51.6281, -0.0040), "IG8": (51.6082, 0.0306), "RM7": (51.5770, 0.1720), "DA1": (51.4440, 0.2140),
    "SE9": (51.4440, 0.0580), "BR1": (51.4060, 0.0150), "CR0": (51.3760, -0.0900), "SM1": (51.3650, -0.1900),
    "KT1": (51.4100, -0.3000), "TW3": (51.4680, -0.3610), "UB3": (51.5040, -0.4180), "HA4": (51.5720, -0.4200),
    "WD6": (51.6560, -0.2730), "AL1": (51.7500, -0.3360), "LU1": (51.8790, -0.4170), "CM1": (51.7350, 0.4690),
    "SS1": (51.5390, 0.7130), "ME1": (51.3800, 0.5000), "GU1": (51.2400, -0.5700), "RH1": (51.2400, -0.1700),
    "EC1A": (51.5200, -0.0990), "EC2A": (51.5240, -0.0820), "W1T": (51.5200, -0.1370), "SW1A": (51.5010, -0.1410),
    "SE1": (51.4990, -0.0890), "E14": (51.5050, -0.0200), "NW1": (51.5320, -0.1430), "W2": (51.5150, -0.1800),
    "N1": (51.5380, -0.1000), "E1": (51.5170, -0.0590), "SW11": (51.4660, -0.1650), "W6": (51.4930, -0.2290),
    "E15": (51.5410, 0.0040), "HA0": (51.5530, -0.2990), "CR4": (51.4030, -0.1660),
}
HOME_OUTCODES = ["N3", "N12", "EN5", "HA8", "NW7", "NW4", "N14", "E17", "E4", "IG8", "RM7", "DA1", "SE9", "BR1",
                 "CR0", "SM1", "KT1", "TW3", "UB3", "HA4", "WD6", "AL1", "LU1", "CM1", "SS1", "ME1", "GU1", "RH1",
                 "N12", "HA8", "E17", "NW7"]
SITE_OUTCODES = ["EC1A", "EC2A", "W1T", "SW1A", "SE1", "E14", "NW1", "W2", "N1", "E1", "SW11", "W6", "E15", "HA0",
                 "CR4", "KT1", "BR1", "N3", "AL1", "RH1", "SS1"]
FIRST = ["Adam", "Ben", "Callum", "Dan", "Eli", "Femi", "George", "Harry", "Imran", "Jack", "Kofi", "Liam",
         "Marek", "Nathan", "Owen", "Paul", "Rory", "Sam", "Tom", "Vik", "Will", "Yusuf", "Zak", "Rob"]
LAST = ["Ahmed", "Brown", "Clarke", "Davies", "Evans", "Fisher", "Green", "Hughes", "Iqbal", "Jones", "Khan",
        "Lewis", "Morgan", "Novak", "Okafor", "Patel", "Quinn", "Reid", "Smith", "Taylor", "Usman", "Walsh",
        "Young", "Zielinski"]
LETTERS = "ABDEFGHJLNPQRSTUWXYZ"


def _postcode(rng: random.Random, out: str) -> str:
    return f"{out} {rng.randint(1, 9)}{rng.choice(LETTERS)}{rng.choice(LETTERS)}"


def fake_fetch(method: str, url: str, body: dict | None):
    """Offline stand-in for postcodes.io: outcode centre + a stable jitter per postcode."""
    def locate(pc: str):
        out = postcodes.outcode(pc)
        if out not in OUTCODES:
            return None
        lat, lon = OUTCODES[out]
        h = hashlib.md5(pc.encode()).digest()
        return {"latitude": lat + (h[0] - 128) / 128 * 0.012, "longitude": lon + (h[1] - 128) / 128 * 0.018}

    if method == "POST":
        return {"result": [{"query": pc, "result": locate(pc)} for pc in body["postcodes"]]}
    tail = url.rsplit("/", 1)[-1]
    if "/outcodes/" in url and tail in OUTCODES:
        lat, lon = OUTCODES[tail]
        return {"result": {"latitude": lat, "longitude": lon}}
    return None


def demo_tables(today: dt.date, seed: int = 7) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    engineers = []
    for i in range(24):
        out = HOME_OUTCODES[i % len(HOME_OUTCODES)]
        pc = _postcode(rng, out)
        engineers.append({
            "EngineerID": 100 + i, "FirstName": FIRST[i], "Surname": LAST[i],
            "HomeAddress": f"{rng.randint(1, 120)} Example Road, London, {pc}",
            "HomePostcode": pc if i % 7 else None,  # some only have it inside the address
            "Active": i != 23,
        })

    board = []
    start = today - dt.timedelta(days=4)
    job_no = 5000
    for n in range(20):
        day = start + dt.timedelta(days=n)
        if day.weekday() >= 5:
            continue
        crew = rng.sample(engineers[:23], 17)
        while crew:
            job_no += 1
            pc = _postcode(rng, rng.choice(SITE_OUTCODES))
            team = crew[: rng.choice((1, 1, 2))]
            crew = crew[len(team):]
            for eng in team:
                board.append({"BoardID": len(board) + 1, "JobDate": dt.datetime.combine(day, dt.time()),
                              "EngineerID": eng["EngineerID"], "JobNo": f"J{job_no}", "SiteAddress": f"Site {job_no}",
                              "SitePostcode": pc, "MaterialsPickup": rng.random() < 0.4})
        for _ in range(rng.randint(2, 4)):
            job_no += 1
            board.append({"BoardID": len(board) + 1, "JobDate": dt.datetime.combine(day, dt.time()),
                          "EngineerID": None, "JobNo": f"J{job_no}", "SiteAddress": f"Site {job_no}",
                          "SitePostcode": _postcode(rng, rng.choice(SITE_OUTCODES)),
                          "MaterialsPickup": rng.random() < 0.5})
    return {"tblEngineers": engineers, "tblBoard": board, "tblVans": [{"VanID": 1, "Reg": "AB12 CDE"}]}


def demo_geocoder() -> Geocoder:
    return Geocoder(cache_path=None, fetch=fake_fetch)
