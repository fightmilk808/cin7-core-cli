from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, DataTable, Input, Static, Tree  # noqa: E402

from cin7_cli import spec  # noqa: E402
from cin7_cli.tui import Cin7Tui, ParamInput  # noqa: E402

from .conftest import KEY  # noqa: E402

SALE_ID = "11111111-2222-3333-4444-555555555555"


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def _param(app, flag: str) -> Input:
    return app.query_one(f"#p-{flag}", ParamInput)


def _groups(tree):
    return [(h, g) for h in tree.root.children for g in h.children if isinstance(g.data, tuple)]


async def test_tree_sections_and_main_groups_first(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#ops", Tree)
        heads = [str(h.label) for h in tree.root.children]
        assert "MAIN" in heads[0] and "Public API" in heads[1] and "INTERNAL" in heads[2] and "Reference" in heads[3]
        main = [g.data[1] for g in tree.root.children[0].children]
        assert main == ["product", "sale", "stock", "production", "purchase"]
        groups = _groups(tree)
        assert len(groups) == len(spec.groups())
        assert sum(len(g.children) for _, g in groups) == len(spec.operations())
        app.query_one("#filter", Input).value = "saleList"
        await pilot.pause()
        assert [g.data[1] for _, g in _groups(tree)][0] == "sale"


async def test_internal_read_uses_session_cookie(mock, monkeypatch, tmp_path):
    monkeypatch.setenv("CIN7_WEB_BASE_URL", mock.url.removesuffix("/ExternalApi/v2"))
    monkeypatch.setenv("CIN7_WEB_COOKIE", "ASP.NET_SessionId=abcdef123456; .ASPXAUTH=zzzz9999zzzz")
    mock.routes[("GET", "/api/MRP_vMRP")] = (200, {"root": [{"ID": "m1", "Name": "Run 1"}], "total": 1}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        app.select(spec.find("web-mrp", "mrp-v-mrp"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        req = mock.requests[0]
        assert req["headers"]["cookie"].startswith("ASP.NET_SessionId=") and "api-auth-applicationkey" not in req["headers"]
        assert app.query_one("#table", DataTable).row_count == 1


async def test_internal_write_is_never_sent(mock, monkeypatch, tmp_path):
    monkeypatch.setenv("CIN7_WEB_BASE_URL", mock.url.removesuffix("/ExternalApi/v2"))
    monkeypatch.setenv("CIN7_WEB_COOKIE", "a=bbbbbbbbbb")
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        op = spec.find("web-mrp", "mrp-run")
        app.select(op)
        await pilot.pause()
        assert app.query_one("#run", Button).disabled
        app.action_run()
        await _settle(app, pilot)
        assert mock.requests == []


async def test_run_list_fills_table_and_exports(mock, tmp_path):
    rows = [{"SaleID": "a", "CustomerReference": "#25147", "Lines": [1]}, {"SaleID": "b", "CustomerReference": "#25148"}]
    mock.routes[("GET", "/saleList")] = (200, {"Total": 2, "Page": 1, "SaleList": rows}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        _param(app, "search").value = "#25147"
        await pilot.press("ctrl+r")
        await _settle(app, pilot)
        assert mock.requests[0]["query"] == {"Search": "#25147"}
        assert mock.requests[0]["headers"]["api-auth-applicationkey"] == KEY
        table = app.query_one("#table", DataTable)
        assert table.row_count == 2
        assert "2 rows of 2" in str(app.query_one("#status", Static).render())
        await pilot.press("ctrl+e")
        await pilot.pause()
        files = list(tmp_path.glob("cin7-sale-list-*.csv"))
        assert len(files) == 1 and "#25147" in files[0].read_text()


async def test_all_pages(mock, tmp_path):
    def page(req):
        p = int(req["query"]["Page"])
        return 200, {"Total": 150, "Page": p, "Products": [{"ID": i} for i in range(100 if p == 1 else 50)]}, {}

    mock.routes[("GET", "/product")] = page
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        app.select(spec.find("product", "list"))
        await pilot.pause()
        box = app.query_one("#all-pages", Checkbox)
        assert not box.disabled
        box.value = True
        app.action_run()
        await _settle(app, pilot)
        assert [r["query"]["Page"] for r in mock.requests] == ["1", "2"]
        assert app.query_one("#table", DataTable).row_count == 150


async def test_write_ops_cannot_run(mock, tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        app.select(spec.find("sale", "create"))
        await pilot.pause()
        assert app.query_one("#run", Button).disabled
        app.action_run()
        await _settle(app, pilot)
        assert mock.requests == []


async def test_required_and_typed_params_are_checked(mock, tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        app.select(spec.find("sale", "get"))
        await pilot.pause()
        app.action_run()  # ID missing
        await _settle(app, pilot)
        app.select(spec.find("product", "list"))
        await pilot.pause()
        _param(app, "page").value = "two"
        app.action_run()
        await _settle(app, pilot)
        assert mock.requests == []


async def test_api_error_is_shown(mock, tmp_path):
    mock.routes[("GET", "/sale")] = (400, [{"ErrorCode": 400, "Exception": "Sale not found"}], {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        app.select(spec.find("sale", "get"))
        await pilot.pause()
        _param(app, "id").value = SALE_ID
        app.action_run()
        await _settle(app, pilot)
        assert "Sale not found" in str(app.query_one("#status", Static).render())
        assert not app.query_one("#run", Button).disabled


async def test_row_select_shows_record(mock, tmp_path):
    mock.routes[("GET", "/webhooks")] = (200, {"Webhooks": [{"ID": "w1", "Type": "Sale/Created"}]}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        app.select(spec.find("webhooks", "list"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert '"Sale/Created"' in app.query_one("#record").text
