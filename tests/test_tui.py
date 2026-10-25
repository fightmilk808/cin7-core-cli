from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, DataTable, Input, Static, Tree  # noqa: E402

from cin7_cli import printing, spec  # noqa: E402
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
    return [(h, g) for h in tree.root.children for g in h.children if isinstance(g.data, tuple) and g.data[0] == "group"]


async def test_tree_sections_and_main_groups_first(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    app.show_writes = True
    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#ops", Tree)
        app._fill_tree("")
        await pilot.pause()
        heads = [str(h.label) for h in tree.root.children]
        assert "MAIN" in heads[0] and "PUBLIC API" in heads[1] and "INTERNAL" in heads[2]
        assert "PRINT" in heads[3] and "REFERENCE" in heads[4]
        main = [g.data[1] for g in tree.root.children[0].children]
        assert main == ["product", "sale", "stock", "production", "purchase"]
        groups = _groups(tree)
        assert len(groups) == len(spec.groups())
        assert sum(len(g.children) for _, g in groups) == len(spec.operations())
        assert len(tree.root.children[3].children) == len(printing.endpoints())
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
        assert app.cols[:3] == ["CustomerReference", "Customer", "OrderNumber"] and table.fixed_columns == 3
        assert app.cols[-1] == "SaleID", "ids sit at the far right"
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


async def test_row_colors_and_links():
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


# --- pinned, recent, quick views, saved filters, history, help ---------------------------------


async def test_pin_and_recent_sections(mock, tmp_path):
    mock.routes[("GET", "/saleList")] = (200, {"Total": 4, "Page": 1, "SaleList": SALES}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        await pilot.press("p")
        assert app.pins == ["sale list"]
        heads = [str(h.label) for h in app.query_one("#ops", Tree).root.children]
        assert "PINNED" in heads[0]
        app.action_run()
        await _settle(app, pilot)
        assert app.recent == ["sale list"]
        await pilot.press("p")
        assert app.pins == []
        heads = [str(h.label) for h in app.query_one("#ops", Tree).root.children]
        assert "RECENT" in heads[0]
    assert Cin7Tui(export_dir=tmp_path).recent == ["sale list"], "remembered across sessions"


async def test_quick_views_fill_run_and_sort(mock, tmp_path):
    mock.routes[("GET", "/saleList")] = (200, {"Total": 4, "Page": 1, "SaleList": SALES}, {})
    mock.routes[("GET", "/production/orderList")] = (200, {"ProductionOrderListItems": [
        {"ProductionOrderID": "a", "OrderNumber": "MO-1", "Status": "COMPLETED", "RequiredByDate": "2026-11-01T00:00:00"},
        {"ProductionOrderID": "b", "OrderNumber": "MO-3", "Status": "RELEASED", "RequiredByDate": "2026-11-09T00:00:00"},
        {"ProductionOrderID": "c", "OrderNumber": "MO-2", "Status": "RELEASED", "RequiredByDate": "2026-11-02T00:00:00"},
    ]}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("1")
        await pilot.pause()
        await _settle(app, pilot)
        await pilot.pause()
        assert mock.requests[0]["path"] == "/production/orderList" and mock.requests[0]["query"] == {"Limit": "100"}
        assert [r["OrderNumber"] for r in app.view] == ["MO-2", "MO-3"], "completed hidden, soonest due first"
        await pilot.press("4")
        await pilot.pause()
        await _settle(app, pilot)
        await pilot.pause()
        q = mock.requests[-1]["query"]
        assert q == {"Limit": "100", "OrderStatus": "AUTHORISED", "CombinedShippingStatus": "NOT SHIPPED"}
        assert app.query_one("#modebar").has_class("public")


async def test_quick_views_are_read_only_operations():
    from cin7_cli.tui import QUICK_VIEWS

    assert [v[0] for v in QUICK_VIEWS] == ["1", "2", "3", "4"]
    for _, _, group, name, params, _ in QUICK_VIEWS:
        op = spec.find(group, name)
        assert op is not None and first_ok(op) and not op.is_write
        known = {p.name.lower() for p in op.params}
        assert {k.lower() for k in params} <= known, (group, name, params)


async def test_saved_filters_round_trip(mock, tmp_path):
    mock.routes[("GET", "/saleList")] = (200, {"Total": 4, "Page": 1, "SaleList": SALES}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        _param(app, "search").value = "#25147"
        await pilot.press("ctrl+s")
        await pilot.pause()
        app.screen.query_one("#name").value = "this week"
        await pilot.press("enter")
        await pilot.pause()
        assert app.saved["this week"] == {"op": "sale list", "params": {"Search": "#25147"}, "all_pages": False}
        _param(app, "search").value = ""
        await pilot.press("ctrl+o")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await _settle(app, pilot)
        await pilot.pause()
        assert mock.requests[-1]["query"] == {"Search": "#25147"}
        await pilot.press("ctrl+o")
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert app.saved == {}


async def test_history_back_forward_and_side_pane(mock, tmp_path):
    mock.routes[("GET", "/saleList")] = [
        (200, {"Total": 1, "Page": 1, "SaleList": SALES[:1]}, {}),
        (200, {"Total": 3, "Page": 1, "SaleList": SALES[1:]}, {}),
    ]
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        await pilot.press("k")
        side = app.query_one("#side")
        assert side.has_class("on") and app.query_one("#table2", DataTable).row_count == 1
        app.action_run()
        await _settle(app, pilot)
        assert len(app.view) == 3 and len(app.history) == 2
        assert app.query_one("#table2", DataTable).row_count == 1, "pinned result stays put"
        await pilot.press("less_than_sign")
        assert len(app.view) == 1
        await pilot.press("greater_than_sign")
        assert len(app.view) == 3
        await pilot.press("k")
        assert not side.has_class("on")


async def test_help_overlay(tmp_path):
    from cin7_cli.tui import HelpScreen

    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


# --- printing -------------------------------------------------------------------------------


async def test_mark_rows_and_print_flow(mock, monkeypatch, tmp_path):
    monkeypatch.setenv("CIN7_WEB_BASE_URL", mock.url.removesuffix("/ExternalApi/v2"))
    monkeypatch.setenv("CIN7_WEB_COOKIE", "a=bbbbbbbbbbbb")
    mock.routes[("GET", "/saleList")] = (200, {"Total": 4, "Page": 1, "SaleList": SALES}, {})
    mock.routes[("GET", "/ref/templates")] = (200, {"Page": 1, "Templates": [
        {"TemplateID": "t-pick", "Type": "Sale", "Name": "Pick list"}, {"TemplateID": "t-inv", "Type": "Sale", "Name": "Invoice"}]}, {})
    mock.routes[("POST", "/SaleList/Print")] = (200, "%PDF-fake", {"Content-Disposition": 'attachment; filename="picks.pdf"'})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        table = app.query_one("#table", DataTable)
        table.move_cursor(row=0, column=0)
        await pilot.press("space")
        table.move_cursor(row=2, column=0)
        await pilot.press("space")
        assert len(app.marked) == 2 and str(table.get_row_at(0)[0]).startswith("●")
        await pilot.press("P")
        await pilot.pause()
        await pilot.pause()
        # "Print as": plain SaleList/Print is the first choice
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # pick the first template
        await pilot.pause()
        assert not any(r["path"] == "/SaleList/Print" for r in mock.requests), "nothing printed before the confirmation"
        app.screen.query_one("#name").value = "print 3"  # wrong count
        await pilot.press("enter")
        await pilot.pause()
        assert not any(r["path"] == "/SaleList/Print" for r in mock.requests)


async def test_print_confirmed_sends_form_and_saves(mock, monkeypatch, tmp_path):
    monkeypatch.setenv("CIN7_WEB_BASE_URL", mock.url.removesuffix("/ExternalApi/v2"))
    monkeypatch.setenv("CIN7_WEB_COOKIE", "a=bbbbbbbbbbbb")
    mock.routes[("POST", "/SaleList/Print")] = (200, "%PDF-fake", {"Content-Disposition": 'attachment; filename="picks.pdf"'})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        pe = printing.find("/SaleList/Print")
        app._confirm_print(pe, ["id1", "id2"], "t-pick", "Sale · Pick list")
        await pilot.pause()
        app.screen.query_one("#name").value = "print 2"
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        req = next(r for r in mock.requests if r["path"] == "/SaleList/Print")
        assert req["body"] == {"TemplateID": "t-pick", "IDList": "id1,id2"} and "cookie" in req["headers"]
        assert (tmp_path / "picks.pdf").exists()
        assert app.query_one("#downloads", DataTable).row_count == 1


async def test_print_section_is_informational(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app._show_print_info(printing.find("/SaleList/PrintCombined"))
        await pilot.pause()
        assert app.query_one("#run", Button).disabled
        assert "Sales" in str(app.query_one("#op-meta", Static).render())


# --- dropdown filters and bold drill-throughs ----------------------------------------------------


async def test_option_filters_are_dropdowns(mock, tmp_path):
    from textual.widgets import Select

    from cin7_cli.tui import ParamSelect

    mock.routes[("GET", "/saleList")] = (200, {"Total": 1, "Page": 1, "SaleList": SALES[:1]}, {})
    mock.routes[("GET", "/product")] = (200, {"Total": 0, "Page": 1, "Products": []}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        ship = app.query_one("#p-combined-shipping-status", ParamSelect)
        assert "NOT SHIPPED" in [v for _, v in ship._options if isinstance(v, str)]
        assert not isinstance(ship.value, str)
        ship.value = "NOT SHIPPED"
        _param(app, "search").value = ""
        app.action_run()
        await _settle(app, pilot)
        assert mock.requests[-1]["query"] == {"CombinedShippingStatus": "NOT SHIPPED"}, "blank dropdowns are left out"
        app.select(spec.find("product", "list"))
        await pilot.pause()
        bom = app.query_one("#p-include-bom", ParamSelect)
        bom.value = "true"
        app.action_run()
        await _settle(app, pilot)
        assert mock.requests[-1]["query"] == {"IncludeBOM": "true"}
        # saved filters carry dropdown values too
        app.action_save_filter()
        await pilot.pause()
        app.screen.query_one("#name").value = "with bom"
        await pilot.press("enter")
        await pilot.pause()
        assert app.saved["with bom"]["params"] == {"IncludeBOM": "true"}


async def test_drill_through_and_url_cells_are_bold(mock, tmp_path):
    rows = [{"SaleID": "00000000-0000-0000-0000-000000000001", "CustomerReference": "#1", "Status": "ORDERED", "Link": "https://x.test/a"}]
    mock.routes[("GET", "/saleList")] = (200, {"Total": 1, "Page": 1, "SaleList": rows}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "list"))
        await pilot.pause()
        app.action_run()
        await _settle(app, pilot)
        table = app.query_one("#table", DataTable)
        first = table.get_row_at(0)[0]
        assert first.style.bold and first.style.link and "/Sale#" in first.style.link
        link_cell = table.get_row_at(0)[app.cols.index("Link")]
        assert link_cell.style.bold and link_cell.style.link == "https://x.test/a"
        assert not table.get_row_at(0)[app.cols.index("Status")].style.bold


async def test_text_in_an_id_field_is_looked_up(mock, tmp_path):
    from cin7_cli.tui import PickScreen

    sid = "11111111-2222-3333-4444-555555555555"
    one = [{"SaleID": sid, "CustomerReference": "#25147", "Customer": "Acme"}]
    mock.routes[("GET", "/saleList")] = [(200, {"Total": 1, "Page": 1, "SaleList": one}, {})]
    mock.routes[("GET", "/sale")] = (200, {"ID": sid, "Customer": "Acme"}, {})
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        app.select(spec.find("sale", "get"))
        await pilot.pause()
        assert "ref, SO-" in _param(app, "id").placeholder
        _param(app, "id").value = "#25147"
        app.action_run()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await _settle(app, pilot)
        await pilot.pause()
        assert _param(app, "id").value == sid, "the field now shows the id it resolved to"
        assert [r["path"] for r in mock.requests] == ["/saleList", "/sale"]
        # several matches: a picker, and nothing runs until one is chosen
        many = [{"SaleID": f"2222222{i}-2222-3333-4444-555555555555", "CustomerReference": f"#{i}", "Customer": "Acme"} for i in range(2)]
        mock.routes[("GET", "/saleList")] = [(200, {"Total": 2, "Page": 1, "SaleList": many}, {})]
        mock.requests.clear()
        _param(app, "id").value = "Acme"
        app.action_run()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, PickScreen) and [r["path"] for r in mock.requests] == ["/saleList"]
        await pilot.press("down", "enter")
        await pilot.pause()
        await _settle(app, pilot)
        await pilot.pause()
        assert mock.requests[-1]["path"] == "/sale" and mock.requests[-1]["query"] == {"ID": many[1]["SaleID"]}
