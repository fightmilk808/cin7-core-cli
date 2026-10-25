from __future__ import annotations

import csv
import io
import json
import os
import stat

import pytest

from cin7_cli import cli, safety, spec
from cin7_cli.ratelimit import RateLimiter

from .conftest import ACCOUNT, KEY

SALE_ID = "11111111-2222-3333-4444-555555555555"




def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --- the generated command set ------------------------------------------------


def test_spec_covers_every_operation():
    ops = spec.operations()
    public = [o for o in ops if o.is_public]
    assert len(public) == 295
    assert len({o.group for o in public}) == 32
    assert sum(o.method == "GET" for o in public) == 102
    assert sum(o.source == "web" for o in ops) == 2177
    names = [(o.group, o.name) for o in ops]
    assert len(names) == len(set(names)), "subcommand names must be unique per group"


def test_every_operation_parses(capsys):
    parser = cli.build_parser()
    for op in spec.operations():
        argv = [op.group, op.name]
        for p in op.params:
            if p.required:
                argv += [f"--{p.flag}", {"int": "1", "bool": "true", "guid": SALE_ID}.get(p.type, "x")]
        args = parser.parse_args(argv)
        assert args.op.path == op.path


@pytest.mark.parametrize(
    "group,name,method,path",
    [
        ("sale", "get", "GET", "/sale"),
        ("sale", "list", "GET", "/saleList"),
        ("product", "list", "GET", "/product"),
        ("product", "get", "GET", "/product"),  # alias of list
        ("webhooks", "list", "GET", "/webhooks"),
        ("me", "get", "GET", "/me"),
        ("production", "order-list", "GET", "/production/orderList"),
        ("production", "work-centers-list", "GET", "/production/workcenters"),
        ("purchase", "advanced-purchase-get", "GET", "/advanced-purchase"),
        ("production", "order-authorise", "POST", "/production/order/authorise"),
    ],
)
def test_known_commands(group, name, method, path):
    op = spec.find(group, name)
    assert op is not None and op.method == method and op.path == path


def test_flags_are_typed():
    op = spec.find("product", "list")
    by = {p.name: p for p in op.params}
    assert by["ModifiedSince"].flag == "modified-since"
    assert by["Page"].type == "int"
    assert by["IncludeBOM"].type == "bool"
    assert op.paged and op.list_key == "Products"


def test_bad_typed_flag_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["product", "list", "--page", "two"])
    assert e.value.code == 2


def test_ops_and_describe(capsys):
    code, out, _ = run(capsys, "ops", "saleList", "-o", "csv")
    assert code == 0 and "cin7 sale list" in out
    code, out, _ = run(capsys, "describe", "sale", "get")
    d = json.loads(out)
    assert d["path"] == "/sale" and not d["write"] and d["response"]["ID"]
    code, _, err = run(capsys, "describe", "sale", "nope")
    assert code == 2


def test_webhook_events(capsys):
    code, out, _ = run(capsys, "webhook-events")
    assert "Sale/Created" in out.splitlines()
    code, out, _ = run(capsys, "webhook-events", "Sale/Created")
    assert "SaleID" in json.loads(out)


# --- reads against the mock -----------------------------------------------------


def test_get_sends_auth_headers_and_query(mock, capsys):
    mock.routes[("GET", "/sale")] = (200, {"ID": SALE_ID, "Customer": "Acme", "CustomerReference": "#25147"}, {})
    code, out, err = run(capsys, "sale", "get", "--id", SALE_ID)
    assert code == 0
    assert json.loads(out)["CustomerReference"] == "#25147"
    req = mock.requests[0]
    assert req["query"] == {"ID": SALE_ID}
    assert req["headers"]["api-auth-accountid"] == ACCOUNT
    assert req["headers"]["api-auth-applicationkey"] == KEY
    assert KEY not in out + err


def test_bool_flag_encoding(mock, capsys):
    mock.routes[("GET", "/product")] = (200, {"Total": 0, "Page": 1, "Products": []}, {})
    run(capsys, "product", "list", "--include-bom", "yes", "-q", "Sku=ABC")
    assert mock.requests[0]["query"] == {"IncludeBOM": "true", "Sku": "ABC"}


