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


def _painted(app, region, keep=lambda style: True):
    """(text, fg, bg, bold) for every run of letters or digits painted inside a region.

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
            if style is None or not any(c.isalnum() for c in seg.text) or not keep(style):
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
