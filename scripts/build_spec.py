"""Build src/cin7_cli/spec.json from the cin7-core-rebuild API map.

Usage:
    python scripts/build_spec.py [path/to/cin7-core-rebuild]

Reads data/public-api-shapes.json (group -> operations with params and body
shapes) and data/public-api.csv (one row per operation, in the same order, with
resource name, bare path and required-parameter marks), and writes one flat
list of commands the CLI registers at start-up.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "cin7_cli" / "spec.json"
DEFAULT_REBUILD = ROOT.parent / "cin7-core-rebuild"

METHOD_VERBS = {"GET": "get", "POST": "create", "PUT": "update", "DELETE": "delete"}

TYPE_MAP = {
    "number": "int",
    "int": "int",
    "boolean": "bool",
    "bool": "bool",
    "guid": "guid",
    "uniqueidentifier": "guid",
    "date": "date",
    "datetime": "date",
    "datetime?": "date",
    "string": "string",
    "": "string",
}


def slug(text: str) -> str:
    """Lower-case kebab slug of a display name."""
    text = re.sub(r"[^A-Za-z0-9]+", " ", text).strip().lower()
    return "-".join(text.split())


def flag_name(param: str) -> str:
    """CamelCase query parameter -> kebab flag (OrderLocationID -> order-location-id)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", param)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "-", s)
    return s.replace("_", "-").lower()


def words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^A-Za-z0-9]+", " ", text).lower().split() if w]


def stem(w: str) -> str:
    """Crude singular form, enough to match 'Categories'/'Category', 'Webhooks'/'Webhook'."""
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def resource_suffix(group: str, resource: str) -> str:
    """Resource name with the group's own words stripped from the front."""
    g = [stem(w) for w in words(group)]
    r = words(resource)
    i = 0
    while i < len(g) and i < len(r) and stem(r[i]) == g[i]:
        i += 1
    if i < len(g):  # group name is not a prefix: keep the whole resource name
        return "-".join(r)
    return "-".join(r[i:])


# Responses the Blueprint parser could only outline as `_keys`; their list key is
# documented in cin7-core-rebuild docs/PUBLIC-API.md.
LIST_KEY_OVERRIDES = {"/product": "Products", "/production/workcenters": "Workcenters"}


def list_key(path: str, response) -> str | None:
    """Name of the array in a `{Total, Page, <Key>: [...]}` style response."""
    if path in LIST_KEY_OVERRIDES:
        return LIST_KEY_OVERRIDES[path]
    if not isinstance(response, dict):
        return None
    keys = [k for k, v in response.items() if isinstance(v, list)]
    return keys[0] if len(keys) == 1 else None


def path_query_names(path: str) -> list[str]:
    if "?" not in path:
        return []
    return [m for m in re.findall(r"([A-Za-z0-9_]+)=\{", path.split("?", 1)[1])]


def command_name(group: str, row: dict, op: dict, collection: bool) -> str:
    suffix = resource_suffix(group, row["resource"])
    title = op["title"].strip()
    method = row["method"]
    if title.upper() == method or title.lower() == "delete":
        verb = METHOD_VERBS[method]
        if method == "GET" and (collection or suffix == "list" or suffix.endswith("-list")):
            verb = "list"
    else:
        skip = set(words(suffix.replace("-", " "))) | {stem(w) for w in words(group)}
        verb = "-".join(w for w in words(title) if w not in skip and stem(w) not in skip) or slug(title)
    if suffix.endswith("list") and verb == "list":
        return suffix
    return f"{suffix}-{verb}" if suffix else verb


def build(rebuild: Path) -> dict:
    shapes = json.loads((rebuild / "data" / "public-api-shapes.json").read_text())
    rows = list(csv.DictReader((rebuild / "data" / "public-api.csv").open()))
    ops = [(g, o) for g, v in shapes["groups"].items() for o in v["operations"]]
    if len(rows) != len(ops):
        raise SystemExit(f"CSV has {len(rows)} rows but shapes has {len(ops)} operations")

    commands = []
    seen: dict[tuple[str, str], int] = {}
    for row, (group, op) in zip(rows, ops):
        if row["group"] != group or row["method"] != op["method"]:
            raise SystemExit(f"CSV and shapes disagree at {row}")
        required = {p.rstrip("*") for p in row["params"].split() if p.endswith("*")}
        params: dict[str, dict] = {}
        lower: dict[str, str] = {}

        def add(name: str, typ: str) -> None:
            # Spellings differ between sources (returnAttachmentsContent vs
            # ReturnAttachmentsContent); keep the first, which comes from shapes.
            if name.lower() not in lower:
                lower[name.lower()] = name
                params[name] = {"type": typ}

        for p in op["params"]:
            add(p["name"], TYPE_MAP.get(p["type"].lower(), "string"))
        for name in path_query_names(op["path"]) + [q for q in row["query"].split(",") if q]:
            add(name, "string")
        # The CSV is authoritative for required marks; shapes sometimes miss them.
        for name in required:
            add(name, "string")
            params[lower[name.lower()]]["required"] = True
        for p in op["params"]:
            if p.get("required"):
                params[p["name"]]["required"] = True

        key = list_key(row["path"], op.get("response")) if row["method"] == "GET" else None
        has_required = any(m.get("required") for m in params.values())
        paged = key is not None and "page" in lower
        cmd_group = slug(group)
        resp = op.get("response")
        envelope = row["path"] in LIST_KEY_OVERRIDES or (isinstance(resp, dict) and len(resp) == 1)
        collection = key is not None and not has_required and (paged or envelope)
        name = command_name(group, row, op, collection)
        n = seen.get((cmd_group, name), 0)
        seen[(cmd_group, name)] = n + 1
        if n:
            name = f"{name}-{n + 1}"
        for pname, meta in params.items():
            meta["flag"] = flag_name(pname)
        commands.append(
            {
                "group": cmd_group,
                "group_title": group,
                "name": name,
                "resource": row["resource"],
                "title": op["title"],
                "method": row["method"],
                "path": row["path"],
                "params": params,
                "list_key": key,
                "paged": paged,
                "request": op.get("request"),
                "response": op.get("response"),
            }
        )
    return {
        "source": "fightmilk808/cin7-core-rebuild data/public-api-shapes.json + data/public-api.csv",
        "base_url": "https://inventory.dearsystems.com/ExternalApi/v2",
        "commands": commands,
        "webhooks": shapes.get("webhooks", {}),
    }


def main() -> None:
    rebuild = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REBUILD
    spec = build(rebuild)
    OUT.write_text(json.dumps(spec, indent=1, sort_keys=False) + "\n")
    groups = {c["group"] for c in spec["commands"]}
    print(f"wrote {OUT.relative_to(ROOT)}: {len(spec['commands'])} operations in {len(groups)} groups")


if __name__ == "__main__":
    main()
