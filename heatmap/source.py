"""Table readers: the real Access database, and an in-memory stand-in for tests/demo."""
from __future__ import annotations

from typing import Any, Protocol


class TableSource(Protocol):
    def tables(self) -> list[str]: ...

    def columns(self, table: str) -> list[str]: ...

    def rows(self, table: str, columns: list[str], date_col: str | None = None,
             date_from: Any = None, date_to: Any = None) -> list[dict]: ...


def _q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


class AccessSource:
    """Reads an .accdb/.mdb through the Microsoft Access ODBC driver (Windows).

    Needs ``pip install pyodbc`` and the "Microsoft Access Database Engine"
    redistributable with the same bitness (32/64-bit) as your Python.
    """

    def __init__(self, path: str):
        try:
            import pyodbc  # imported here so the demo and tests work without it
        except ImportError:
            raise RuntimeError("pyodbc isn't installed - run: pip install -r requirements.txt") from None

        drivers = [d for d in pyodbc.drivers() if "Access Driver" in d]
        if not drivers:
            raise RuntimeError(
                "No Microsoft Access ODBC driver found. Install the 'Microsoft Access Database "
                "Engine' redistributable matching your Python bitness, then retry. "
                f"Drivers available: {pyodbc.drivers()}"
            )
        self.conn = pyodbc.connect(f"DRIVER={{{drivers[0]}}};DBQ={path};ReadOnly=1;", autocommit=True)

    def tables(self) -> list[str]:
        cur = self.conn.cursor()
        return sorted(r.table_name for r in cur.tables(tableType="TABLE"))

    def columns(self, table: str) -> list[str]:
        cur = self.conn.cursor()
        return [r.column_name for r in cur.columns(table=table)]

    def rows(self, table, columns, date_col=None, date_from=None, date_to=None):
        sql = f"SELECT {', '.join(_q(c) for c in columns)} FROM {_q(table)}"
        params: list = []
        if date_col and date_from is not None and date_to is not None:
            sql += f" WHERE {_q(date_col)} >= ? AND {_q(date_col)} < ?"
            params = [date_from, date_to]
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return [dict(zip(columns, r)) for r in cur.fetchall()]


class MemorySource:
    """``{"TableName": [ {col: value, ...}, ... ]}`` - used by tests and the demo."""

    def __init__(self, data: dict[str, list[dict]]):
        self.data = data

    def tables(self):
        return sorted(self.data)

    def columns(self, table):
        rows = self.data[table]
        return list(rows[0].keys()) if rows else []

    def rows(self, table, columns, date_col=None, date_from=None, date_to=None):
        out = []
        for row in self.data[table]:
            if date_col and date_from is not None and date_to is not None:
                value = row.get(date_col)
                if value is None or not (date_from <= value < date_to):
                    continue
            out.append({c: row.get(c) for c in columns})
        return out
