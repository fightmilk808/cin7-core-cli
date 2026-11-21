from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.theme import Theme  # noqa: E402

from rich.color import Color  # noqa: E402

from cin7_cli import spec, theme  # noqa: E402
from cin7_cli.tui import Cin7Tui  # noqa: E402


async def test_light_theme_is_default(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "cin7-light"


async def test_ctrl_t_cycles(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "cin7-dark"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "cin7-light"


async def test_custom_themes_first_is_active(tmp_path):
    app = Cin7Tui(export_dir=tmp_path, themes=[Theme(name="x", primary="#ff0000")])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "x"


async def test_custom_themes_cycle_through_the_list(tmp_path):
    themes = [Theme(name="a", primary="#ff0000"), Theme(name="b", primary="#00ff00")]
    app = Cin7Tui(export_dir=tmp_path, themes=themes)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "b"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == "a"


def test_palette_values():
    t = theme.CIN7_LIGHT
    assert t.name == "cin7-light" and t.dark is False
    assert t.background == "#f3f5f8"
    assert t.primary == "#2f7ff0"
    assert t.panel == "#1c2b41"
    assert t.foreground == "#1d2633"
    assert t.error == "#e5484d"
    assert t.success == "#2fa36b"
    assert t.accent == "#18b0a8"


def test_dark_theme_keeps_todays_look():
    t = theme.CIN7_DARK
    assert t.name == "cin7-dark" and t.dark is True
    assert t.primary == "#0178D4"
    assert t.foreground == "#e0e0e0"


def test_register_registers_each_theme():
    class Fake:
        def __init__(self):
            self.seen = []

        def register_theme(self, t):
            self.seen.append(t.name)

    fake = Fake()
    theme.register(fake, [theme.CIN7_LIGHT, theme.CIN7_DARK])
    assert fake.seen == ["cin7-light", "cin7-dark"]


def test_sidebar_defaults_to_30(tmp_path, monkeypatch):
    monkeypatch.setattr("cin7_cli.tui.load_ui_state", lambda: {})
    app = Cin7Tui(export_dir=tmp_path)
    assert app.sidebar_width == 30


# --- readability: contrast measured on what is actually painted -------------------


def _lum(rgb) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rgb(color: Color | None):
    return color.get_truecolor() if color is not None else None


def _painted(app, region, keep=lambda style: True, ink=str.isalnum):
    """(text, fg, bg, bold) for every run of ink painted inside a region (letters and digits by default).

    Disabled controls are skipped: WCAG exempts them, and they are meant to look dim.
    """
    disabled = [w.region for w in app.screen.query("*") if w.disabled and w.region]
    strips = app.screen._compositor.render_strips()
    out = []
    for y in range(region.y, region.y + region.height):
        strip = strips[y].crop(region.x, region.x + region.width)
        x = region.x
        for seg in strip:
            width = seg.cell_length
            at = x
            x += width
            style = seg.style
            if style is None or not any(ink(c) for c in seg.text) or not keep(style):
                continue
            if any(r.x <= at < r.x + r.width and r.y <= y < r.y + r.height for r in disabled):
                continue
            fg, bg = _rgb(style.color), _rgb(style.bgcolor)
            if fg is not None and bg is not None:
                out.append((seg.text.strip(), fg, bg, bool(style.bold)))
    return out


def _low_contrast(runs, minimum=4.5):
    return [
        f"{text!r} fg={tuple(fg)} bg={tuple(bg)} ratio={_contrast(fg, bg):.2f}"
        for text, fg, bg, _ in runs
        if _contrast(fg, bg) < minimum
    ]


async def _settled(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_light_theme_text_is_readable(tmp_path):
    from textual.widgets import Footer, Header

    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT])
    async with app.run_test(size=(140, 40)) as pilot:
        await _settled(app, pilot)
        header = _painted(app, app.query_one(Header).region)
        footer = _painted(app, app.query_one(Footer).region)
        chips = [r for r in footer if r[3]]
        labels = [r for r in footer if not r[3]]
        sidebar = _painted(app, app.query_one("#ops").region)
        content = _painted(app, app.query_one("#right").region)
        assert header and chips and labels and sidebar and content, "nothing was painted to measure"
        assert not _low_contrast(header), "header"
        assert not _low_contrast(chips), "footer key chips"
        assert not _low_contrast(labels), "footer descriptions"
        assert not _low_contrast(sidebar), "ops tree"
        assert not _low_contrast(content), "right-hand content"
        app.select(spec.find("sale", "list"))
        await _settled(app, pilot)
        form = _painted(app, app.query_one("#right").region)
        assert form and not _low_contrast(form), "right-hand content with an operation open"


async def test_light_theme_sidebar_is_navy_and_content_is_light(tmp_path):
    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT])
    async with app.run_test(size=(140, 40)) as pilot:
        await _settled(app, pilot)
        side_bg = {tuple(bg) for _, _, bg, _ in _painted(app, app.query_one("#ops").region)}
        right_bg = {tuple(bg) for _, _, bg, _ in _painted(app, app.query_one("#op-title").region)}
        assert any(_lum(bg) < 0.05 for bg in side_bg), side_bg
        assert all(_lum(bg) > 0.8 for bg in right_bg), right_bg


