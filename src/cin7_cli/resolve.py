"""Bridge from something a person knows (customer ref, SO number, SKU, name) to the GUID a call wants.

A parameter such as `--id` or `--sale-id` accepts text as well as a GUID. Text is looked up through the matching
list operation (read-only, API key), and the one match is used; several matches are shown so one can be chosen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import runner, spec

GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Resolver:
    """How to find records of one kind: which list, which filter, which field is the id, how to label a row."""

    group: str
    list_op: str
    search: tuple[str, ...]  # filter params to try, in order, until one returns rows
    id_field: str
    label: tuple[str, ...]
    hint: str


RESOLVERS = {
    "sale": Resolver("sale", "list", ("Search",), "SaleID", ("CustomerReference", "OrderNumber", "Customer", "Status"),
                     "ref, SO-… or id"),
    "purchase": Resolver("purchase", "list", ("Search",), "ID", ("OrderNumber", "Supplier", "Status"), "PO number or id"),
    "product": Resolver("product", "list", ("Sku", "Name"), "ID", ("SKU", "Name"), "SKU, name or id"),
    "customer": Resolver("customer", "list", ("Name",), "ID", ("Name", "Status"), "name or id"),
    "supplier": Resolver("supplier", "list", ("Name",), "ID", ("Name", "Status"), "name or id"),
    "production": Resolver("production", "order-list", ("Search",), "ProductionOrderID",
                           ("OrderNumber", "ProductSKU", "ProductName", "Status"), "MO number, SKU or id"),
    "location": Resolver("location", "list", ("Name",), "ID", ("Name",), "name or id"),
}
# Parameter names that mean one kind of record whatever operation they sit on.
BY_PARAM = {
    "saleid": "sale", "purchaseid": "purchase", "productid": "product", "customerid": "customer",
    "supplierid": "supplier", "productionorderid": "production", "locationid": "location",
}


def is_guid(text: str) -> bool:
    return bool(GUID.match(text.strip()))


def resolver_for(op: spec.Operation, param: spec.Param) -> Resolver | None:
    """The resolver for `param` on `op`, or None when it isn't a record id we can look up."""
    name = param.name.lower()
    if name in BY_PARAM:
        return RESOLVERS[BY_PARAM[name]]
    if name in ("id", "taskid") and op.is_public:
        return RESOLVERS.get(op.group)
    return None


def lookup_for(op: spec.Operation, param: spec.Param, value: object) -> Resolver | None:
    """The resolver to use when `value` for `param` is text rather than an id; None when no lookup is needed.

    Raises ValueError for a GUID-typed parameter that holds text we cannot look up.
    """
    if value is None or is_guid(str(value)):
        return None
    r = resolver_for(op, param)
    if r is not None:
        return r
    if param.type == "guid":
        raise ValueError(f"--{param.flag} expects a GUID, got {value!r}")
    return None


def label_of(r: Resolver, row: dict) -> str:
    return " · ".join(str(row[f]) for f in r.label if row.get(f)) or str(row.get(r.id_field))


def search(r: Resolver, text: str, make_client, limit: int = 25) -> list[tuple[str, str]]:
    """(label, id) for records matching `text`. Tries each filter until one finds rows."""
    list_op = spec.find(r.group, r.list_op)
    client = make_client(list_op)
    for param in r.search:
        data = runner.fetch(client, list_op, {param: text, "Limit": limit}, progress=False)
        rows = data.get(list_op.list_key) or [] if isinstance(data, dict) else []
        found = [(label_of(r, row), row[r.id_field]) for row in rows if isinstance(row.get(r.id_field), str)]
        if found:
            return found
    return []