def test_all_pages(mock, capsys):
    total = 250

    def page(req):
        p, lim = int(req["query"]["Page"]), int(req["query"]["Limit"])
        rows = [{"ID": i, "SKU": f"S{i}"} for i in range((p - 1) * lim, min(p * lim, total))]
        return 200, {"Total": total, "Page": p, "Products": rows}, {}

    mock.routes[("GET", "/product")] = page
    code, out, err = run(capsys, "product", "list", "--modified-since", "2026-10-01", "--all-pages")
    assert code == 0
    data = json.loads(out)
    assert len(data["Products"]) == 250 and data["Pages"] == 3
    assert [r["query"]["Page"] for r in mock.requests] == ["1", "2", "3"]
    assert all(r["query"]["ModifiedSince"] == "2026-10-01" for r in mock.requests)


def test_all_pages_max_pages(mock, capsys):
    mock.routes[("GET", "/saleList")] = lambda r: (200, {"Total": 9999, "Page": 1, "SaleList": [{}] * 5}, {})
    code, out, _ = run(capsys, "sale", "list", "--limit", "5", "--all-pages", "--max-pages", "2")
    assert code == 0 and len(json.loads(out)["SaleList"]) == 10 and len(mock.requests) == 2


def test_table_and_csv_output(mock, capsys):
    body = {"Total": 2, "Page": 1, "SaleList": [
        {"SaleID": "a", "CustomerReference": "#1", "Lines": [1, 2]},
        {"SaleID": "b", "CustomerReference": "#2", "Lines": []},
    ]}
    mock.routes[("GET", "/saleList")] = [(200, body, {}), (200, body, {})]
    code, out, _ = run(capsys, "sale", "list", "-o", "csv", "--fields", "CustomerReference,SaleID,Lines")
    rows = list(csv.reader(io.StringIO(out)))
    assert rows == [["CustomerReference", "SaleID", "Lines"], ["#1", "a", "[2]"], ["#2", "b", "[0]"]]
    code, out, _ = run(capsys, "sale", "list", "-o", "table")
    assert out.splitlines()[0].split() == ["CustomerReference", "Lines", "SaleID"]


def test_429_backs_off_and_retries(mock, capsys):
    mock.routes[("GET", "/me")] = [(429, None, {"Retry-After": "2"}), (200, {"Company": "Example Co"}, {})]
    code, out, err = run(capsys, "me", "get")
    assert code == 0 and json.loads(out)["Company"] == "Example Co"
    assert len(mock.requests) == 2 and "HTTP 429" in err


def test_429_exhausted(mock, capsys):
    mock.routes[("GET", "/me")] = lambda r: (429, None, {})
    code, _, err = run(capsys, "me", "get")
    assert code == 1 and "rate limited" in err and len(mock.requests) == 5


def test_wrong_path_302_is_an_error(mock, capsys):
    code, _, err = run(capsys, "raw", "GET", "/salelist")
    assert code == 1 and "302" in err and "wrong path" in err


def test_400_missing_record(mock, capsys):
    mock.routes[("GET", "/sale")] = (400, [{"ErrorCode": 400, "Exception": "Sale not found"}], {})
    code, _, err = run(capsys, "sale", "get", "--id", SALE_ID)
    assert code == 1 and "Sale not found" in err


def test_403_message_never_echoes_key(mock, capsys):
    mock.routes[("GET", "/me")] = (403, [{"ErrorCode": 403, "Exception": f"bad key {KEY}"}], {})
    code, out, err = run(capsys, "me", "get", "-v")
    assert code == 1 and KEY not in out + err and "***" in err


def test_missing_credentials(monkeypatch, capsys):
    monkeypatch.delenv("CIN7_APP_KEY")
    code, _, err = run(capsys, "me", "get")
    assert code == 3 and "CIN7_APP_KEY" in err


FAKE_OP = """#!/bin/sh
# Fake 1Password CLI: `op inject` swaps {{ op://.../<field> }} for resolved-<field>.
[ "$1" = inject ] || exit 1
echo "call" >> "$FAKE_OP_LOG"
sed -E 's#\\{\\{ op://[^}]*/([^/} ]+) \\}\\}#resolved-\\1#g'
"""


@pytest.fixture
def fake_op(monkeypatch, tmp_path):
    fake = tmp_path / "bin" / "op"
    fake.parent.mkdir()
    fake.write_text(FAKE_OP)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "op.log"
    monkeypatch.setenv("FAKE_OP_LOG", str(log))
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    return log


