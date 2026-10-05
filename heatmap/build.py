"""Turn a dataset into a single self-contained HTML page."""
from __future__ import annotations

import json
from pathlib import Path

WEB = Path(__file__).parent / "web"


def render_html(dataset: dict) -> str:
    page = (WEB / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(dataset, default=str, separators=(",", ":")).replace("</", "<\\/")
    # Plain string replacement (not str.format) so braces in CSS/JS are left alone.
    for marker, content in (
        ("/*__CSS__*/", (WEB / "style.css").read_text(encoding="utf-8")),
        ("/*__PLANNER__*/", (WEB / "planner.js").read_text(encoding="utf-8")),
        ("/*__APP__*/", (WEB / "app.js").read_text(encoding="utf-8")),
        ("__DATA__", payload),
    ):
        page = page.replace(marker, content, 1)
    return page


def write_html(dataset: dict, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(dataset), encoding="utf-8")
    return out
