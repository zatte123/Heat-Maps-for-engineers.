"""Postcode -> lat/lon using postcodes.io (free, no API key), with a local cache.

Each postcode is only looked up once; results are kept in a JSON file so
re-running the map is fast and works offline for postcodes already seen.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from . import postcodes

API = "https://api.postcodes.io"
BATCH = 100  # postcodes.io bulk limit

# (method, url, json body or None) -> parsed JSON response, or None on 404
Fetcher = Callable[[str, str, dict | None], dict | None]


def _http_fetch(method: str, url: str, body: dict | None) -> dict | None:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "engineer-heatmap/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


class Geocoder:
    def __init__(self, cache_path: str | Path | None = None, fetch: Fetcher = _http_fetch):
        self.cache_path = Path(cache_path) if cache_path else None
        self.fetch = fetch
        self.cache: dict[str, dict] = {}
        if self.cache_path and self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))

    def save(self) -> None:
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")

    def lookup_many(self, raw: Iterable[str | None]) -> dict[str, dict]:
        """Geocode every postcode in ``raw``.

        Returns {normalised postcode: {"lat", "lon", "approx"}}. ``approx`` is
        True when only the outcode (e.g. "N3") could be placed. Postcodes that
        can't be found at all are left out.
        """
        wanted = {pc for pc in (postcodes.normalise(r) for r in raw) if pc}
        missing = sorted(pc for pc in wanted if pc not in self.cache)

        full = [pc for pc in missing if " " in pc]
        for i in range(0, len(full), BATCH):
            chunk = full[i : i + BATCH]
            resp = self.fetch("POST", f"{API}/postcodes", {"postcodes": chunk}) or {}
            for item in resp.get("result") or []:
                res = item.get("result")
                if res and res.get("latitude") is not None:
                    pc = postcodes.normalise(item["query"])
                    self.cache[pc] = {"lat": res["latitude"], "lon": res["longitude"], "approx": False}

        # Terminated postcodes are common in older address books.
        for pc in full:
            if pc in self.cache:
                continue
            resp = self.fetch("GET", f"{API}/terminated_postcodes/{pc.replace(' ', '')}", None)
            res = (resp or {}).get("result")
            if res and res.get("latitude") is not None:
                self.cache[pc] = {"lat": res["latitude"], "lon": res["longitude"], "approx": False}

        # Fall back to the outcode centre (also handles bare outcodes like "N3").
        for pc in missing:
            if pc in self.cache:
                continue
            out = postcodes.outcode(pc)
            resp = self.fetch("GET", f"{API}/outcodes/{out}", None)
            res = (resp or {}).get("result")
            if res and res.get("latitude") is not None:
                self.cache[pc] = {"lat": res["latitude"], "lon": res["longitude"], "approx": True}

        return {pc: self.cache[pc] for pc in wanted if pc in self.cache}
