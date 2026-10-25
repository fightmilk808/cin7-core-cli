from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, DataTable, Input, Static, Tree  # noqa: E402

from cin7_cli import spec  # noqa: E402
import json  # noqa: E402

from cin7_cli.tui import Cin7Tui, ParamInput, first_ok  # noqa: E402

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
    app.show_writes = True
    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#ops", Tree)
        app._fill_tree("")
        await pilot.pause()
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
        tree = app.query_one("#record-tree", Tree)
        assert any("Sale/Created" in str(n.label) for n in tree.root.children)


def _ops_in_tree(tree):
    return [n.data for h in tree.root.children for g in h.children for n in g.children]


async def test_writes_hidden_by_default_and_toggle(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#ops", Tree)
        assert not app.show_writes
        shown = _ops_in_tree(tree)
        assert shown and all(first_ok(o) for o in shown)
        assert not any("Reference" in str(h.label) for h in tree.root.children)
        await pilot.press("m")
        await pilot.pause()
        everything = _ops_in_tree(tree)
        assert len(everything) == len(spec.operations()) and any(o.is_write for o in everything)
        assert "writes VISIBLE" in str(app.query_one("#modebar", Static).render())
        await pilot.press("m")
        await pilot.pause()
        assert all(first_ok(o) for o in _ops_in_tree(tree))


async def test_mode_bar_tracks_credential_in_use(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        bar = app.query_one("#modebar", Static)
        assert "WRITES OFF" in str(bar.render()) and bar.has_class("idle")
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        assert bar.has_class("public") and "API key" in str(bar.render())
        app.select(spec.find("web-mrp", "mrp-v-mrp"))
        await pilot.pause()
        assert bar.has_class("internal") and "INTERNAL" in str(bar.render())
        app.select(spec.find("wms", spec.groups()["wms"][0].name))
        await pilot.pause()
        assert bar.has_class("reference")


SALES = [
    {"SaleID": "00000000-0000-0000-0000-00000000000%d" % i, "CustomerReference": ref, "OrderNumber": f"SO-{i}",
     "Status": st, "InvoiceAmount": amt, "Customer": cu}
    for i, (ref, st, amt, cu) in enumerate(
        [("#2", "SHIPPED", 10.5, "A"), ("#1", "BACKORDERED", 99, "B"), ("#3", "ORDERED", 5, "A"), ("#10", "SHIPPED", 20, "C")]
    )
]


async def _loaded(mock, tmp_path, pilot_size=(160, 50)):
    mock.routes[("GET", "/saleList")] = (200, {"Total": 4, "Page": 1, "SaleList": SALES}, {})
    app = Cin7Tui(export_dir=tmp_path)
    return app


async def test_sort_hide_search_widths_and_fixed_columns(mock, tmp_path):
    app = await _loaded(mock, tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        table = app.query_one("#table", DataTable)
        assert app.cols[:2] == ["CustomerReference", "OrderNumber"] and table.fixed_columns == 2
        table.move_cursor(row=0, column=0)
        await pilot.press("s")  # asc
        await pilot.pause()
        assert [r["CustomerReference"] for r in app.view] == ["#1", "#10", "#2", "#3"]
        await pilot.press("s")  # desc
        assert [r["CustomerReference"] for r in app.view][0] == "#3"
        await pilot.press("s")  # off
        assert [r["CustomerReference"] for r in app.view] == ["#2", "#1", "#3", "#10"]
        table.move_cursor(row=0, column=app.cols.index("InvoiceAmount"))
        await pilot.press("s", "s")
        assert [r["InvoiceAmount"] for r in app.view] == [99, 20, 10.5, 5], "numeric, not text, order"
        table.move_cursor(row=0, column=app.cols.index("Customer"))
        await pilot.press("h")
        assert "Customer" not in [str(c.label) for c in table.columns.values()]
        await pilot.press("H")
        assert "Customer" in [str(c.label) for c in table.columns.values()]
        app.query_one("#search").value = "backorder"
        await pilot.pause()
        assert [r["CustomerReference"] for r in app.view] == ["#1"] and table.row_count == 1
        app.query_one("#search").value = ""
        await pilot.press("w")
        assert app.cell_width == 160


async def test_row_colours_and_links():
    from cin7_cli.rowtools import record_id, record_url, row_style
    import datetime as dt

    today = dt.date(2026, 10, 24)
    assert row_style({"Status": "BACKORDERED"}, today) == "red"
    assert row_style({"Status": "SHIPPED"}, today) == "green"
    assert row_style({"OrderStatus": "PICKING"}, today) == "yellow"
    assert row_style({"Status": "ORDERED", "ShipBy": "2026-10-01T00:00:00"}, today) == "red"
    assert row_style({"Status": "SHIPPED", "ShipBy": "2026-10-01T00:00:00"}, today) == "green"
    assert row_style({"Name": "x"}, today) is None
    sale = {"SaleID": "11111111-2222-3333-4444-555555555555", "CustomerID": "99999999-2222-3333-4444-555555555555"}
    assert record_id(sale) == sale["SaleID"]
    assert record_url(spec.find("sale", "list"), sale).endswith("/Sale#11111111-2222-3333-4444-555555555555")
    assert record_url(spec.find("sale", "list"), {"CustomerReference": "#1"}) is None
    po = {"ID": "11111111-2222-3333-4444-555555555555", "Type": "Advanced Purchase"}
    assert "/PurchaseAdvanced#" in record_url(spec.find("purchase", "list"), po)
    assert "/StockTransfer#" in record_url(spec.find("stock", "transfer-list"), {"TaskID": po["ID"]})
    assert "/MRP#" in record_url(spec.find("web-mrp", "mrp-v-mrp"), {"ID": po["ID"]})


async def test_copy_and_open(mock, tmp_path, monkeypatch):
    app = await _loaded(mock, tmp_path)
    copied, opened = [], []
    monkeypatch.setattr(Cin7Tui, "_copy", lambda self, text, what: copied.append(text))
    monkeypatch.setattr(Cin7Tui, "_open_url", lambda self, url: opened.append(url))
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        table = app.query_one("#table", DataTable)
        table.move_cursor(row=1, column=0)
        await pilot.press("y", "i", "u", "o")
        assert copied[0] == "#1" and copied[1] == SALES[1]["SaleID"] and copied[2].endswith("/Sale#" + SALES[1]["SaleID"])
        assert opened == [copied[2]]
        await pilot.press("Y")
        assert json.loads(copied[3])["Status"] == "BACKORDERED"


async def test_summary_record_json_and_downloads(mock, tmp_path):
    app = await _loaded(mock, tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        assert app.group_col == "Status"
        summary = app.query_one("#summary", DataTable)
        rows = [[str(c) for c in summary.get_row_at(i)] for i in range(summary.row_count)]
        assert rows[0][:2] == ["SHIPPED", "2"] and rows[-1][:2] == ["[Total]", "4"]
        assert any("Σ InvoiceAmount" == str(c.label) for c in summary.columns.values())
        assert rows[-1][-1] == "134.5"
        # record view: collapsible lazily-built tree
        table = app.query_one("#table", DataTable)
        table.move_cursor(row=0, column=0)
        await pilot.press("enter")
        await pilot.pause()
        rt = app.query_one("#record-tree", Tree)
        assert any("InvoiceAmount" in str(n.label) for n in rt.root.children)
        # json tree: nested containers are lazy until opened
        jt = app.query_one("#json-tree", Tree)
        sale_list = next(n for n in jt.root.children if "SaleList" in str(n.label))
        assert not sale_list.children
        sale_list.expand()
        await pilot.pause()
        assert len(sale_list.children) == 4
        # exports land in the Downloads tab and its history
        await pilot.press("ctrl+e")
        await pilot.pause()
        dl = app.query_one("#downloads", DataTable)
        assert dl.row_count == 1 and "cin7-sale-list" in str(dl.get_row_at(0)[2])


async def test_sidebar_toggle_resize_compact_and_persist(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        left = app.query_one("#left")
        w = app.sidebar_width
        await pilot.press("ctrl+b")
        assert left.has_class("hidden")
        await pilot.press("ctrl+b")
        assert not left.has_class("hidden")
        app.query_one("#ops").focus()
        await pilot.press("right_square_bracket")
        assert app.sidebar_width == w + 4
        await pilot.press("c")
        assert app.compact and "[dim]" not in str(app.query_one("#ops", Tree).root.children[0].children[0].label).split("]")[-1:][0]
    again = Cin7Tui(export_dir=tmp_path)
    assert again.sidebar_width == w + 4 and again.compact
