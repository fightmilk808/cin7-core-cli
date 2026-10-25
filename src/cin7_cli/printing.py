"""Printing from Cin7's web app: where it lives, and a gated way to run it.

Printing builds a document (pick list, PO, invoice) from a template and returns
a PDF. It changes no record, but the rebuild map marks these endpoints
"read (may log an audit line)", meaning Cin7 might note who printed what. So
printing is never part of the plain read path: it needs the signed-in browser
session, an explicit allow, and a typed confirmation each time.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import runner, spec
from .client import Request

# A list result's operation -> the bulk print endpoints that apply to its rows.
LIST_PRINTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("sale", "list"): ("/SaleList/Print", "/SaleList/PrintCombined"),
    ("purchase", "list"): ("/PurchaseList/Print",),
    ("product", "list"): ("/Product/Print",),
    ("customer", "list"): ("/CustomerList/Print",),
    ("production", "order-list"): ("/ProductionOrdersList/Print",),
    ("web-sale", "sale-list"): ("/SaleList/Print", "/SaleList/PrintCombined"),
    ("web-production", "production-orders-list"): ("/ProductionOrdersList/Print",),
}


@dataclass(frozen=True)
class PrintEndpoint:
    """One print action in the web app."""

    path: str
    method: str
    params: tuple[str, ...]
    effect: str
    pages: tuple[str, ...]
    labels: tuple[str, ...]
    id_param: str | None
    template_param: str | None
    sendable: bool

    @property
    def bulk(self) -> bool:
        """Takes a list of ids (the list-view Print) rather than one record."""
        return self.id_param in ("IDList", "orderIDs")

    @property
    def where(self) -> str:
        """Cin7 screens that carry this button, by menu name."""
        return ", ".join(l or p for l, p in zip(self.labels, self.pages))


def endpoints() -> list[PrintEndpoint]:
    """Every print action in the map."""
    raw = spec._load_raw().get("prints", [])
    return [
        PrintEndpoint(
            path=r["path"], method=r["method"], params=tuple(r["params"]), effect=r["effect"], pages=tuple(r["pages"]),
            labels=tuple(r["labels"]), id_param=r["id_param"], template_param=r["template_param"], sendable=r["sendable"],
        )
        for r in raw
    ]


def find(path: str) -> PrintEndpoint | None:
    return next((p for p in endpoints() if p.path.lower() == path.lower()), None)


def for_list(op: spec.Operation) -> list[PrintEndpoint]:
    """Bulk print endpoints that apply to the rows `op` returns."""
    return [p for path in LIST_PRINTS.get((op.group, op.name), ()) if (p := find(path)) and p.sendable]


def build_request(pe: PrintEndpoint, ids: list[str], template_id: str | None = None, extra: dict | None = None) -> Request:
    """The request for printing `ids`. Bulk prints are form POSTs; single ones are GETs."""
    if not ids:
        raise ValueError("nothing to print")
    if not pe.sendable:
        raise ValueError(f"{pe.path} is not mapped well enough to send (no id parameter)")
    if pe.template_param and not template_id:
        raise ValueError(f"{pe.path} needs a template id (cin7 print templates)")
    root = runner.web_base_url()
    fields: dict[str, object] = dict(extra or {})
    if pe.template_param:
        fields[pe.template_param] = template_id
    if pe.method == "GET":
        if len(ids) != 1:
            raise ValueError(f"{pe.path} prints one record at a time")
        fields[pe.id_param] = ids[0]
        from .client import build_url

        return Request("GET", build_url(pe.path, fields, root))
    fields[pe.id_param] = ",".join(ids)
    return Request("POST", root + pe.path, form={k: str(v) for k, v in fields.items()})
