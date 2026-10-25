"""`cin7 mcp`: an MCP server over stdio (stdlib only, no SDK).

Two kinds of tools:

- **Map** tools answer from the bundled spec.json (the cin7-core-rebuild map):
  which endpoint does what, its parameters and shapes, read or write, which
  page calls it. No credentials, no network.
- **Read** calls the public API v2 with the user's own API key, GET only.

There is no write path here at all, whatever CIN7_CLI_READ_ONLY says: an agent
host gets reads. Internal (web app) endpoints are described but never sent.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout; logs go to stderr.
"""

from __future__ import annotations

import json
import sys
from typing import Callable

from . import __version__, auth, runner, spec
from .client import ApiError, Client
from .ratelimit import DEFAULT_PER_MIN, RateLimiter

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_ROWS = 200
MAX_PAGES = 10
SEARCH_LIMIT = 50
SOURCES = ("public", "web", "wms", "mes", "report-service")

INSTRUCTIONS = (
    "Cin7 Core (DEAR Inventory). Use the map tools (search_operations, describe_operation, page_operations, "
    "list_groups) to find endpoints; they need no credentials. `read` sends one public API v2 GET with the user's "
    "API key and returns at most 200 rows. Nothing here can write to Cin7, and internal web-app endpoints are "
    "described only, never sent."
)


class ToolError(Exception):
    """A tool call that failed in a way the agent should see (isError result)."""


# --- map helpers ------------------------------------------------------------------


def _ident(op: spec.Operation) -> str:
    return op.full_name


def _kind(op: spec.Operation) -> str:
    if op.is_public:
        return "write" if op.is_write else "read"
    return "read" if op.runnable else (op.effect or "write")


def _summary(op: spec.Operation) -> dict:
    out = {
        "id": _ident(op),
        "source": op.source,
        "method": op.method,
        "path": op.path,
        "kind": _kind(op),
        "sendable_here": op.is_public and op.method == "GET" and not op.file,
    }
    if op.resource and op.resource != op.path:
        out["resource"] = op.resource
    if op.list_key:
        out["list_key"] = op.list_key
    if op.pages:
        out["pages"] = list(op.pages)
    return out


def _find(ident: str) -> spec.Operation:
    parts = ident.strip().split()
    if len(parts) == 2:
        op = spec.find(parts[0], parts[1])
        if op:
            return op
    raise ToolError(f"no operation {ident!r}: use search_operations to find its id (\"<group> <name>\")")


# --- tools ------------------------------------------------------------------------


def search_operations(query: str = "", source: str | None = None, reads_only: bool = False,
                      limit: int = 20) -> dict:
    """Every operation whose id, path, resource, title or calling page contains all the query words."""
    if source and source not in SOURCES:
        raise ToolError(f"source must be one of {', '.join(SOURCES)}")
    words = [w for w in query.lower().replace("/", " ").replace("_", " ").split() if w]
    hits = []
    for op in spec.operations():
        if source and op.source != source:
            continue
        if reads_only and _kind(op) != "read":
            continue
        hay = " ".join([op.group, op.name, op.path, op.resource, op.title, op.group_title, *op.pages]).lower()
        hay = hay.replace("/", " ").replace("_", " ").replace("-", " ")
        squashed = hay.replace(" ", "")
        if all(w in hay or w in squashed for w in words):
            hits.append(op)
    hits.sort(key=lambda o: (not o.is_public, _kind(o) != "read", len(o.path)))
    limit = max(1, min(int(limit), SEARCH_LIMIT))
    return {"matches": len(hits), "shown": min(len(hits), limit), "operations": [_summary(o) for o in hits[:limit]]}


