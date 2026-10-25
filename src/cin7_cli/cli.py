"""`cin7` entry point. Commands are generated from spec.json at start-up.

    cin7 <group> <operation> [--flags]       one public API v2 operation
    cin7 ops [filter]                        list every operation
    cin7 describe <group> <operation>        params, request and response shape
    cin7 raw <METHOD> <path> [-q K=V ...]    any path, same safety rules
    cin7 webhook-events [event]              webhook payload shapes
    cin7 tui                                 read-only terminal UI
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import __version__, auth, output, paging, spec
from .client import ApiError, Client, Request, base_url, build_url
from .ratelimit import DEFAULT_PER_MIN, MAX_PER_MIN, RateLimiter
from .safety import WriteBlocked, check_write

EXIT_OK, EXIT_API, EXIT_USAGE, EXIT_AUTH = 0, 1, 2, 3
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _bool(v: str) -> bool:
    s = v.strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {v!r}")


def _guid(v: str) -> str:
    if not GUID_RE.match(v.strip()):
        raise argparse.ArgumentTypeError(f"expected a GUID like 00000000-0000-0000-0000-000000000000, got {v!r}")
    return v.strip()


def _kv(v: str) -> tuple[str, str]:
    if "=" not in v:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {v!r}")
    k, val = v.split("=", 1)
    return k, val


TYPES = {"int": int, "bool": _bool, "guid": _guid, "date": str, "string": str}
TYPE_HINT = {"int": "N", "bool": "true|false", "guid": "GUID", "date": "DATE", "string": "TEXT"}


def _common(p: argparse.ArgumentParser, write: bool) -> None:
    g = p.add_argument_group("output and pacing")
    g.add_argument("-o", "--output", choices=["json", "table", "csv"], default="json", help="output format")
    g.add_argument("--fields", help="comma-separated columns to keep (table/csv/json rows)")
    g.add_argument(
        "--rate",
        type=float,
        default=float(os.environ.get("CIN7_RATE_PER_MIN", DEFAULT_PER_MIN)),
        help=f"max calls per minute, shared by every cin7 process (default {DEFAULT_PER_MIN}, cap {MAX_PER_MIN})",
    )
    g.add_argument("--timeout", type=float, default=60, help="seconds per HTTP call")
    g.add_argument("--dry-run", action="store_true", help="print the request and exit without calling Cin7")
    g.add_argument("-v", "--verbose", action="store_true", help="log each call to stderr")
    g.add_argument("-q", "--param", type=_kv, action="append", default=[], metavar="KEY=VALUE", help="extra query parameter")
    if write:
        w = p.add_argument_group("write (off by default)")
        w.add_argument("--allow-write", action="store_true", help="permit this write after interactive confirmation")
        w.add_argument("--body", help="JSON request body: inline JSON, @file.json, or - for stdin")


def _op_parser(sub, op: spec.Operation) -> None:
    kind = "WRITE" if op.is_write else "read"
    help_text = f"[{kind}] {op.method} {op.path}  ({op.resource}: {op.title})"
    p = sub.add_parser(op.name, aliases=list(op.aliases), help=help_text, description=help_text)
    p.set_defaults(op=op)
    g = p.add_argument_group("query parameters")
    for prm in op.params:
        g.add_argument(
            f"--{prm.flag}",
            dest=f"q_{prm.name}",
            type=TYPES.get(prm.type, str),
            metavar=TYPE_HINT.get(prm.type, "TEXT"),
            required=prm.required,
            help=f"{prm.name} ({prm.type})" + (" required" if prm.required else ""),
        )
    if op.paged:
        pg = p.add_argument_group("paging")
        pg.add_argument("--all-pages", action="store_true", help=f"follow Page until every {op.list_key} row is in")
        pg.add_argument("--max-pages", type=int, help="stop --all-pages after this many pages")
    _common(p, op.is_write)


def build_parser(only_group: str | None = "*") -> argparse.ArgumentParser:
    """One subcommand group per API group plus the helpers.

    `only_group` limits which groups get their operations built ("*" = all,
    None = none); the rest are listed but empty, which keeps start-up quick.
    """
    parser = argparse.ArgumentParser(
        prog="cin7",
        description="Cin7 Core (DEAR Inventory) public API v2. Read-only unless --allow-write and a typed confirmation.",
    )
    parser.add_argument("--version", action="version", version=f"cin7-core-cli {__version__}")
    top = parser.add_subparsers(dest="command", metavar="<group>")

    ops = top.add_parser("ops", help="list operations (filter by text)")
    ops.add_argument("filter", nargs="?", default="")
    ops.add_argument("--reads", action="store_true", help="GET operations only")
    ops.add_argument("-o", "--output", choices=["json", "table", "csv"], default="table")

    desc = top.add_parser("describe", help="show an operation's params and body shapes")
    desc.add_argument("group")
    desc.add_argument("operation")

    raw = top.add_parser("raw", help="call any path (same read-only rules)")
    raw.add_argument("method", type=str.upper, choices=["GET", "POST", "PUT", "DELETE"])
    raw.add_argument("path", help="path under the API root, e.g. /me")
    _common(raw, write=True)

    tui = top.add_parser("tui", help="browse and run read operations in a terminal UI (needs the tui extra)")
    tui.add_argument("--rate", type=float, default=float(os.environ.get("CIN7_RATE_PER_MIN", DEFAULT_PER_MIN)))

    wh = top.add_parser("webhook-events", help="webhook payload shapes from the API Blueprint")
    wh.add_argument("event", nargs="?", help="e.g. Sale/Created")

    for group, ops_ in spec.groups().items():
        title = ops_[0].group_title
        gp = top.add_parser(group, help=f"{title} ({len(ops_)} operations)", description=f"Cin7 {title} operations")
        sub = gp.add_subparsers(dest="operation", metavar="<operation>")
        sub.required = True
        if only_group in ("*", group):
            for op in ops_:
                _op_parser(sub, op)
    return parser


def _read_body(arg: str | None) -> object:
    if arg is None:
        return None
    if arg == "-":
        text = sys.stdin.read()
    elif arg.startswith("@"):
        with open(arg[1:]) as fh:
            text = fh.read()
    else:
        text = arg
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"cin7: --body is not valid JSON: {exc}") from None


def _query(args: argparse.Namespace, op: spec.Operation | None) -> dict:
    q: dict[str, object] = {}
    if op is not None:
        for prm in op.params:
            v = getattr(args, f"q_{prm.name}", None)
            if v is not None:
                q[prm.name] = v
    for k, v in args.param:
        q[k] = v
    return q


def _cmd_ops(args) -> int:
    rows = []
    for o in spec.operations():
        if args.reads and o.is_write:
            continue
        line = f"{o.group} {o.name} {o.method} {o.path} {o.resource} {o.title}".lower()
        if args.filter.lower() in line:
            rows.append(
                {
                    "command": f"cin7 {o.full_name}",
                    "method": o.method,
                    "path": o.path,
                    "paged": "yes" if o.paged else "",
                    "aliases": ",".join(o.aliases),
                }
            )
    print(output.render(rows, args.output))
    return EXIT_OK


def _cmd_describe(args) -> int:
    op = spec.find(args.group, args.operation)
    if op is None:
        print(f"cin7: no operation {args.group} {args.operation} (try `cin7 ops {args.group}`)", file=sys.stderr)
        return EXIT_USAGE
    print(
        json.dumps(
            {
                "command": f"cin7 {op.full_name}",
                "aliases": list(op.aliases),
                "write": op.is_write,
                "method": op.method,
                "path": op.path,
                "resource": op.resource,
                "title": op.title,
                "params": [{"flag": f"--{p.flag}", "name": p.name, "type": p.type, "required": p.required} for p in op.params],
                "paged": op.paged,
                "list_key": op.list_key,
                "request": op.request,
                "response": op.response,
            },
            indent=2,
        )
    )
    return EXIT_OK


def _cmd_webhooks(args) -> int:
    events = spec.load()["webhooks"]
    if args.event:
        if args.event not in events:
            print(f"cin7: unknown event {args.event}", file=sys.stderr)
            return EXIT_USAGE
        print(json.dumps(events[args.event], indent=2))
    else:
        print("\n".join(events))
    return EXIT_OK


def _run(args, op: spec.Operation, method: str, path: str) -> int:
    query = _query(args, op if args.command != "raw" else None)
    body = _read_body(getattr(args, "body", None))
    if body is not None and not op.is_write:
        print("cin7: --body is only for writes", file=sys.stderr)
        return EXIT_USAGE
    root = base_url()
    req = Request(method, build_url(path, query, root), body)
    if args.dry_run:
        print(req.describe())
        if op.is_write:
            print("(write: would need --allow-write and a typed confirmation)", file=sys.stderr)
        return EXIT_OK
    try:
        check_write(op, req, getattr(args, "allow_write", False), root)
    except WriteBlocked as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        creds = auth.load()
    except auth.AuthError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_AUTH
    client = Client(creds, RateLimiter(args.rate), timeout=args.timeout, verbose=args.verbose)
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    try:
        if getattr(args, "all_pages", False):
            limit = int(query.get("Limit") or paging.DEFAULT_LIMIT)
            start = int(query.get("Page") or 1)

            def fetch(page: int, lim: int) -> object:
                q = {**query, "Page": page, "Limit": lim}
                return client.send(Request("GET", build_url(path, q, root)))

            data = paging.fetch_all(fetch, op.list_key, limit=limit, start_page=start, max_pages=args.max_pages)
        else:
            data = client.send(req)
    except ApiError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        if exc.body is not None and args.verbose:
            print(creds.redact(json.dumps(exc.body, indent=2, default=str)), file=sys.stderr)
        return EXIT_API
    print(output.render(data, args.output, op.list_key, fields))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Parse argv and run. Returns the process exit code."""
    argv = sys.argv[1:] if argv is None else argv
    first = next((a for a in argv if not a.startswith("-")), None)
    parser = build_parser(first if first in spec.groups() else None)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    if args.command == "ops":
        return _cmd_ops(args)
    if args.command == "describe":
        return _cmd_describe(args)
    if args.command == "tui":
        try:
            from . import tui
        except ImportError:
            print("cin7: the TUI needs Textual: pip install 'cin7-core-cli[tui]'", file=sys.stderr)
            return EXIT_USAGE
        return tui.run(args.rate)
    if args.command == "webhook-events":
        return _cmd_webhooks(args)
    if args.command == "raw":
        path = args.path if args.path.startswith("/") else "/" + args.path
        op = spec.Operation(
            group="raw", group_title="Raw", name=args.method.lower(), resource=path, title=args.method,
            method=args.method, path=path, params=(),
        )
        return _run(args, op, args.method, path)
    op: spec.Operation = args.op
    return _run(args, op, op.method, op.path)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
