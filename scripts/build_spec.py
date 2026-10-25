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


# --- internal (web app) and other surfaces ---------------------------------------

WEB_BASE = "https://inventory.dearsystems.com"
# Never runnable, whatever the map says about reads: names that log out or act.
NEVER_RUN = re.compile(
    r"log ?off|logout|delete|remove|void|undo|cancel|authori[sz]e|release|complete|recalculate|"
    r"_run\b|split|merge|reorder|import|upload|send|post\b",
    re.I,
)
MODULE_TITLES = {
    "00-shared": "Shared", "01-shell": "Shell", "02-account": "Account", "03-dashboard": "Dashboard",
    "04-inventory": "Inventory", "05-purchase": "Purchase", "06-sale": "Sale", "07-production": "Production",
    "08-financials": "Financials", "09-crm": "CRM", "10-ai": "AI", "11-reports": "Reports",
    "12-integrations": "Integrations", "13-settings": "Settings", "14-reference-books": "Reference books",
    "15-other": "Other", "16-mrp": "MRP", "25-portals": "Portals",
}


def _web_name(url: str) -> str:
    s = url.removeprefix("/api/").lstrip("/").replace("_Index_", "_")
    return flag_name(re.sub(r"[_/]+", "-", s)).replace("--", "-").strip("-")


def _module_of(url: str, sections: str, readmes: dict[str, str]) -> str:
    if "MRP" in url:
        return "16-mrp"
    hits = [m for m, text in readmes.items() if re.search(re.escape(url) + r"[`\s|)]", text)]
    hits = [m for m in hits if m != "01-shell"] or hits
    if hits:
        return hits[0]
    if "Integrations" in sections:
        return "12-integrations"
    return "15-other"


FILE_URL = re.compile(r"/(Download\w*|Export\w*)$")


def web_is_file(url: str, kind: str, effect: str, methods: str) -> bool:
    """A controller download/export: a plain read that answers with a file, not JSON.

    `/…/Print` is left out on purpose: the map says printing may log an audit line in Cin7.
    """
    return kind == "mvc" and effect == "read" and bool(FILE_URL.search(url)) and methods in ("", "GET", "GET(navigate)")


def web_read_method(url: str, effect: str, methods: str) -> str | None:
    """How a web endpoint is read, or None if it may not run from here.

    GET for store reads and plain GETs; POST only where the map marks the call
    itself `POST(read)` (a JSON query body, e.g. MRP suggestion lines).
    """
    if NEVER_RUN.search(url) or not effect.startswith("read"):
        return None
    if effect != "read" and "audit" in effect:
        return None
    tokens = methods.split()
    if methods.startswith("REST") or " REST(" in methods or "GET" in tokens:
        return "GET"
    if "POST(read)" in tokens:
        return "POST"
    return None


def build_web(rebuild: Path) -> list[dict]:
    readmes = {p.parent.name: p.read_text() for p in sorted((rebuild / "modules").glob("*/README.md"))}
    rows = list(csv.DictReader((rebuild / "data" / "endpoints.csv").open()))
    out: list[dict] = []
    seen: dict[tuple[str, str], int] = {}
    for r in rows:
        url = r["url"]
        module = _module_of(url, r["sections"], readmes)
        group = "web-" + module.split("-", 1)[1]
        name = _web_name(url)
        n = seen.get((group, name), 0)
        seen[(group, name)] = n + 1
        if n:
            name = f"{name}-{n + 1}"
        is_file = web_is_file(url, r["kind"], r["effect"], r["methods"])
        read_method = "GET" if is_file else web_read_method(url, r["effect"], r["methods"])
        runnable = read_method is not None
        params: dict[str, dict] = {}
        for p in r["params"].split():
            # Spellings differ across pages (CustomerID / customerID); one flag each.
            if p != "_dc" and flag_name(p) not in {m["flag"] for m in params.values()}:
                params[p] = {"type": "string", "flag": flag_name(p)}
                if BOOL_NAME.match(p):
                    params[p].update(type="bool", choices=["true", "false"])
        root = r["reader_root"] or None
        paged = bool(root) and "REST" in r["methods"]
        if paged:
            for p in ("page", "start", "limit"):
                if p not in {m["flag"] for m in params.values()}:
                    params[p] = {"type": "int", "flag": p}
        out.append(
            {
                "source": "web",
                "group": group,
                "group_title": "Web · " + MODULE_TITLES.get(module, module),
                "name": name,
                "resource": url,
                "title": f"{r['kind']} · {r['effect']}",
                "method": read_method or (r["methods"] or "?"),
                "path": url,
                "params": params,
                "list_key": root,
                "paged": paged,
                "runnable": runnable,
                "file": is_file,
                "effect": r["effect"],
                "methods_raw": r["methods"],
                "body_keys": r["body_keys"].split(),
                "pages": r["pages"].split(),
                "request": None,
                "response": None,
            }
        )
    return out


