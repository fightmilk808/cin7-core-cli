from __future__ import annotations

import io
import json

from cin7_cli import mcp

from .conftest import ACCOUNT, KEY


def call(server, name, **arguments):
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
    result = reply["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


# --- protocol -----------------------------------------------------------------------


def test_initialize_echoes_a_supported_version():
    s = mcp.Server()
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert r["result"]["capabilities"] == {"tools": {}}
    r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert r["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_notifications_get_no_reply_and_unknown_methods_error():
    s = mcp.Server()
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601


def test_tools_list_names_and_all_read_only():
    tools = mcp.Server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "search_operations", "describe_operation", "page_operations", "list_groups", "auth_status", "read"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


def test_serve_speaks_newline_json():
    lines = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]) + "\nnot json\n"
    out = io.StringIO()
    assert mcp.serve(io.StringIO(lines), out) == 0
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700


# --- map tools ----------------------------------------------------------------------


def test_search_ranks_public_reads_first():
    err, out = call(mcp.Server(), "search_operations", query="supplier")
    assert not err and out["matches"] > 1
    first = out["operations"][0]
    assert first["source"] == "public" and first["kind"] == "read"


def test_search_reads_only_and_source_filter():
    _, out = call(mcp.Server(), "search_operations", query="mrp", source="web", reads_only=True, limit=50)
    assert out["operations"] and all(o["kind"] == "read" and o["source"] == "web" for o in out["operations"])
    err, out = call(mcp.Server(), "search_operations", query="x", source="mars")
    assert err and "source must be" in out["error"]


def test_describe_operation_and_unknown_id():
    err, out = call(mcp.Server(), "describe_operation", id="sale get")
    assert not err and out["path"] == "/sale"
    assert any(p["name"] == "ID" and p["required"] for p in out["params"])
    err, out = call(mcp.Server(), "describe_operation", id="no such")
    assert err and "search_operations" in out["error"]


def test_page_operations_lists_what_a_page_calls():
    err, out = call(mcp.Server(), "page_operations", page="MRPList")
    assert not err and "web-mrp mrp-v-mrp" in {o["id"] for o in out["operations"]}


def test_list_groups_counts():
    _, out = call(mcp.Server(), "list_groups", source="public")
    product = next(g for g in out["groups"] if g["group"] == "product")
    assert product["reads"] >= 1 and product["writes"] >= 1


def test_auth_status_never_shows_values():
    err, out = call(mcp.Server(), "auth_status")
    text = json.dumps(out)
    assert not err and out["ready"] and ACCOUNT not in text and KEY not in text


def test_bad_arguments_are_a_tool_error():
    err, out = call(mcp.Server(), "describe_operation", nope=1)
    assert err and "bad arguments" in out["error"]


# --- read ---------------------------------------------------------------------------


def test_read_sends_one_get_with_the_api_key(mock):
    mock.routes[("GET", "/supplier")] = (200, {"Total": 1, "Page": 1, "SupplierList": [{"Name": "Acme"}]}, {})
    err, out = call(mcp.Server(), "read", id="supplier list", params={"name": "Acme"})
    assert not err and out["SupplierList"] == [{"Name": "Acme"}]
    req = mock.requests[-1]
    assert req["method"] == "GET" and req["query"]["Name"] == "Acme"
    assert req["headers"]["api-auth-applicationkey"] == KEY


def test_read_refuses_writes_and_internal_without_sending(mock):
    s = mcp.Server()
    err, out = call(s, "read", id="sale create")
    assert err and "write" in out["error"]
    err, out = call(s, "read", id="web-mrp mrp-v-mrp")
    assert err and "never sent" in out["error"]
    assert mock.requests == []


def test_read_checks_params(mock):
    s = mcp.Server()
    err, out = call(s, "read", id="sale get")
    assert err and "needs ID" in out["error"]
    err, out = call(s, "read", id="supplier list", params={"Colour": "red"})
    assert err and "no parameter" in out["error"]
    assert mock.requests == []


def test_read_caps_rows(mock):
    rows = [{"Name": f"S{i}"} for i in range(mcp.MAX_ROWS + 5)]
    mock.routes[("GET", "/supplier")] = (200, {"Total": len(rows), "Page": 1, "SupplierList": rows}, {})
    _, out = call(mcp.Server(), "read", id="supplier list")
    assert out["truncated"] and len(out["SupplierList"]) == mcp.MAX_ROWS and out["rows_fetched"] == len(rows)


def test_read_api_error_is_a_tool_error_and_redacted(mock):
    mock.routes[("GET", "/supplier")] = (403, {"Message": f"bad key {KEY}"}, {})
    err, out = call(mcp.Server(), "read", id="supplier list")
    assert err and "403" in out["error"] and KEY not in out["error"]


def test_credentials_resolve_once(mock):
    made = []

    def factory():
        from cin7_cli import auth
        from cin7_cli.client import Client
        from cin7_cli.ratelimit import RateLimiter

        made.append(1)
        return Client(auth.load(), RateLimiter(60))

    mock.routes[("GET", "/me")] = lambda req: (200, {"Company": "Test"}, {})
    s = mcp.Server(mcp.Reader(factory))
    call(s, "read", id="me get")
    call(s, "read", id="me get")
    assert len(made) == 1 and len(mock.requests) == 2