def describe_operation(id: str) -> dict:  # noqa: A002 - the tool's argument name
    """Everything the map knows about one operation."""
    op = _find(id)
    out = _summary(op)
    out.update({
        "group_title": op.group_title,
        "title": op.title,
        "params": [
            {"name": p.name, "type": p.type, "required": p.required, **({"choices": list(p.choices)} if p.choices else {})}
            for p in op.params
        ],
        "paged": op.paged,
    })
    if op.methods_raw and op.methods_raw != op.method:
        out["methods_raw"] = op.methods_raw
    if op.body_keys:
        out["body_keys"] = list(op.body_keys)
    if op.request is not None:
        out["request"] = op.request
    if op.response is not None:
        out["response"] = op.response
    if op.aliases:
        out["aliases"] = [f"{op.group} {a}" for a in op.aliases]
    return out


def page_operations(page: str) -> dict:
    """The internal endpoints one web-app page calls, e.g. /MRPList."""
    page = "/" + page.strip().lstrip("/")
    ops = [o for o in spec.operations() if any(p.lower() == page.lower() for p in o.pages)]
    if not ops:
        raise ToolError(f"no operations list the page {page!r} (pages look like /MRPList or /Sale)")
    return {"page": page, "count": len(ops), "operations": [_summary(o) for o in ops]}


def list_groups(source: str | None = None) -> dict:
    """Operation groups with how many reads and writes each has."""
    if source and source not in SOURCES:
        raise ToolError(f"source must be one of {', '.join(SOURCES)}")
    out: dict[str, dict] = {}
    for op in spec.operations():
        if source and op.source != source:
            continue
        g = out.setdefault(op.group, {"group": op.group, "title": op.group_title, "source": op.source,
                                      "reads": 0, "writes": 0})
        g["reads" if _kind(op) == "read" else "writes"] += 1
    return {"groups": list(out.values())}


def auth_status() -> dict:
    """Where the API credentials come from (env, config or missing). Never the values."""
    src = auth.source()
    return {
        "account_id": src[auth.ACCOUNT_ENV],
        "app_key": src[auth.KEY_ENV],
        "ready": src[auth.ACCOUNT_ENV] != "missing" and src[auth.KEY_ENV] != "missing",
        "how_to_set": "set CIN7_ACCOUNT_ID and CIN7_APP_KEY (values or op:// references) in the MCP server's "
                      "environment, or run `cin7 auth setup`",
    }


class Reader:
    """Sends public GETs. Credentials are resolved once per server, so op:// costs one approval."""

    def __init__(self, client_factory: Callable[[], Client] | None = None):
        self._factory = client_factory or self._default_client
        self._client: Client | None = None

    @staticmethod
    def _default_client() -> Client:
        rate = float(auth.setting("CIN7_RATE_PER_MIN", str(DEFAULT_PER_MIN)))
        return Client(auth.load(), RateLimiter(rate))

    def client(self) -> Client:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def read(self, id: str, params: dict | None = None, all_pages: bool = False,  # noqa: A002
             max_pages: int = 3) -> dict:
        """One public API v2 GET (or several pages of it), rows capped at MAX_ROWS."""
        op = _find(id)
        if not op.is_public:
            raise ToolError(f"{op.full_name} is an internal {op.source} endpoint: described here, never sent")
        if op.method != "GET":
            raise ToolError(f"{op.full_name} is a write ({op.method}); this server only reads")
        if op.file:
            raise ToolError(f"{op.full_name} returns a file; use the cin7 CLI for downloads")
        query = _query(op, params or {})
        pages = max(1, min(int(max_pages), MAX_PAGES))
        data = runner.fetch(self.client(), op, query, all_pages=bool(all_pages) and op.paged,
                            max_pages=pages, progress=False)
        return _cap(data, op.list_key)


def _query(op: spec.Operation, params: dict) -> dict:
    names = {p.name.lower(): p.name for p in op.params}
    out = {}
    for k, v in params.items():
        real = names.get(str(k).lower())
        if real is None:
            valid = ", ".join(p.name for p in op.params) or "none"
            raise ToolError(f"{op.full_name} has no parameter {k!r} (valid: {valid})")
        out[real] = v
    missing = [p.name for p in op.params if p.required and p.name not in out]
    if missing:
        raise ToolError(f"{op.full_name} needs {', '.join(missing)}")
    return out