def build_other(rebuild: Path) -> list[dict]:
    """MES, WMS and report-service endpoints: listed for reference, never runnable here."""
    out: list[dict] = []
    for fname, group, title in (
        ("wms-api.csv", "wms", "WMS app (own login)"),
        ("mes-api.csv", "mes", "MES app (own login)"),
        ("report-service-endpoints.csv", "report-service", "Report service (session)"),
    ):
        seen: dict[str, int] = {}
        for r in csv.DictReader((rebuild / "data" / fname).open()):
            path = r.get("path") or r.get("url")
            name = _web_name(path)
            n = seen.get(name, 0)
            seen[name] = n + 1
            if n:
                name = f"{name}-{n + 1}"
            out.append(
                {
                    "source": group,
                    "group": group,
                    "group_title": title,
                    "name": name,
                    "resource": r.get("full_url") or path,
                    "title": f"{r.get('effect') or 'unknown'} · reference only",
                    "method": r.get("methods") or "?",
                    "path": path,
                    "params": {p: {"type": "string", "flag": flag_name(p)} for p in dict.fromkeys((r.get("params") or "").split())},
                    "list_key": None,
                    "paged": False,
                    "runnable": False,
                    "effect": r.get("effect") or "",
                    "methods_raw": r.get("methods") or "",
                    "body_keys": [],
                    "pages": [],
                    "request": None,
                    "response": None,
                }
            )
    return out


def build_prints(rebuild: Path) -> list[dict]:
    """Every print action in the web app, with the Cin7 pages it lives on (they are often buried)."""
    labels = {r["route"]: r["menu_label"] for r in csv.DictReader((rebuild / "data" / "pages.csv").open())}
    out = []
    for r in csv.DictReader((rebuild / "data" / "endpoints.csv").open()):
        url = r["url"]
        if r["kind"] != "mvc" or not re.search(r"/Print\w*$", url):
            continue
        params = r["params"].split()
        id_param = next((p for p in ("IDList", "orderIDs", "PrintEntityID") if p in params), None)
        out.append(
            {
                "path": url,
                "method": "GET" if r["methods"].startswith("GET") else "POST",
                "methods_raw": r["methods"],
                "params": params,
                "effect": r["effect"],
                "pages": r["pages"].split(),
                "labels": [labels.get(p, "") for p in r["pages"].split()],
                "id_param": id_param,
                "template_param": next((p for p in ("TemplateID", "PrintTemplateID") if p in params), None),
                # Sendable only where the map names an id parameter and calls it a read.
                "sendable": bool(id_param) and r["effect"].startswith("read"),
            }
        )
    return out


NOT_CHOICES = {"POST", "PUT", "GET", "DELETE"}
BOOL_NAME = re.compile(r"^(is|include|only|has|deprecated|active|allow|show|hide)[A-Za-z0-9]*$", re.I)


def _norm(name: str) -> str:
    return re.sub(r"ing", "", name.lower())


def group_choices(shapes: dict, group: str) -> dict[str, list[str]]:
    """Enumerated values per field name for a group, from the Blueprint's field tables."""
    out: dict[str, list[str]] = {}
    for t in shapes["groups"][group]["tables"]:
        for f in t["fields"]:
            vals = [v for v in (f.get("values") or []) if v not in NOT_CHOICES and v != "Not used"]
            if vals:
                merged = out.setdefault(_norm(f["name"]), [])
                merged += [v for v in dict.fromkeys(vals) if v not in merged]
    return out


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

        choices = group_choices(shapes, group)
        for pname, meta in params.items():
            if meta["type"] == "bool":
                meta["choices"] = ["true", "false"]
            elif _norm(pname) in choices:
                meta["choices"] = choices[_norm(pname)]
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
                "source": "public",
                "runnable": True,
                "group": cmd_group,
                "group_title": group,
                "name": name,
                "resource": row["resource"],
                "title": op["title"],
                "method": row["method"],
                "path": row["path"],
                "params": params,
                # A detail record that merely contains an array (/me has RoundingTable[])
                # is still one record; only paged lists and single-key envelopes are lists.
                "list_key": key if (paged or envelope) else None,
                "paged": paged,
                "request": op.get("request"),
                "response": op.get("response"),
            }
        )
    commands += build_web(rebuild) + build_other(rebuild)
    return {
        "source": "fightmilk808/cin7-core-rebuild data/ (public-api-shapes.json, public-api.csv, endpoints.csv, "
        "wms-api.csv, mes-api.csv, report-service-endpoints.csv)",
        "web_base_url": WEB_BASE,
        "prints": build_prints(rebuild),
        "base_url": "https://inventory.dearsystems.com/ExternalApi/v2",
        "commands": commands,
        "webhooks": shapes.get("webhooks", {}),
    }


def main() -> None:
    rebuild = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REBUILD
    spec = build(rebuild)
    OUT.write_text(json.dumps(spec, indent=1, sort_keys=False) + "\n")
    from collections import Counter

    by = Counter(c["source"] for c in spec["commands"])
    runnable = Counter(c["source"] for c in spec["commands"] if c["runnable"])
    groups = {c["group"] for c in spec["commands"]}
    print(f"wrote {OUT.relative_to(ROOT)}: {len(spec['commands'])} operations in {len(groups)} groups")
    for src, n in by.items():
        print(f"  {src}: {n} ({runnable[src]} runnable)")


if __name__ == "__main__":
    main()
