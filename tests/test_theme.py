from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.theme import Theme  # noqa: E402

from cin7_cli import theme  # noqa: E402
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