async def test_dark_theme_chrome_is_unchanged(tmp_path):
    from textual.widgets import Footer, Header

    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_DARK])
    async with app.run_test(size=(140, 40)) as pilot:
        await _settled(app, pilot)
        header = _painted(app, app.query_one(Header).region)
        assert header and all(tuple(fg) == (224, 224, 224) for _, fg, _, _ in header)
        footer = _painted(app, app.query_one(Footer).region)
        assert footer and not _low_contrast(footer)


RECORD = {
    "CustomerReference": "#10001",
    "Name": "Example",
    "Quantity": 12,
    "Price": 3.5,
    "Active": True,
    "Archived": False,
    "Note": None,
    "Lines": [{"SKU": "ABC-1", "Qty": 2, "Shipped": False, "Memo": None}],
    "Address": {"City": "Springfield", "Geo": {"Lat": 1.5, "Lon": -2}},
}


def _expand_all(tree):
    def walk(node):
        node.expand()
        for child in node.children:
            walk(child)

    walk(tree.root)


async def test_light_theme_right_hand_tags_and_trees_are_readable(tmp_path):
    from textual.widgets import Tree

    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT])
    async with app.run_test(size=(140, 45)) as pilot:
        await _settled(app, pilot)
        ops = {
            "write": spec.find("attribute-set", "create"),
            "internal read": spec.find("web-settings", "account-country"),
        }
        never_sent = next(
            (o for o in spec.operations() if o.source == "web" and not o.runnable),
            None,
        )
        reference = next((o for o in spec.operations() if o.source in ("wms", "mes", "report-service")), None)
        if never_sent:
            ops["internal never sent"] = never_sent
        if reference:
            ops["reference only"] = reference
        for label, op in ops.items():
            assert op is not None, label
            app.select(op)
            await _settled(app, pilot)
            low = _low_contrast(_painted(app, app.query_one("#op-title").region))
            assert not low, f"{label}: {low}"

        app.data = RECORD
        app._show_record(RECORD)
        await _settled(app, pilot)
        record = app.query_one("#record-tree", Tree)
        _expand_all(record)
        await _settled(app, pilot)
        runs = _painted(app, record.region)
        assert len(runs) > 8
        assert not _low_contrast(runs), "record tree"
        record.cursor_line = 3
        await _settled(app, pilot)
        assert not _low_contrast(_painted(app, record.region)), "record tree, cursor moved"

        app.query_one("#results").active = "tab-json"
        jt = app.query_one("#json-tree", Tree)
        jt.root.set_label("response")
        from cin7_cli.tui import fill_json

        jt.clear()
        fill_json(jt.root, RECORD, app.tags)
        jt.root.expand()
        _expand_all(jt)
        await _settled(app, pilot)
        runs = _painted(app, jt.region)
        assert len(runs) > 8
        assert not _low_contrast(runs), "json tree"


def test_dark_json_labels_keep_todays_tags():
    from cin7_cli.tui import json_label

    assert json_label("n", 5) == "[bold]n[/]: [cyan]5[/]"
    assert json_label("s", "x") == '[bold]s[/]: [green]"x"[/]'
    assert json_label("b", True) == "[bold]b[/]: [magenta]true[/]"
    assert json_label("d", {"a": 1}) == "[bold]d[/] [dim]{1}[/]"


async def test_dark_theme_uses_todays_op_title_tags(tmp_path):
    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_DARK])
    async with app.run_test(size=(140, 40)) as pilot:
        await _settled(app, pilot)
        assert app.tags == theme.TAGS_DARK
        painted = _painted(app, app.query_one("#op-title").region)
        app.select(spec.find("attribute-set", "create"))
        await _settled(app, pilot)
        runs = _painted(app, app.query_one("#op-title").region)
        assert any(fg.red > 150 and fg.green > 150 and fg.blue < 60 for _, fg, _, _ in runs), (painted, runs)


async def _open_write_op_and_record(app, pilot):
    from textual.widgets import Tree

    from cin7_cli.tui import fill_json

    app.select(spec.find("attribute-set", "create"))
    await _settled(app, pilot)
    app.data = RECORD
    app._show_record(RECORD)
    await _settled(app, pilot)
    _expand_all(app.query_one("#record-tree", Tree))
    jt = app.query_one("#json-tree", Tree)
    jt.clear()
    jt.root.set_label("response")
    fill_json(jt.root, RECORD, app.tags)
    _expand_all(jt)
    await _settled(app, pilot)


