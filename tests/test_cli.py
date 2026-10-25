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
    assert len(ops) == 295
    assert len(spec.groups()) == 32
    names = [(o.group, o.name) for o in ops]
    assert len(names) == len(set(names)), "subcommand names must be unique per group"
    assert sum(o.method == "GET" for o in ops) == 102


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
    assert out.splitlines()[0].split() == ["CustomerReference", "SaleID", "Lines"]


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


def test_op_reference_resolution(mock, monkeypatch, tmp_path, capsys):
    fake = tmp_path / "bin" / "op"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\n[ "$1" = read ] && printf "resolved-%s" "$(basename "$3")"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CIN7_ACCOUNT_ID", "op://Vault/Cin7/account")
    monkeypatch.setenv("CIN7_APP_KEY", "op://Vault/Cin7/key")
    mock.routes[("GET", "/me")] = (200, {}, {})
    assert run(capsys, "me", "get")[0] == 0
    h = mock.requests[0]["headers"]
    assert h["api-auth-accountid"] == "resolved-account" and h["api-auth-applicationkey"] == "resolved-key"


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
