"""`cin7` entry point. Commands are generated from spec.json at start-up.

    cin7 <group> <operation> [--flags]       one public API v2 operation
    cin7 ops [filter]                        list every operation
    cin7 describe <group> <operation>        params, request and response shape
    cin7 raw <METHOD> <path> [-q K=V ...]    any path, same safety rules
    cin7 webhook-events [event]              webhook payload shapes
    cin7 tui                                 read-only terminal UI
    cin7 auth setup|status|test              credentials (op:// references)
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from . import __version__, auth, output, printing, resolve, runner, spec
from .client import ApiError, Client, Request, base_url, build_url
from .ratelimit import DEFAULT_PER_MIN, MAX_PER_MIN, RateLimiter
from .safety import WriteBlocked, check_print, check_write

EXIT_OK, EXIT_API, EXIT_USAGE, EXIT_AUTH = 0, 1, 2, 3
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _bool(v: str) -> bool:
    s = v.strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {v!r}")


def _kv(v: str) -> tuple[str, str]:
    if "=" not in v:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {v!r}")
    k, val = v.split("=", 1)
    return k, val


TYPES = {"int": int, "bool": _bool, "guid": str, "date": str, "string": str}  # guid: text is looked up in _run
TYPE_HINT = {"int": "N", "bool": "true|false", "guid": "GUID", "date": "DATE", "string": "TEXT"}


def _common(p: argparse.ArgumentParser, write: bool, post_read: bool = False) -> None:
    g = p.add_argument_group("output and pacing")
    g.add_argument("-o", "--output", choices=["json", "table", "csv"], default="json", help="output format")
    g.add_argument("--fields", help="comma-separated columns to keep (table/csv/json rows)")
    g.add_argument(
        "--rate",
        type=float,
        default=float(auth.setting("CIN7_RATE_PER_MIN", str(DEFAULT_PER_MIN))),
        help=f"max calls per minute, shared by every cin7 process (default {DEFAULT_PER_MIN}, cap {MAX_PER_MIN})",
    )
    g.add_argument("--timeout", type=float, default=60, help="seconds per HTTP call")
    g.add_argument("--dry-run", action="store_true", help="print the request and exit without calling Cin7")
    g.add_argument("-v", "--verbose", action="store_true", help="log each call to stderr")
    g.add_argument("-q", "--param", type=_kv, action="append", default=[], metavar="KEY=VALUE", help="extra query parameter")
    p.add_argument("--save-dir", default=".", help="folder for file downloads (web download/export endpoints)")
    if post_read:
        p.add_argument("--body", help="query fields for this POST read as JSON, sent form-encoded like the web app (e.g. {\"MrpID\": \"…\", \"LoadProducts\": true}): inline, @file.json, or -")
    if write:
        w = p.add_argument_group("write (off by default)")
        w.add_argument("--allow-write", action="store_true", help="permit this write after interactive confirmation")
        w.add_argument("--body", help="JSON request body: inline JSON, @file.json, or - for stdin")


def _op_parser(sub, op: spec.Operation) -> None:
    if op.is_public:
        kind = "WRITE" if op.is_write else "read"
        help_text = f"[{kind}] {op.method} {op.path}  ({op.resource}: {op.title})"
    elif op.runnable:
        kind = "internal read" + (" via POST" if op.read_via_post else "")
        help_text = f"[{kind}] {op.method} {op.path}  ({op.title}; browser session)"
    else:
        help_text = f"[internal · not sendable] {op.methods_raw or '?'} {op.path}  ({op.title})"
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
            help=f"{prm.name} ({prm.type})" + (" required" if prm.required else "")
            + (f" · any of (comma-separate for several): {', '.join(prm.choices)}" if prm.choices and prm.type != "bool" else "")
            + (f" · or look up by {r.hint}" if (r := resolve.resolver_for(op, prm)) else ""),
        )
    if op.paged:
        pg = p.add_argument_group("paging")
        pg.add_argument("--all-pages", action="store_true", help=f"follow Page until every {op.list_key} row is in")
        pg.add_argument("--max-pages", type=int, help="stop --all-pages after this many pages")
    _common(p, write=op.is_public and op.is_write, post_read=op.read_via_post)


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
    ops.add_argument("--reads", action="store_true", help="sendable reads only")
    ops.add_argument("--source", choices=["public", "web", "wms", "mes", "report-service"], help="one surface only")
    ops.add_argument("-o", "--output", choices=["json", "table", "csv"], default="table")

    desc = top.add_parser("describe", help="show an operation's params and body shapes")
    desc.add_argument("group")
    desc.add_argument("operation")

    raw = top.add_parser("raw", help="call any path (same read-only rules)")
    raw.add_argument("method", type=str.upper, choices=["GET", "POST", "PUT", "DELETE"])
    raw.add_argument("path", help="path under the API root, e.g. /me")
    _common(raw, write=True)

    tui = top.add_parser("tui", help="browse and run read operations in a terminal UI (needs the tui extra)")
    tui.add_argument("--rate", type=float, default=float(auth.setting("CIN7_RATE_PER_MIN", str(DEFAULT_PER_MIN))))

    au = top.add_parser("auth", help="set up or check credentials (op:// references in a config file)")
    au_sub = au.add_subparsers(dest="auth_command", metavar="<setup|status|test>")
    au_sub.required = True
    st = au_sub.add_parser("setup", help="save op:// references to the config file (never key values)")
    st.add_argument("--account-ref", help="op:// reference to the API account id")
    st.add_argument("--key-ref", help="op:// reference to the API application key")
    st.add_argument("--cookie-ref", help="op:// reference to a signed-in browser Cookie header (internal endpoints)")
    st.add_argument("--allow-writes", action="store_true", help="leave CIN7_CLI_READ_ONLY off (default: writes off)")
    au_sub.add_parser("status", help="where credentials come from (no values)")
    au_sub.add_parser("test", help="one GET /me call; prints the company name")

    pr = top.add_parser("print", help="where printing lives in Cin7, and gated printing")
    pr_sub = pr.add_subparsers(dest="print_command", metavar="<where|templates|send>")
    pr_sub.required = True
    pw = pr_sub.add_parser("where", help="every print action and the Cin7 screens it is on (no network)")
    pw.add_argument("filter", nargs="?", default="")
    pw.add_argument("-o", "--output", choices=["json", "table", "csv"], default="table")
    pt = pr_sub.add_parser("templates", help="document templates (public API, API key)")
    pt.add_argument("--type", help="Type filter, e.g. Sale")
    pt.add_argument("--name", help="Name filter")
    pt.add_argument("-o", "--output", choices=["json", "table", "csv"], default="table")
    ps = pr_sub.add_parser("send", help="print documents through your Cin7 session (typed confirmation)")
    ps.add_argument("endpoint", help="e.g. /SaleList/Print (see `cin7 print where`)")
    ps.add_argument("--ids", required=True, help="comma-separated record ids (a list print takes many; others one)")
    ps.add_argument("--template", help="template id (cin7 print templates)")
    ps.add_argument("-q", "--param", type=_kv, action="append", default=[], metavar="KEY=VALUE", help="extra form field, e.g. SortBy=…")
    ps.add_argument("--allow-print", action="store_true", help="permit it after a typed confirmation")
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--save-dir", default=".")
    ps.add_argument("--rate", type=float, default=float(auth.setting("CIN7_RATE_PER_MIN", str(DEFAULT_PER_MIN))))

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
        if args.reads and (o.is_write or not o.runnable):
            continue
        if args.source and o.source != args.source:
            continue
        line = f"{o.group} {o.name} {o.method} {o.path} {o.resource} {o.title}".lower()
        if args.filter.lower() in line:
            rows.append(
                {
                    "command": f"cin7 {o.full_name}",
                    "source": o.source,
                    "sendable": "yes" if (o.is_public or o.runnable) else "",
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
                "source": op.source,
                "sendable": op.is_public or op.runnable,
                "effect": op.effect or None,
                "methods_in_web_app": op.methods_raw or None,
                "body_keys": list(op.body_keys) or None,
                "web_pages": list(op.pages) or None,
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


def _cmd_auth(args) -> int:
    try:
        if args.auth_command == "setup":
            values = auth.read_config()
            new = {auth.ACCOUNT_ENV: args.account_ref, auth.KEY_ENV: args.key_ref, auth.COOKIE_ENV: args.cookie_ref}
            if not any(new.values()):
                print("cin7: pass --account-ref/--key-ref and/or --cookie-ref", file=sys.stderr)
                return EXIT_USAGE
            values.update({k: v for k, v in new.items() if v})
            if args.allow_writes:
                values.pop("CIN7_CLI_READ_ONLY", None)
            else:
                values["CIN7_CLI_READ_ONLY"] = "1"
            path = auth.write_config(values)
            print(f"saved {path} (references only, mode 600)" + ("" if args.allow_writes else "; writes are off"))
            return EXIT_OK
        if args.auth_command == "status":
            src = auth.source()
            print(f"config file: {auth.config_path()}" + ("" if auth.config_path().exists() else " (not present)"))
            for k, where in src.items():
                print(f"{k}: {where}")
            ro = (auth.setting("CIN7_CLI_READ_ONLY") or "").strip() not in ("", "0", "false", "no")
            print(f"writes: {'off (CIN7_CLI_READ_ONLY)' if ro else 'gated by --allow-write + typed confirmation'}")
            print(f"API root: {base_url()}")
            api_ok = src[auth.ACCOUNT_ENV] != "missing" and src[auth.KEY_ENV] != "missing"
            return EXIT_OK if api_ok else EXIT_AUTH
        creds = auth.load()
        client = Client(creds, RateLimiter(float(auth.setting("CIN7_RATE_PER_MIN", str(DEFAULT_PER_MIN)))))
        me = client.send(Request("GET", build_url("/me", {})))
        company = me.get("Company") if isinstance(me, dict) else None
        print(f"ok: connected to {company or '(no company name in /me)'}")
        return EXIT_OK
    except auth.AuthError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except ApiError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_API


def _cmd_print(args) -> int:
    if args.print_command == "where":
        rows = [
            {
                "endpoint": p.path, "method": p.method, "takes": ("many ids" if p.bulk else "one id") if p.sendable else "?", "template": "yes" if p.template_param else "",
                "sendable": "yes" if p.sendable else "no (not mapped)", "on Cin7 screens": p.where,
            }
            for p in printing.endpoints()
            if args.filter.lower() in f"{p.path} {p.where}".lower()
        ]
        print(output.render(rows, args.output))
        return EXIT_OK
    if args.print_command == "templates":
        op = spec.find("templates", "list")
        query = {k: v for k, v in (("Type", args.type), ("Name", args.name)) if v}
        try:
            client = runner.make_client(op, DEFAULT_PER_MIN)
            data = runner.fetch(client, op, query, all_pages=True, progress=False)
        except auth.AuthError as exc:
            print(f"cin7: {exc}", file=sys.stderr)
            return EXIT_AUTH
        except ApiError as exc:
            print(f"cin7: {exc}", file=sys.stderr)
            return EXIT_API
        print(output.render(data, args.output, op.list_key, ["TemplateID", "Type", "Name"]))
        return EXIT_OK
    pe = printing.find(args.endpoint)
    if pe is None:
        print(f"cin7: unknown print endpoint {args.endpoint} (cin7 print where)", file=sys.stderr)
        return EXIT_USAGE
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    try:
        req = printing.build_request(pe, ids, args.template, dict(args.param))
    except ValueError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.dry_run:
        print(req.describe())
        return EXIT_OK
    try:
        check_print(len(ids), req, args.allow_print, runner.web_base_url())
        client = Client(auth.load_web(), RateLimiter(args.rate))
        result = client.send(req, raw=True)
    except WriteBlocked as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except auth.AuthError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except ApiError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_API
    fake = spec.Operation(group="print", group_title="Print", name=pe.path.strip("/").replace("/", "-").lower(), resource=pe.path,
                          title="print", method=pe.method, path=pe.path, params=())
    print(f"saved {runner.save_file(result, fake, args.save_dir)}")
    return EXIT_OK


def _resolve_ids(args, op: spec.Operation, query: dict) -> int | None:
    """Turn text given for an id parameter (a customer ref, SKU, name) into its GUID. None = all good."""
    for prm in op.params:
        value = query.get(prm.name)
        try:
            r = resolve.lookup_for(op, prm, value)
        except ValueError as exc:
            print(f"cin7: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if r is None:
            continue
        try:
            found = resolve.search(r, str(value), lambda lop: runner.make_client(lop, args.rate, args.timeout, args.verbose))
        except (auth.AuthError, ApiError) as exc:
            print(f"cin7: could not look up {value!r}: {exc}", file=sys.stderr)
            return EXIT_AUTH if isinstance(exc, auth.AuthError) else EXIT_API
        if not found:
            print(f"cin7: nothing matches {value!r} ({r.hint})", file=sys.stderr)
            return EXIT_USAGE
        exact = [f for f in found if str(value).lower() in (f[0].lower().split(" · "))]
        if len(found) > 1 and len(exact) == 1:
            found = exact
        if len(found) > 1:
            print(f"cin7: {len(found)} records match {value!r}; pass the id of one:", file=sys.stderr)
            for label, rid in found[:10]:
                print(f"  {rid}  {label}", file=sys.stderr)
            return EXIT_USAGE
        print(f"cin7: {value!r} → {found[0][0]}", file=sys.stderr)
        query[prm.name] = found[0][1]
    return None


def _run(args, op: spec.Operation, method: str, path: str) -> int:
    query = _query(args, op if args.command != "raw" else None)
    for prm in op.params:  # enumerated filters take several values: "NOT SHIPPED,PARTIALLY SHIPPED"
        v = query.get(prm.name)
        if prm.choices and prm.type != "bool" and isinstance(v, str) and "," in v:
            query[prm.name] = [x.strip() for x in v.split(",") if x.strip()]
    if not args.dry_run:
        bad = _resolve_ids(args, op, query)
        if bad is not None:
            return bad
    body = _read_body(getattr(args, "body", None))
    if body is not None and not (op.is_write or op.read_via_post):
        print("cin7: --body is only for writes and POST reads", file=sys.stderr)
        return EXIT_USAGE
    try:
        runner.check_runnable(op)
    except runner.NotRunnable as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        reqs = [runner.request_for(op, q, body) for q in runner.expand(query)]
    except ValueError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_USAGE
    req = reqs[0]
    if args.dry_run:
        print("\n".join(r.describe() for r in reqs))
        if op.is_write:
            print("(write: would need --allow-write and a typed confirmation)", file=sys.stderr)
        return EXIT_OK
    try:
        check_write(op, req, getattr(args, "allow_write", False), runner.root_for(op))
    except WriteBlocked as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        client = runner.make_client(op, args.rate, timeout=args.timeout, verbose=args.verbose)
    except auth.AuthError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        return EXIT_AUTH
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    try:
        data = runner.fetch_many(
            client, op, query, body, all_pages=getattr(args, "all_pages", False), max_pages=getattr(args, "max_pages", None)
        )
    except ApiError as exc:
        print(f"cin7: {exc}", file=sys.stderr)
        if exc.body is not None and args.verbose:
            print(client.creds.redact(json.dumps(exc.body, indent=2, default=str)), file=sys.stderr)
        return EXIT_API
    if op.file:
        print(f"saved {runner.save_file(data, op, getattr(args, 'save_dir', None) or '.')}")
        return EXIT_OK
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
    if args.command == "auth":
        return _cmd_auth(args)
    if args.command == "print":
        return _cmd_print(args)
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