async def _everything(app, pilot):
    """Painted text runs for the right-hand widgets, one tab at a time."""
    from textual.widgets import Tree

    out = {}
    for name, tab, tree_id in (("record", "tab-record", "#record-tree"), ("json", "tab-json", "#json-tree")):
        app.query_one("#results").active = tab
        await _settled(app, pilot)
        out[name] = _painted(app, app.query_one(tree_id, Tree).region)
        if name == "record":
            out["op-title"] = _painted(app, app.query_one("#op-title").region)
            out["right"] = _painted(app, app.query_one("#right").region)
    return out


def _colours(runs):
    """Text colour of every run (the words differ: the status line names the theme)."""
    return [tuple(f) for _, f, _, _ in runs]


async def test_ctrl_t_recolours_content_that_is_already_on_screen(tmp_path):
    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT, theme.CIN7_DARK])
    async with app.run_test(size=(140, 45)) as pilot:
        await _settled(app, pilot)
        await _open_write_op_and_record(app, pilot)

        for name, runs in (await _everything(app, pilot)).items():
            assert runs and not _low_contrast(runs), f"light start, {name}"
        await _everything(app, pilot)

        # In dark, "right" means: same colours as a fresh dark start (some of it is below 4.5 today).
        await pilot.press("ctrl+t")
        await _settled(app, pilot)
        assert app.theme == "cin7-dark"
        dark_now = await _everything(app, pilot)

        fresh = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_DARK])
        async with fresh.run_test(size=(140, 45)) as fpilot:
            await _settled(fresh, fpilot)
            await _open_write_op_and_record(fresh, fpilot)
            await _everything(fresh, fpilot)  # same tab hopping as the app above, so focus matches
            baseline = await _everything(fresh, fpilot)
        for name in baseline:
            assert baseline[name] and _colours(dark_now[name]) == _colours(baseline[name]), f"dark after switch, {name}"

        await pilot.press("ctrl+t")
        await _settled(app, pilot)
        assert app.theme == "cin7-light"
        for name, runs in (await _everything(app, pilot)).items():
            assert runs and not _low_contrast(runs), f"light after cycling, {name}"


BARS = "▁▂▃▄▅▆▇█"


class _HeatProvider:
    """A custom section with heat prefixes and coloured labels, as another app would add."""

    title = "[bold cyan]Hot items[/]"

    def nodes(self, filter_text):
        from cin7_cli.providers import Node

        return [Node(id=str(h), label=f"item {h}", icon="●", heat=h, style="cyan") for h in (100, 60, 30, 0)]

    def run(self, app, node):
        pass


async def test_light_sidebar_custom_provider_with_heat_is_readable(tmp_path):
    from textual.widgets import Tree

    from cin7_cli import providers

    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT, theme.CIN7_DARK],
                  providers=[_HeatProvider(), providers.QuickViewsProvider(), providers.OperationsProvider()])

    def ink(c):
        return c.isalnum() or c in BARS

    async with app.run_test(size=(140, 45)) as pilot:
        await _settled(app, pilot)
        app.query_one("#table").focus()  # keep the tree cursor off the rows being measured
        await _settled(app, pilot)
        for when in ("light start", "light after ctrl+t twice"):
            # the tree's rows only: its scrollbar is drawn with the same block characters
            runs = _painted(app, app.query_one("#ops", Tree).scrollable_content_region, ink=ink)
            assert len([r for r in runs if any(c in BARS for c in r[0])]) >= 4, f"{when}: heat prefixes painted"
            assert any("Hot" in r[0] for r in runs), f"{when}: section title painted"
            assert not _low_contrast(runs), f"{when}: {_low_contrast(runs)}"
            await pilot.press("ctrl+t", "ctrl+t")
            await _settled(app, pilot)
            assert app.theme == "cin7-light"


def _scrollbar_colours(app, widget):
    """(thumb colours, track colours) as shown in a widget's vertical scrollbar cells.

    Textual paints the thumb as `color=bar` with `reverse`, so a reversed cell shows its
    foreground colour; any other cell shows its background.
    """
    strips = app.screen._compositor.render_strips()
    c, r = widget.scrollable_content_region, widget.region
    thumb, track = set(), set()
    for y in range(c.y, c.y + c.height):
        for seg in strips[y].crop(c.x + c.width, r.x + r.width):
            st = seg.style
            if st is None:
                continue
            if st.reverse:
                if st.color is not None:
                    thumb.add(tuple(_rgb(st.color)))
            elif st.bgcolor is not None:
                track.add(tuple(_rgb(st.bgcolor)))
    return thumb, track