def test_op_reference_resolution_one_approval(mock, monkeypatch, fake_op, capsys):
    monkeypatch.setenv("CIN7_ACCOUNT_ID", "op://Vault/Cin7 API/account")
    monkeypatch.setenv("CIN7_APP_KEY", "op://Vault/Cin7 API/key")
    mock.routes[("GET", "/me")] = (200, {}, {})
    assert run(capsys, "me", "get")[0] == 0
    h = mock.requests[0]["headers"]
    assert h["api-auth-accountid"] == "resolved-account" and h["api-auth-applicationkey"] == "resolved-key"
    assert fake_op.read_text().count("call") == 1, "both references in one op call"


def test_config_file_fallback_and_setup(mock, monkeypatch, fake_op, capsys):
    monkeypatch.delenv("CIN7_ACCOUNT_ID")
    monkeypatch.delenv("CIN7_APP_KEY")
    code, out, _ = run(capsys, "auth", "status")
    assert code == 3 and "missing" in out
    code, out, _ = run(capsys, "auth", "setup", "--account-ref", "op://V/I/username", "--key-ref", "op://V/I/password")
    assert code == 0 and "writes are off" in out
    from cin7_cli import auth

    path = auth.config_path()
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert "op://V/I/password" in path.read_text()
    code, out, _ = run(capsys, "auth", "status")
    assert code == 0 and "CIN7_APP_KEY: config" in out and "writes: off" in out
    mock.routes[("GET", "/me")] = (200, {"Company": "Sample Co"}, {})
    code, out, _ = run(capsys, "auth", "test")
    assert code == 0 and "Sample Co" in out
    assert mock.requests[0]["headers"]["api-auth-applicationkey"] == "resolved-password"
    # read-only from the config file blocks writes even with --allow-write
    monkeypatch.setattr(safety, "_ask", lambda prompt: "sale create")
    code, _, err = run(capsys, "sale", "create", "--body", "{}", "--allow-write")
    assert code == 2 and "CIN7_CLI_READ_ONLY" in err and len(mock.requests) == 1


def test_setup_refuses_raw_values(capsys):
    code, _, err = run(capsys, "auth", "setup", "--account-ref", "abc", "--key-ref", "op://V/I/p")
    assert code == 3 and "op:// reference" in err


def test_op_failure_is_reported(monkeypatch, tmp_path, capsys):
    fake = tmp_path / "bin" / "op"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'authorization prompt dismissed' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CIN7_APP_KEY", "op://V/I/password")
    code, _, err = run(capsys, "me", "get")
    assert code == 3 and "dismissed" in err


def test_dry_run_makes_no_call(mock, capsys):
    code, out, _ = run(capsys, "sale", "get", "--id", SALE_ID, "--dry-run")
    assert code == 0 and out.startswith("GET ") and mock.requests == []
    assert KEY not in out


# --- the write gate -----------------------------------------------------------------


def test_write_blocked_by_default(mock, capsys):
    code, _, err = run(capsys, "sale", "create", "--body", '{"Customer": "x"}')
    assert code == 2 and "read-only by default" in err
    assert mock.requests == []


def test_write_blocked_without_terminal(mock, monkeypatch, capsys):
    def no_tty(*a, **k):
        raise OSError("no tty")

    monkeypatch.setattr(safety, "open", no_tty, raising=False)
    code, _, err = run(capsys, "brand", "delete", "--id", SALE_ID, "--allow-write")
    assert code == 2 and "interactive terminal" in err and mock.requests == []


def test_write_wrong_confirmation(mock, monkeypatch, capsys):
    monkeypatch.setattr(safety, "_ask", lambda prompt: "yes")
    code, _, err = run(capsys, "sale", "create", "--body", "{}", "--allow-write")
    assert code == 2 and "did not match" in err and mock.requests == []


