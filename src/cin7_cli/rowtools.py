"""Pure helpers behind the TUI's table: record links, record ids, status colours, sorting."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable

from . import spec

WEB_ROOT = "https://inventory.dearsystems.com"

# (op group, route) per list. Routes come from the rebuild map's pages.csv; the
# record opens at `/<Route>#<guid>`.
ROUTES: dict[str, str] = {
    "sale": "Sale",
    "product": "Product",
    "product-family": "ProductFamily",
    "purchase": "Purchase",
    "supplier": "Supplier",
    "customer": "Customer",
    "production": "ProductionOrder",
    "stock:transfer": "StockTransfer",
    "stock:adjustment": "StockAdjustment",
    "finished-goods": "FinishedGoods",
    "disassembly": "Disassembly",
    "journal": "Journal",
    "web-mrp": "MRP",
}

# Id fields, most specific first. The first present and GUID-shaped wins.
ID_FIELDS = ("SaleID", "ProductionOrderID", "TaskID", "MrpID", "ID", "ProductID", "SupplierID", "CustomerID")
GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def record_id(row: dict) -> str | None:
    """The row's own GUID (not a foreign key when a more specific field exists)."""
    for f in ID_FIELDS:
        v = row.get(f)
        if isinstance(v, str) and GUID.match(v):
            return v
    return None


def record_url(op: spec.Operation, row: dict) -> str | None:
    """Link to the record in the Cin7 web app, or None when there is no known page."""
    key = op.group
    if op.group == "stock":
        key = "stock:" + ("adjustment" if "adjustment" in op.name else "transfer" if "transfer" in op.name else "")
    route = ROUTES.get(key)
    if route is None:
        return None
    if op.group == "purchase" and "advanced" in str(row.get("Type", "")).lower():
        route = "PurchaseAdvanced"
    if op.group == "production":
        rid = row.get("ProductionOrderID")
        rid = rid if isinstance(rid, str) and GUID.match(rid) else None
    else:
        rid = record_id(row)
    return f"{WEB_ROOT}/{route}#{rid}" if rid else None


# --- status colours ------------------------------------------------------------

STATUS_FIELDS = (
    "Status", "OrderStatus", "CombinedShippingStatus", "CombinedInvoiceStatus", "CombinedPickStatus", "QuoteStatus",
)
GOOD = {"shipped", "fulfilled", "completed", "complete", "invoiced", "paid", "received", "authorised", "active",
        "fully shipped", "fully invoiced", "fully received", "closed", "done"}
BAD = {"backordered", "back ordered", "void", "voided", "cancelled", "canceled", "overdue", "error", "failed", "unpaid",
       "inactive", "deprecated", "suspended"}
WAIT = {"draft", "ordered", "pending", "not available", "not shipped", "not picked", "not packed", "picking", "packing",
        "partially shipped", "partially received", "partially picked", "partially invoiced", "released", "in progress",
        "not invoiced", "not received"}
DATE_FIELDS = ("ShipBy", "ShipByDate", "RequiredByDate", "RequiredBy", "DueDate", "InvoiceDueDate")


def _parse_date(v: object) -> dt.date | None:
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        return dt.date.fromisoformat(v[:10])
    except ValueError:
        return None


def row_style(row: dict, today: dt.date | None = None) -> str | None:
    """Rich style for a whole row from its status and due date.

    Red: backordered, void, cancelled, overdue and still open. Green: done.
    Yellow: in progress. Anything else: no colour.
    """
    today = today or dt.date.today()
    statuses = [str(row[f]).strip().lower() for f in STATUS_FIELDS if row.get(f)]
    done = any(s in GOOD for s in statuses)
    if any(s in BAD for s in statuses):
        return "red"
    if not done:
        for f in DATE_FIELDS:
            d = _parse_date(row.get(f))
            if d is not None and d < today and statuses:
                return "red"
    if done:
        return "green"
    if any(s in WAIT for s in statuses):
        return "yellow"
    return None


# --- sorting -----------------------------------------------------------------------


def sort_key(value: object):
    """Numbers sort as numbers, dates and text as text; empty values sort last."""
    if value is None or value == "":
        return (2, 0, "")
    if isinstance(value, bool):
        return (1, 0, str(value))
    if isinstance(value, (int, float)):
        return (0, float(value), "")
    if isinstance(value, (list, dict)):
        return (0, float(len(value)), "")
    text = str(value)
    try:
        return (0, float(text.replace(",", "")), "")
    except ValueError:
        return (1, 0, text.lower())


def sort_rows(rows: Iterable[dict], column: str | None, reverse: bool = False) -> list[dict]:
    """Stable sort by `column`; blanks stay at the bottom either way."""
    rows = list(rows)
    if not column:
        return rows
    present = [r for r in rows if sort_key(r.get(column))[0] != 2]
    blank = [r for r in rows if sort_key(r.get(column))[0] == 2]
    present.sort(key=lambda r: sort_key(r.get(column)), reverse=reverse)
    return present + blank


def frozen_columns(cols: list[str]) -> int:
    """How many leading columns stay put while the table scrolls sideways."""
    lead = ("CustomerReference", "OrderNumber", "SKU", "Name")
    n = 0
    for c, want in zip(cols, lead):
        if c != want:
            break
        n += 1
    # CustomerReference + OrderNumber together are the sale's name; keep both.
    return min(n, 2) if n else (1 if cols and cols[0] in ("SKU", "Name", "OrderNumber", "Code") else 0)
