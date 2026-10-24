"""Render results as JSON, a text table, or CSV."""

from __future__ import annotations

import csv
import io
import json


def rows_of(data: object, list_key: str | None) -> list[dict]:
    """The records to tabulate: the list under `list_key`, a bare list, or the object itself."""
    if isinstance(data, dict) and list_key and isinstance(data.get(list_key), list):
        data = data[list_key]
    if isinstance(data, list):
        return [r if isinstance(r, dict) else {"value": r} for r in data]
    if isinstance(data, dict):
        return [data]
    return [{"value": data}]


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return f"[{len(v)}]"
    if isinstance(v, dict):
        return "{…}"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _columns(rows: list[dict], fields: list[str] | None) -> list[str]:
    if fields:
        return fields
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def render(data: object, fmt: str = "json", list_key: str | None = None, fields: list[str] | None = None) -> str:
    """Format `data` for stdout. Table and CSV flatten nested values to counts/markers."""
    if fmt == "json":
        if fields:
            rows = [{f: r.get(f) for f in fields} for r in rows_of(data, list_key)]
            return json.dumps(rows, indent=2, default=str)
        return json.dumps(data, indent=2, default=str)
    rows = rows_of(data, list_key)
    cols = _columns(rows, fields)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([_cell(r.get(c)) for c in cols])
        return buf.getvalue().rstrip("\n")
    if fmt == "table":
        cells = [[_cell(r.get(c)) for c in cols] for r in rows]
        widths = [min(48, max([len(c)] + [len(row[i]) for row in cells])) for i, c in enumerate(cols)]

        def line(vals: list[str]) -> str:
            return "  ".join((v if len(v) <= w else v[: w - 1] + "…").ljust(w) for v, w in zip(vals, widths)).rstrip()

        out = [line(cols), line(["-" * w for w in widths])] + [line(r) for r in cells]
        return "\n".join(out)
    raise ValueError(f"unknown output format {fmt!r}")
