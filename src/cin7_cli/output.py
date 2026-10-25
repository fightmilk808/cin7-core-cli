"""Render results as JSON, a text table, or CSV."""

from __future__ import annotations

import csv
import io
import json
import re


def rows_of(data: object, list_key: str | None) -> list[dict]:
    """The records to tabulate: the list under `list_key`, a bare list, or the object itself."""
    if isinstance(data, dict) and list_key and isinstance(data.get(list_key), list):
        data = data[list_key]
    if isinstance(data, list):
        return [r if isinstance(r, dict) else {"value": r} for r in data]
    if isinstance(data, dict):
        return [data]
    return [{"value": data}]


def cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return f"[{len(v)}]"
    if isinstance(v, dict):
        return "{…}"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# A sale is named by its customer reference (the Shopify order number), so that
# column leads, with the SO- number after it.
LEAD_COLUMNS = ("CustomerReference", "OrderNumber")
_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_ID_NAME = re.compile(r"(^|[a-z])(ID|Id|Guid|GUID)s?$|^(ID|Id)$")


def is_id_column(name: str, rows: list[dict]) -> bool:
    """An id column: named like one, or holding GUIDs. These go to the far right."""
    if name in LEAD_COLUMNS or name in IDENTITY or name in KEY_PRIORITY:
        return False
    if _ID_NAME.search(name):
        return True
    sample = [r.get(name) for r in rows[:50] if r.get(name)]
    return bool(sample) and all(isinstance(v, str) and _GUID.match(v) for v in sample)


# The columns that say what a row *is*. The identity column leads (a sale by its customer reference, a product
# by SKU); the next two most important ones follow and stay locked in the TUI while the table scrolls.
IDENTITY = ("CustomerReference", "SKU", "ProductSKU", "OrderNumber", "Code", "Name", "Title")
KEY_PRIORITY = (
    "SKU", "ProductSKU", "Code", "Name", "Customer", "CustomerName", "Supplier", "SupplierName", "Product",
    "ProductName", "OrderNumber", "Location", "LocationName", "Status",
)


def key_columns(cols: list[str]) -> list[str]:
    """Identity column plus the next two most important ones, in that order (fewer when the table has fewer)."""
    by_lower = {c.lower(): c for c in cols}
    identity = next((by_lower[n.lower()] for n in IDENTITY if n.lower() in by_lower), None)
    if identity is None:
        return []
    picks = [by_lower[n.lower()] for n in KEY_PRIORITY if n.lower() in by_lower and by_lower[n.lower()] != identity]
    return [identity] + picks[:2]


def columns(rows: list[dict], fields: list[str] | None = None) -> list[str]:
    """Columns in first-seen order: key columns first, ids pushed to the far right; `fields` overrides."""
    if fields:
        return fields
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    lead = key_columns(cols)
    rest = [c for c in cols if c not in lead]
    ids = [c for c in rest if is_id_column(c, rows)]
    return lead + [c for c in rest if c not in ids] + ids


def render(data: object, fmt: str = "json", list_key: str | None = None, fields: list[str] | None = None) -> str:
    """Format `data` for stdout. Table and CSV flatten nested values to counts/markers."""
    if fmt == "json":
        if fields:
            rows = [{f: r.get(f) for f in fields} for r in rows_of(data, list_key)]
            return json.dumps(rows, indent=2, default=str)
        return json.dumps(data, indent=2, default=str)
    rows = rows_of(data, list_key)
    cols = columns(rows, fields)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([cell(r.get(c)) for c in cols])
        return buf.getvalue().rstrip("\n")
    if fmt == "table":
        cells = [[cell(r.get(c)) for c in cols] for r in rows]
        widths = [min(48, max([len(c)] + [len(row[i]) for row in cells])) for i, c in enumerate(cols)]

        def line(vals: list[str]) -> str:
            return "  ".join((v if len(v) <= w else v[: w - 1] + "…").ljust(w) for v, w in zip(vals, widths)).rstrip()

        out = [line(cols), line(["-" * w for w in widths])] + [line(r) for r in cells]
        return "\n".join(out)
    raise ValueError(f"unknown output format {fmt!r}")
