"""The generated API map (spec.json) and lookups over it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

READ_METHODS = frozenset({"GET"})


@dataclass(frozen=True)
class Param:
    """One query parameter of an operation."""

    name: str
    flag: str
    type: str = "string"
    required: bool = False


@dataclass(frozen=True)
class Operation:
    """One public API v2 operation, as a CLI subcommand."""

    group: str
    group_title: str
    name: str
    resource: str
    title: str
    method: str
    path: str
    params: tuple[Param, ...]
    list_key: str | None = None
    paged: bool = False
    request: object = None
    response: object = None
    aliases: tuple[str, ...] = field(default=())
    source: str = "public"  # public | web | wms | mes | report-service
    runnable: bool = True
    effect: str = ""
    methods_raw: str = ""
    body_keys: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    file: bool = False

    @property
    def is_public(self) -> bool:
        """Public API v2 (API key) rather than an internal surface."""
        return self.source == "public"

    @property
    def is_write(self) -> bool:
        """Public: anything but GET. Internal: anything the map doesn't call a read
        (those are never sent from here)."""
        if self.is_public:
            return self.method not in READ_METHODS
        return not self.runnable

    @property
    def read_via_post(self) -> bool:
        """An internal read the web app sends as POST with a JSON query body."""
        return not self.is_public and self.runnable and self.method == "POST"

    @property
    def full_name(self) -> str:
        """`group name`, the string a person types to confirm a write."""
        return f"{self.group} {self.name}"


def _load_raw() -> dict:
    return json.loads(resources.files("cin7_cli").joinpath("spec.json").read_text())


@lru_cache(maxsize=1)
def load() -> dict:
    """Return {"operations": [Operation...], "webhooks": {...}, "base_url": str}."""
    raw = _load_raw()
    ops = []
    for c in raw["commands"]:
        params = tuple(
            Param(name=n, flag=m["flag"], type=m.get("type", "string"), required=bool(m.get("required")))
            for n, m in c["params"].items()
        )
        ops.append(
            Operation(
                group=c["group"],
                group_title=c["group_title"],
                name=c["name"],
                resource=c["resource"],
                title=c["title"],
                method=c["method"],
                path=c["path"],
                params=params,
                list_key=c.get("list_key"),
                paged=bool(c.get("paged")),
                request=c.get("request"),
                response=c.get("response"),
                source=c.get("source", "public"),
                runnable=bool(c.get("runnable", True)),
                effect=c.get("effect", ""),
                methods_raw=c.get("methods_raw", ""),
                body_keys=tuple(c.get("body_keys") or ()),
                pages=tuple(c.get("pages") or ()),
                file=bool(c.get("file")),
            )
        )
    return {
        "operations": _with_aliases(ops),
        "webhooks": raw.get("webhooks", {}),
        "base_url": raw["base_url"],
        "web_base_url": raw.get("web_base_url", "https://inventory.dearsystems.com"),
    }


def _with_aliases(ops: list[Operation]) -> list[Operation]:
    """Give a collection `list` a `get` alias when the group has no `get` of its own.

    Many Cin7 resources (`/product`, `/customer`, `/supplier`) answer both a list
    and a single record from the same GET, picked by an optional `ID`.
    """
    names = {(o.group, o.name) for o in ops}
    out = []
    for o in ops:
        aliases: tuple[str, ...] = ()
        if o.is_public and o.method == "GET" and o.name.endswith("list"):
            alias = o.name[: -len("list")] + "get"
            if (o.group, alias) not in names and any(p.name.lower() == "id" for p in o.params):
                aliases = (alias,)
        out.append(Operation(**{**o.__dict__, "aliases": aliases}))
    return out


def operations() -> list[Operation]:
    """Every operation in spec order."""
    return load()["operations"]


def groups() -> dict[str, list[Operation]]:
    """Operations grouped by command group, in spec order."""
    out: dict[str, list[Operation]] = {}
    for o in operations():
        out.setdefault(o.group, []).append(o)
    return out


def find(group: str, name: str) -> Operation | None:
    """Look an operation up by group and subcommand name or alias."""
    for o in groups().get(group, []):
        if o.name == name or name in o.aliases:
            return o
    return None