def _cap(data: object, key: str | None) -> dict:
    if isinstance(data, dict) and key and isinstance(data.get(key), list) and len(data[key]) > MAX_ROWS:
        rows = data[key]
        return {**data, key: rows[:MAX_ROWS], "truncated": True, "rows_returned": MAX_ROWS, "rows_fetched": len(rows)}
    if isinstance(data, dict):
        return data
    return {"result": data}


# --- tool table -------------------------------------------------------------------

_STR = {"type": "string"}
_SOURCE = {"type": "string", "enum": list(SOURCES), "description": "one surface only"}

TOOLS = [
    {
        "name": "search_operations",
        "description": "Find Cin7 Core endpoints in the map by words (e.g. 'supplier list', 'mrp supply'). Public API "
                       "operations rank first, reads before writes. No credentials needed.",
        "inputSchema": {"type": "object", "properties": {
            "query": {**_STR, "description": "words that must all appear in the id, path, title or calling page"},
            "source": _SOURCE,
            "reads_only": {"type": "boolean", "description": "only operations the map marks as reads"},
            "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT, "default": 20},
        }},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "describe_operation",
        "description": "Parameters, request and response shapes, read/write marking and calling pages for one "
                       "operation id (\"<group> <name>\", from search_operations). No credentials needed.",
        "inputSchema": {"type": "object", "properties": {"id": _STR}, "required": ["id"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "page_operations",
        "description": "The internal endpoints one Cin7 web page calls, e.g. /MRPList or /Sale. No credentials needed.",
        "inputSchema": {"type": "object", "properties": {"page": _STR}, "required": ["page"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "list_groups",
        "description": "Operation groups with read and write counts. No credentials needed.",
        "inputSchema": {"type": "object", "properties": {"source": _SOURCE}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "auth_status",
        "description": "Whether the Cin7 API credentials are set, and where from. Never shows values.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "read",
        "description": "Send one Cin7 public API v2 GET with the user's API key and return the JSON (at most "
                       f"{MAX_ROWS} rows). Only public GET operations; writes and internal endpoints are refused.",
        "inputSchema": {"type": "object", "properties": {
            "id": {**_STR, "description": "operation id, e.g. \"product list\""},
            "params": {"type": "object", "description": "query parameters by their API name, e.g. {\"Name\": \"bolt\"}"},
            "all_pages": {"type": "boolean", "description": "walk the pages of a paged list", "default": False},
            "max_pages": {"type": "integer", "minimum": 1, "maximum": MAX_PAGES, "default": 3},
        }, "required": ["id"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
]


# --- JSON-RPC ---------------------------------------------------------------------


class Server:
    """Dispatches MCP requests. Kept free of I/O so tests can call `handle` directly."""

    def __init__(self, reader: Reader | None = None):
        self.reader = reader or Reader()
        self.handlers: dict[str, Callable[..., dict]] = {
            "search_operations": search_operations,
            "describe_operation": describe_operation,
            "page_operations": page_operations,
            "list_groups": list_groups,
            "auth_status": auth_status,
            "read": self.reader.read,
        }

    def handle(self, msg: dict) -> dict | None:
        """One incoming message -> the response, or None for a notification."""
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return None
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                result = {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cin7-core", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call(msg.get("params") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        except Exception as exc:  # noqa: BLE001 - never let one request kill the server
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        fn = self.handlers.get(name)
        if fn is None:
            return _text({"error": f"unknown tool {name!r}"}, error=True)
        try:
            return _text(fn(**(params.get("arguments") or {})))
        except TypeError as exc:
            return _text({"error": f"bad arguments for {name}: {exc}"}, error=True)
        except (ToolError, ApiError, auth.AuthError, runner.NotRunnable, ValueError) as exc:
            return _text({"error": str(exc)}, error=True)


def _text(payload: dict, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1, default=str)}], "isError": error}


def serve(stdin=None, stdout=None) -> int:
    """Read JSON-RPC lines until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server()
    print(f"cin7 mcp {__version__}: ready (reads only)", file=sys.stderr)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, default=str) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    """`cin7-mcp` entry point."""
    return serve()