def test_write_confirmed_reaches_mock_only(mock, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(safety, "_ask", lambda prompt: prompts.append(prompt) or "sale create")
    mock.routes[("POST", "/sale")] = (200, {"ID": SALE_ID}, {})
    code, out, err = run(capsys, "sale", "create", "--body", '{"Customer": "Mock"}', "--allow-write")
    assert code == 0 and json.loads(out)["ID"] == SALE_ID
    assert mock.requests[0]["method"] == "POST" and mock.requests[0]["body"] == {"Customer": "Mock"}
    assert "`sale create`" in prompts[0]
    assert "WRITE to Cin7" in err and '"Customer": "Mock"' in err and KEY not in err


def test_read_only_env_beats_allow_write(mock, monkeypatch, capsys):
    monkeypatch.setenv("CIN7_CLI_READ_ONLY", "1")
    monkeypatch.setattr(safety, "_ask", lambda prompt: "sale create")
    code, _, err = run(capsys, "sale", "create", "--body", "{}", "--allow-write")
    assert code == 2 and "CIN7_CLI_READ_ONLY" in err and mock.requests == []


def test_raw_write_blocked_and_raw_read_works(mock, capsys):
    code, _, err = run(capsys, "raw", "DELETE", "/sale", "-q", f"ID={SALE_ID}")
    assert code == 2 and mock.requests == []
    mock.routes[("GET", "/me")] = (200, {"Company": "x"}, {})
    code, out, _ = run(capsys, "raw", "GET", "me")
    assert code == 0 and json.loads(out)["Company"] == "x"


def test_body_on_read_rejected(capsys):
    code, _, err = run(capsys, "raw", "GET", "/me", "--body", "{}")
    assert code == 2


def test_body_from_file(mock, monkeypatch, tmp_path, capsys):
    f = tmp_path / "b.json"
    f.write_text('{"Name": "Brand X"}')
    monkeypatch.setattr(safety, "_ask", lambda prompt: "brand create")
    mock.routes[("POST", "/ref/brand")] = (200, {"ID": "1"}, {})
    assert run(capsys, "brand", "create", "--body", f"@{f}", "--allow-write")[0] == 0
    assert mock.requests[0]["body"] == {"Name": "Brand X"}


# --- the rate limiter -----------------------------------------------------------------


def test_rate_limiter_spaces_calls(tmp_path):
    now = [1000.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    rl = RateLimiter(40, path=tmp_path / "lc", sleep=sleep, clock=lambda: now[0])
    rl.wait()
    rl.wait()
    rl.wait()
    assert slept == [pytest.approx(1.5), pytest.approx(1.5)]


def test_rate_limiter_is_shared_across_instances(tmp_path):
    now = [1000.0]
    slept = []
    clock = lambda: now[0]  # noqa: E731
    a = RateLimiter(60, path=tmp_path / "lc", sleep=slept.append, clock=clock)
    b = RateLimiter(60, path=tmp_path / "lc", sleep=slept.append, clock=clock)
    a.wait()
    b.wait()
    assert slept == [pytest.approx(1.0)]


def test_rate_limiter_caps_at_60(tmp_path):
    assert RateLimiter(500, path=tmp_path / "lc").interval == pytest.approx(1.0)
    with pytest.raises(ValueError):
        RateLimiter(0, path=tmp_path / "lc")


def test_penalise_pushes_next_call(tmp_path):
    now = [100.0]
    slept = []
    rl = RateLimiter(60, path=tmp_path / "lc", sleep=slept.append, clock=lambda: now[0])
    rl.penalise(10)
    rl.wait()
    assert slept == [pytest.approx(10)]


def test_spec_matches_rebuild_repo_when_present():
    """spec.json is generated; regenerate with scripts/build_spec.py if this fails."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    rebuild = root.parent / "cin7-core-rebuild"
    if not (rebuild / "data" / "public-api-shapes.json").exists():
        pytest.skip("cin7-core-rebuild checkout not beside this repo")
    mod_spec = importlib.util.spec_from_file_location("build_spec", root / "scripts" / "build_spec.py")
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    committed = json.loads((root / "src" / "cin7_cli" / "spec.json").read_text())
    assert mod.build(rebuild)["commands"] == committed["commands"]


def test_detail_record_with_inner_array_is_one_row(mock, capsys):
    """/me has RoundingTable[]; --fields must pick from the record, not the array (live bug 2026-10-24)."""
    me = {"Company": "Sample Co", "Currency": "USD", "TimeZone": "America/Denver", "LockDate": None,
          "RoundingTable": [{"RangeTo": 0.0, "RoundToNearest": 0.01}]}
    mock.routes[("GET", "/me")] = (200, me, {})
    code, out, _ = run(capsys, "me", "get", "-o", "csv", "--fields", "Company,Currency,TimeZone")
    assert code == 0 and out.splitlines() == ["Company,Currency,TimeZone", "Sample Co,USD,America/Denver"]


# --- internal (web app) endpoints ---------------------------------------------------


@pytest.fixture
def web(mock, monkeypatch):
    monkeypatch.setenv("CIN7_WEB_BASE_URL", mock.url.removesuffix("/ExternalApi/v2"))
    monkeypatch.setenv("CIN7_WEB_COOKIE", "ASP.NET_SessionId=sess12345678; .ASPXAUTH=auth87654321")
    return mock


def test_internal_read_sends_cookie_not_api_key(web, capsys):
    web.routes[("GET", "/api/MRP_Results")] = (200, {"root": [{"ID": 1}], "total": 1}, {})
    code, out, err = run(capsys, "web-mrp", "mrp-results", "--mrp-id", "m1")
    assert code == 0 and json.loads(out)["total"] == 1
    r = web.requests[0]
    assert r["path"] == "/api/MRP_Results" and r["query"] == {"MrpID": "m1"}
    assert "sess12345678" in r["headers"]["cookie"] and "api-auth-applicationkey" not in r["headers"]
    assert "sess12345678" not in out + err


def test_internal_writes_and_reference_only_never_send(web, capsys):
    for argv in (["web-mrp", "mrp-run"], ["web-mrp", "mrp-recalculate"], ["wms", "pos-ping"]):
        if spec.find(argv[0], argv[1]) is None:
            continue
        code, _, err = run(capsys, *argv, "--dry-run")
        assert code == 2, argv
    assert web.requests == []
    # no operation marked internal-write is runnable, whatever its name
    for o in spec.operations():
        if not o.is_public and o.runnable:
            assert o.effect.startswith("read"), o.full_name
            assert o.source == "web"


def test_internal_post_read_sends_body(web, capsys):
    web.routes[("POST", "/api/MRP_SupplySuggestions")] = (200, {"root": [], "total": 0}, {})
    code, _, _ = run(capsys, "web-mrp", "mrp-supply-suggestions", "--body", '{"MrpID": "m1", "LoadProducts": true, "limit": 10}')
    assert code == 0 and web.requests[0]["method"] == "POST"
    # form-encoded like the web app's own call (captured in a real MRP session)
    assert web.requests[0]["body"] == {"MrpID": "m1", "LoadProducts": "true", "limit": "10"}
    assert web.requests[0]["headers"]["x-requested-with"] == "XMLHttpRequest"


def test_internal_all_pages_uses_ext_paging(web, capsys):
    def page(req):
        p = int(req["query"]["page"])
        assert req["query"]["start"] == str((p - 1) * 100)
        return 200, {"root": [{}] * (100 if p == 1 else 20), "total": 120}, {}

    web.routes[("GET", "/api/MRP_vMRP")] = page
    code, out, _ = run(capsys, "web-mrp", "mrp-v-mrp", "--all-pages")
    assert code == 0 and len(json.loads(out)["root"]) == 120


def test_expired_session_message(web, capsys):
    web.routes[("GET", "/api/MRP_vMRP")] = (302, None, {"Location": "/Account/Login?ReturnUrl=%2f"})
    code, _, err = run(capsys, "web-mrp", "mrp-v-mrp")
    assert code == 1 and "session has expired" in err


def test_missing_web_cookie(web, monkeypatch, capsys):
    monkeypatch.delenv("CIN7_WEB_COOKIE")
    code, _, err = run(capsys, "web-mrp", "mrp-v-mrp")
    assert code == 3 and "browser session" in err


def test_file_download_saved(web, tmp_path, capsys):
    op = next(o for o in spec.operations() if o.file)
    web.routes[("GET", op.path)] = (200, "a,b\n1,2", {"Content-Disposition": 'attachment; filename="stock.csv"'})
    code, out, _ = run(capsys, op.group, op.name, "--save-dir", str(tmp_path))
    assert code == 0 and "stock.csv" in out
    assert (tmp_path / "stock.csv").exists()


def test_cookie_ref_stored_as_reference_only(capsys):
    code, _, err = run(capsys, "auth", "setup", "--cookie-ref", "ASPXAUTH=rawsecretvalue")
    assert code == 3 and "op:// reference" in err


# --- print ----------------------------------------------------------------------------------


def test_print_where_lists_every_endpoint_and_screens(capsys):
    from cin7_cli import printing

    code, out, _ = run(capsys, "print", "where", "-o", "csv")
    assert code == 0 and len(out.splitlines()) == len(printing.endpoints()) + 1
    code, out, _ = run(capsys, "print", "where", "production", "-o", "csv")
    assert "/ProductionOrdersList/Print" in out and "Production orders" in out
    # the run-level production pick list is not mapped: say so rather than pretend
    row = next(l for l in out.splitlines() if l.startswith("/ProductionOrder/Print"))
    assert "no (not mapped)" in row


def test_print_templates_uses_api_key(mock, capsys):
    mock.routes[("GET", "/ref/templates")] = (200, {"Page": 1, "Templates": [{"TemplateID": "t1", "Type": "Sale", "Name": "Pick"}]}, {})
    code, out, _ = run(capsys, "print", "templates", "--type", "Sale", "-o", "csv")
    assert code == 0 and "t1,Sale,Pick" in out
    assert mock.requests[0]["query"]["Type"] == "Sale" and "api-auth-applicationkey" in mock.requests[0]["headers"]


def test_print_send_is_gated(web, monkeypatch, capsys, tmp_path):
    web.routes[("POST", "/SaleList/Print")] = (200, "%PDF", {"Content-Disposition": 'attachment; filename="x.pdf"'})
    base = ["print", "send", "/SaleList/Print", "--ids", "a,b", "--template", "T", "--save-dir", str(tmp_path)]
    code, out, _ = run(capsys, *base, "--dry-run")
    assert code == 0 and "IDList=a%2Cb" in out and web.requests == []
    code, _, err = run(capsys, *base)
    assert code == 2 and "--allow-print" in err and web.requests == []
    monkeypatch.setattr(safety, "_ask", lambda prompt: "yes")
    code, _, err = run(capsys, *base, "--allow-print")
    assert code == 2 and "did not match" in err and web.requests == []
    monkeypatch.setattr(safety, "_ask", lambda prompt: "print 2")
    code, out, err = run(capsys, *base, "--allow-print")
    assert code == 0 and "x.pdf" in out and "audit line" in err
    assert web.requests[0]["body"] == {"TemplateID": "T", "IDList": "a,b"}
    assert "sess12345678" in web.requests[0]["headers"]["cookie"]


def test_print_send_refuses_unmapped_and_missing_template(web, capsys):
    code, _, err = run(capsys, "print", "send", "/ProductionOrder/Print", "--ids", "a", "--dry-run")
    assert code == 2 and "not mapped" in err
    code, _, err = run(capsys, "print", "send", "/SaleList/Print", "--ids", "a", "--dry-run")
    assert code == 2 and "template" in err
    code, _, err = run(capsys, "print", "send", "/Sale/Print", "--ids", "a,b", "--template", "T", "--dry-run")
    assert code == 2 and "one record" in err


def test_id_columns_go_to_the_far_right():
    from cin7_cli import output

    rows = [{"SaleID": "11111111-2222-3333-4444-555555555555", "CustomerReference": "#1", "CustomerID": "99999999-2222-3333-4444-555555555555",
             "OrderNumber": "SO-1", "Customer": "A", "Status": "SHIPPED", "LocationGuid": "x", "Ref": "22222222-2222-3333-4444-555555555555",
             "InvoiceAmount": 5}]
    assert output.columns(rows) == ["CustomerReference", "Customer", "OrderNumber", "Status", "InvoiceAmount", "SaleID", "CustomerID", "LocationGuid", "Ref"]
    assert output.columns(rows, ["SaleID", "Status"]) == ["SaleID", "Status"], "--fields keeps your order"
    assert output.columns([{"ID": "a", "Name": "n", "Description": "d"}]) == ["Name", "Description", "ID"]


def test_key_columns_per_table():
    from cin7_cli import output, rowtools

    product = ["ID", "Description", "Brand", "Name", "SKU", "Status", "Category", "Price"]
    assert output.key_columns(product) == ["SKU", "Name", "Status"], "SKU leads; the next two most important follow"
    assert rowtools.frozen_columns(output.columns([dict.fromkeys(product, "x")])) == 3
    po = ["ID", "OrderNumber", "Supplier", "OrderDate", "Status", "Total"]
    assert output.key_columns(po) == ["OrderNumber", "Supplier", "Status"]
    sale = ["SaleID", "OrderNumber", "CustomerReference", "Customer", "Status"]
    assert output.key_columns(sale) == ["CustomerReference", "Customer", "OrderNumber"]
    assert output.key_columns(["Foo", "Bar"]) == [] and rowtools.frozen_columns(["Foo", "Bar"]) == 0
    assert rowtools.frozen_columns(["Customer", "SKU", "Name"]) == 0, "hiding the lead column un-freezes rather than misleads"


# --- text instead of ids -----------------------------------------------------------------------


def test_text_is_resolved_to_an_id(mock, capsys):
    mock.routes[("GET", "/saleList")] = (200, {"Total": 1, "Page": 1, "SaleList": [
        {"SaleID": SALE_ID, "CustomerReference": "#25147", "OrderNumber": "SO-9", "Customer": "Acme"}]}, {})
    mock.routes[("GET", "/sale")] = (200, {"ID": SALE_ID, "Customer": "Acme"}, {})
    code, out, err = run(capsys, "sale", "get", "--id", "#25147")
    assert code == 0 and json.loads(out)["ID"] == SALE_ID
    assert mock.requests[0]["path"] == "/saleList" and mock.requests[0]["query"] == {"Search": "#25147", "Limit": "25"}
    assert mock.requests[1]["path"] == "/sale" and mock.requests[1]["query"] == {"ID": SALE_ID}
    assert "#25147" in err and "Acme" in err, "says what the text resolved to"


def test_product_lookup_tries_sku_then_name(mock, capsys):
    pid = "99999999-2222-3333-4444-555555555555"

    def products(req):
        if req["query"].get("Name") == "Pump":
            return 200, {"Total": 1, "Page": 1, "Products": [{"ID": pid, "SKU": "P-1", "Name": "Pump"}]}, {}
        return 200, {"Total": 0, "Page": 1, "Products": []}, {}

    mock.routes[("GET", "/product")] = products
    code, out, _ = run(capsys, "product", "list", "--id", "Pump", "--fields", "SKU")
    assert code == 0
    assert [r["query"] for r in mock.requests[:2]] == [{"Sku": "Pump", "Limit": "25"}, {"Name": "Pump", "Limit": "25"}]
    assert mock.requests[2]["query"] == {"ID": pid}


def test_several_matches_ask_for_an_id(mock, capsys):
    rows = [{"SaleID": f"1111111{i}-2222-3333-4444-555555555555", "CustomerReference": f"#{i}", "Customer": "Acme"} for i in range(3)]
    mock.routes[("GET", "/saleList")] = (200, {"Total": 3, "Page": 1, "SaleList": rows}, {})
    code, _, err = run(capsys, "sale", "get", "--id", "Acme")
    assert code == 2 and "3 records match" in err and rows[0]["SaleID"] in err
    assert [r["path"] for r in mock.requests] == ["/saleList"], "never guesses between several"


def test_exact_match_wins_among_several(mock, capsys):
    rows = [{"SaleID": f"1111111{i}-2222-3333-4444-555555555555", "CustomerReference": ref} for i, ref in enumerate(["#25", "#2", "#250"])]
    mock.routes[("GET", "/saleList")] = (200, {"Total": 3, "Page": 1, "SaleList": rows}, {})
    mock.routes[("GET", "/sale")] = (200, {"ID": rows[1]["SaleID"]}, {})
    code, out, _ = run(capsys, "sale", "get", "--id", "#2")
    assert code == 0 and mock.requests[1]["query"] == {"ID": rows[1]["SaleID"]}


def test_no_match_and_unresolvable(mock, capsys):
    mock.routes[("GET", "/saleList")] = (200, {"Total": 0, "Page": 1, "SaleList": []}, {})
    code, _, err = run(capsys, "sale", "get", "--id", "nope")
    assert code == 2 and "nothing matches" in err
    from cin7_cli import resolve

    group, op, prm = next((o.group, o, p) for o in spec.operations() if o.is_public for p in o.params
                          if p.type == "guid" and resolve.resolver_for(o, p) is None)
    code, _, err = run(capsys, group, op.name, f"--{prm.flag}", "not-a-guid")
    assert code == 2 and "expects a GUID" in err


def test_guid_passes_through_and_dry_run_never_looks_up(mock, capsys):
    run(capsys, "sale", "get", "--id", SALE_ID, "--dry-run")
    run(capsys, "sale", "get", "--id", "#25147", "--dry-run")
    assert mock.requests == []


def test_dates_show_without_times():
    from cin7_cli import output

    assert output.clean_date("2026-10-25T00:00:00") == "Oct 25, 2026"
    assert output.clean_date("2026-10-25T14:03:22.123Z") == "Oct 25, 2026"
    assert output.clean_date("2026-10-25 14:03") == "Oct 25, 2026"
    assert output.clean_date("2026-10-25") == "Oct 25, 2026"
    assert output.clean_date("2026-10-25T00:00:00", iso=True) == "2026-10-25"
    assert output.clean_date("0001-01-01T00:00:00") == "", "Cin7's empty date"
    for keep in ("SO-18200", "#25147", "2026-13-40x", "12:30", "Total: 2026-10-25"):
        assert output.clean_date(keep) == keep
    assert output.cell(["2026-10-25"]) == "[1]" and output.cell(5) == "5"


def test_table_and_csv_dates(mock, capsys):
    rows = [{"CustomerReference": "#1", "ShipBy": "2026-10-25T00:00:00", "OrderDate": "2026-10-20T08:30:00Z"}]
    mock.routes[("GET", "/saleList")] = [(200, {"Total": 1, "Page": 1, "SaleList": rows}, {})] * 2
    _, out, _ = run(capsys, "sale", "list", "-o", "table")
    assert "Oct 25, 2026" in out and "T00:00" not in out and "08:30" not in out
    _, out, _ = run(capsys, "sale", "list", "-o", "csv")
    assert "2026-10-25" in out and "T00" not in out


def test_currency_columns_show_dollars():
    from cin7_cli import output

    for col in ("Total", "InvoiceAmount", "Price", "Cost", "Balance", "AverageCost", "LandedCost", "SubTotal", "TaxTotal",
                "Paid", "Freight", "AmountDue"):
        assert output.is_money(col), col
    for col in ("Quantity", "TaxRule", "TaxPercent", "Discount", "OrderNumber", "Status", "DueDate", "Currency", "CostingMethod",
                "TotalCount", "ID", "PriceTier", "Account", "PaymentTerm"):
        assert not output.is_money(col), col
    assert output.cell(1234.5, col="Total") == "$1,234.50"
    assert output.cell(-5, col="Balance") == "-$5.00"
    assert output.cell(1234.5, col="Quantity") == "1234.5"
    assert output.cell(1234.5, iso=True, col="Total") == "1234.5", "files keep raw numbers"
    assert output.cell("12", col="Total") == "12", "only real numbers are formatted"


def test_table_shows_dollars_csv_does_not(mock, capsys):
    rows = [{"CustomerReference": "#1", "InvoiceAmount": 4812.5, "Quantity": 3}]
    mock.routes[("GET", "/saleList")] = [(200, {"Total": 1, "Page": 1, "SaleList": rows}, {})] * 2
    _, out, _ = run(capsys, "sale", "list", "-o", "table")
    assert "$4,812.50" in out and "3" in out
    _, out, _ = run(capsys, "sale", "list", "-o", "csv")
    assert "4812.5" in out and "$" not in out


def test_multi_value_filters_make_one_call_each_and_merge(mock, capsys):
    def rows(req):
        st = req["query"]["CombinedShippingStatus"]
        return 200, {"Total": 2, "Page": 1, "SaleList": [{"SaleID": f"id-{st}", "CustomerReference": st}, {"SaleID": "dup", "CustomerReference": "both"}]}, {}

    mock.routes[("GET", "/saleList")] = rows
    code, out, _ = run(capsys, "sale", "list", "--combined-shipping-status", "NOT SHIPPED,PARTIALLY SHIPPED")
    data = json.loads(out)
    assert code == 0 and [r["query"]["CombinedShippingStatus"] for r in mock.requests] == ["NOT SHIPPED", "PARTIALLY SHIPPED"]
    assert len(data["SaleList"]) == 3 and data["Total"] == 3 and data["Calls"] == 2


def test_too_many_combinations_refused(mock, capsys):
    vals = ",".join(["VOIDED", "NOT AVAILABLE", "PICKED", "PICKING", "NOT PICKED", "PARTIALLY PICKED"])
    code, _, err = run(capsys, "sale", "list", "--combined-pick-status", vals, "--combined-pack-status", "VOIDED,NOT AVAILABLE,PACKED")
    assert code == 2 and "too many" in err and mock.requests == []