@pytest.mark.parametrize("size", [(140, 45), (100, 30)])
async def test_sidebar_scrollbar_thumb_is_visible(tmp_path, size):
    """WCAG non-text contrast: the sidebar scrollbar thumb is at least 3:1 against its track, both themes."""
    from textual.widgets import Tree

    from cin7_cli import providers

    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT, theme.CIN7_DARK],
                  providers=[_HeatProvider(), providers.QuickViewsProvider(), providers.OperationsProvider()])
    async with app.run_test(size=size) as pilot:
        await _settled(app, pilot)
        app.query_one("#table").focus()
        await _settled(app, pilot)
        for name in ("cin7-light", "cin7-dark"):
            assert app.theme == name
            tree = app.query_one("#ops", Tree)
            assert tree.show_vertical_scrollbar, "the tree is long enough to scroll"
            thumb, track = _scrollbar_colours(app, tree)
            assert thumb and track, f"{name}: scrollbar painted"
            low = [(t, k, round(_contrast(t, k), 2)) for t in thumb for k in track if _contrast(t, k) < 3.0]
            assert not low, f"{name}: thumb vs track {low}"
            if name == "cin7-light":
                assert track == {(0x1C, 0x2B, 0x41)}, "track is the navy panel"
            await pilot.press("ctrl+t")
            await _settled(app, pilot)


STATUS_ROWS = [
    {"SaleID": "a", "CustomerReference": "#10001", "Status": "BACKORDERED", "Customer": "Example A", "Amount": 10},
    {"SaleID": "b", "CustomerReference": "#10002", "Status": "SHIPPED", "Customer": "Example B", "Amount": 20},
    {"SaleID": "c", "CustomerReference": "#10003", "Status": "ORDERED", "Customer": "Example C", "Amount": 30},
    {"SaleID": "d", "CustomerReference": "#10004", "Status": "IN PROGRESS", "Customer": "Example D", "Amount": 40},
    {"SaleID": "e", "CustomerReference": "#10005", "Status": "NOTHING", "Customer": "Example E", "Amount": 50},
]


async def _run_sales(app, pilot, mock):
    mock.routes[("GET", "/saleList")] = (200, {"Total": 5, "Page": 1, "SaleList": STATUS_ROWS}, {})
    await _settled(app, pilot)
    app.select(spec.find("sale", "list"))
    await _settled(app, pilot)
    app.action_run()
    await _settled(app, pilot)
    return app.query_one("#table")


async def test_light_theme_table_is_readable(mock, tmp_path):
    """Header, every status colour, locked columns and the cursor cell all read on the light theme."""
    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_LIGHT])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await _run_sales(app, pilot, mock)
        assert table.row_count == 5 and table.fixed_columns >= 1
        region = table.region
        header = _painted(app, region.__class__(region.x, region.y, region.width, 1))
        assert any(text == "CustomerReference" for text, *_ in header), "header text was not painted"
        assert not _low_contrast(header), "table header"
        seen = set()
        for row in range(5):  # the cursor visits each row: plain cells, locked cells and the cursor cell
            table.cursor_coordinate = (row, 0)
            await pilot.pause()
            body = _painted(app, region.__class__(region.x, region.y + 1, region.width, region.height - 1))
            assert body, "nothing painted in the body"
            assert not _low_contrast(body), f"table body, cursor on row {row}"
            seen |= {tuple(fg) for _, fg, _, _ in body}
        for colour in theme.ROW_LIGHT.values():
            assert tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5)) in seen, f"{colour} never painted"
        for name in ("datatable--header-hover", "datatable--header-cursor", "datatable--cursor"):
            style = table.get_component_rich_style(name)
            fg, bg = _rgb(style.color), _rgb(style.bgcolor)
            assert _contrast(fg, bg) >= 4.5, f"{name} {fg} on {bg}"


async def test_row_colours_follow_the_theme(mock, tmp_path):
    """Dark keeps the ANSI names; ctrl+t redraws the rows in the other theme's colours."""
    app = Cin7Tui(export_dir=tmp_path, themes=[theme.CIN7_DARK, theme.CIN7_LIGHT])
    async with app.run_test(size=(140, 40)) as pilot:
        table = await _run_sales(app, pilot, mock)
        assert app.row_colours == theme.ROW_DARK
        assert table.get_row_at(0)[1].style.color.name == "red"
        table.cursor_coordinate = (2, 1)
        await pilot.press("ctrl+t")
        await _settled(app, pilot)
        assert app.theme == "cin7-light" and app.row_colours == theme.ROW_LIGHT
        assert table.row_count == 5 and table.cursor_coordinate == (2, 1)
        assert table.get_row_at(0)[1].style.color.name == "#a01e14"
        assert table.get_row_at(1)[1].style.color.name == "#185a2b"
        assert table.get_row_at(4)[1].style.color is None
