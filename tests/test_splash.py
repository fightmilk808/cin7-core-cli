from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Tree  # noqa: E402

from cin7_cli import splash, theme  # noqa: E402
from cin7_cli.splash import Splash, SplashScreen  # noqa: E402
from cin7_cli.tui import Cin7Tui  # noqa: E402

from .test_theme import _low_contrast, _painted  # noqa: E402

ART_FILE = Path(splash.__file__).parent / "art" / "cin7.txt"

QUICK = Splash(art="CIN7", gradient=["#2f7ff0", "#18b0a8"], effect="none", seconds=0.3, tagline="tagline")


def _on_splash(app) -> bool:
    return isinstance(app.screen, SplashScreen)


def _app(tmp_path, spl=QUICK, **kw):
    return Cin7Tui(export_dir=tmp_path, splash=spl, show_splash=True, **kw)


def _long(**kw) -> Splash:
    """A splash that stays up until the test dismisses it."""
    return Splash(art="CIN7", gradient=["#2f7ff0"], effect=kw.pop("effect", "none"), seconds=kw.pop("seconds", 30), **kw)


async def _until(pilot, cond, timeout: float = 10.0) -> None:
    """Poll `cond()` every 0.02 s until it is true; fail at a monotonic deadline (slow CI is fine)."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        await pilot.pause(0.02)


# --- the default splash -----------------------------------------------------------


def test_cin7_splash_defaults():
    s = splash.CIN7_SPLASH
    assert list(s.gradient) == ["#2f7ff0", "#18b0a8"]
    assert s.effect == "fade" and s.seconds == 0.75 and s.tagline == ""
    assert s.status_provider is None
    assert s.art == ART_FILE.read_text(encoding="utf-8").rstrip("\n")
    assert "cli · tui · mcp" in s.art


def test_art_file_is_the_wordmark_and_is_packaged():
    lines = ART_FILE.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "   ┌─┐┬┌┐┌ ══╗   ┌─┐┌─┐┬─┐┌─┐"
    assert lines[2].endswith("   cli · tui · mcp")
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "art/*.txt" in pyproject


async def test_splash_shows_then_tree(tmp_path):
    app = _app(tmp_path, splash.CIN7_SPLASH)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert _on_splash(app)
        await _until(pilot, lambda: not _on_splash(app))
        assert app.focused is app.query_one("#ops", Tree)


async def test_any_key_skips_and_is_consumed(tmp_path):
    app = _app(tmp_path, _long())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert _on_splash(app)
        await pilot.press("enter")
        await pilot.pause()
        assert not _on_splash(app)
        assert app.op is None  # the enter did not reach the tree behind it
        assert app.focused is app.query_one("#ops", Tree)


async def test_app_bindings_are_swallowed_while_the_splash_is_up(tmp_path):
    app = _app(tmp_path, _long())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert not _on_splash(app)
        assert app.theme == "cin7-light"  # ctrl+t only skipped the splash


async def test_ctrl_q_quits_during_the_splash(tmp_path):
    app = _app(tmp_path, _long())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert _on_splash(app)
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.return_code == 0, "ctrl+q quit the app"


async def test_ctrl_c_reaches_the_app_during_the_splash(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(Cin7Tui, "action_help_quit", lambda self: seen.append("ctrl+c"), raising=False)
    app = _app(tmp_path, _long())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert _on_splash(app)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert seen == ["ctrl+c"]


async def test_a_click_skips(tmp_path):
    app = _app(tmp_path, _long())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.click()
        await pilot.pause()
        assert not _on_splash(app)


async def test_no_splash_by_default_in_tests(tmp_path):
    app = Cin7Tui(export_dir=tmp_path)  # the autouse fixture sets CIN7_TUI_SPLASH=0
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not _on_splash(app)


async def test_splash_none_means_no_splash(tmp_path):
    app = Cin7Tui(export_dir=tmp_path, splash=None, show_splash=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not _on_splash(app)


async def test_env_on_shows_splash_when_not_told(tmp_path, monkeypatch):
    monkeypatch.setenv("CIN7_TUI_SPLASH", "1")
    app = Cin7Tui(export_dir=tmp_path, splash=_long())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert _on_splash(app)


# --- enabled() ---------------------------------------------------------------------


def test_no_splash_flag_wins(monkeypatch):
    monkeypatch.setenv("CIN7_TUI_SPLASH", "1")
    assert splash.enabled(True) is False


def test_env_off(monkeypatch):
    monkeypatch.setenv("CIN7_TUI_SPLASH", "0")
    assert splash.enabled(False) is False


def test_env_unset_is_on(monkeypatch):
    monkeypatch.delenv("CIN7_TUI_SPLASH", raising=False)
    assert splash.enabled(False) is True


# --- small terminals ---------------------------------------------------------------


@pytest.mark.parametrize("size", [(60, 20), (30, 8), (10, 3)])
async def test_narrow_terminal(tmp_path, size):
    spl = Splash(
        art=splash.CIN7_SPLASH.art,
        gradient=["#2f7ff0", "#18b0a8"],
        effect="fade",
        seconds=30,
        tagline="x" * 80,
    )
    app = _app(tmp_path, spl)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert _on_splash(app)
        await pilot.press("enter")
        await pilot.pause()
        assert not _on_splash(app)


async def test_narrow_terminal_crops_the_art_instead_of_wrapping_it(tmp_path):
    app = _app(tmp_path, Splash(art=splash.CIN7_SPLASH.art, gradient=["#2f7ff0"], effect="none", seconds=30))
    async with app.run_test(size=(40, 12)) as pilot:
        def drawn() -> bool:
            return sum(1 for s in app.screen._compositor.render_strips() if s.text.strip()) >= 3

        await _until(pilot, drawn)
        rows = [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]
        shown = [r for r in rows if r.strip()]
        assert len(shown) == 3, rows  # three art lines, no spill-over lines
        assert all(len(r) <= 40 for r in rows)


# --- fade --------------------------------------------------------------------------


async def test_fade_animates_opacity_up(tmp_path):
    app = _app(tmp_path, _long(effect="fade", seconds=10))  # fades in over 6 s
    async with app.run_test(size=(80, 24)) as pilot:
        body = app.screen.query_one("#splash-body")
        first = body.styles.opacity
        await _until(pilot, lambda: body.styles.opacity > first)
        later = body.styles.opacity
        assert 0.0 <= first < later < 1.0


# --- custom effect -----------------------------------------------------------------


async def test_custom_effect_is_awaited_and_splash_dismisses(tmp_path):
    seen: list[object] = []

    async def effect(screen: SplashScreen) -> None:
        seen.append(screen)
        for i in range(3):
            screen.draw(f"frame {i}")
            await asyncio.sleep(0.02)

    app = _app(tmp_path, _long(effect=effect, seconds=5))
    async with app.run_test(size=(80, 24)) as pilot:
        await _until(pilot, lambda: seen and not _on_splash(app), timeout=4.0)  # not the 5 s ceiling
        assert len(seen) == 1 and isinstance(seen[0], SplashScreen)


async def test_custom_effect_can_draw_frames(tmp_path):
    gate = asyncio.Event()

    async def effect(screen: SplashScreen) -> None:
        screen.draw("FRAME-ONE")
        await gate.wait()

    app = _app(tmp_path, _long(effect=effect, seconds=5))
    async with app.run_test(size=(80, 24)) as pilot:
        await _until(pilot, lambda: _on_splash(app) and "FRAME-ONE" in str(app.screen.query_one("#splash-art").content))
        gate.set()
        await _until(pilot, lambda: not _on_splash(app), timeout=4.0)


async def test_custom_effect_is_cut_off_at_seconds(tmp_path):
    cancelled = asyncio.Event()

    async def effect(screen: SplashScreen) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    app = _app(tmp_path, _long(effect=effect, seconds=0.2))
    async with app.run_test(size=(80, 24)) as pilot:
        await _until(pilot, lambda: cancelled.is_set() and not _on_splash(app))


async def test_custom_effect_that_raises_still_dismisses(tmp_path):
    async def effect(screen: SplashScreen) -> None:
        raise RuntimeError("boom")

    app = _app(tmp_path, _long(effect=effect, seconds=5))
    async with app.run_test(size=(80, 24)) as pilot:
        await _until(pilot, lambda: not _on_splash(app), timeout=4.0)  # not the 5 s ceiling


async def test_key_skips_a_custom_effect_and_stops_it(tmp_path):
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def effect(screen: SplashScreen) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    app = _app(tmp_path, _long(effect=effect, seconds=30))
    async with app.run_test(size=(80, 24)) as pilot:
        await _until(pilot, started.is_set)
        await pilot.press("space")
        await _until(pilot, lambda: cancelled.is_set() and not _on_splash(app))


# --- status provider ---------------------------------------------------------------


def _status(app) -> str:
    if not _on_splash(app):
        return ""
    return str(app.screen.query_one("#splash-status").content).strip()


async def test_status_line_shows_the_providers_text(tmp_path):
    async def provider() -> str:
        await asyncio.sleep(0.02)
        return "3 open orders"

    app = _app(tmp_path, _long(status_provider=provider))
    async with app.run_test(size=(80, 24)) as pilot:
        await _until(pilot, lambda: _on_splash(app) and _status(app) == "3 open orders")


async def test_status_provider_that_raises_shows_a_question_mark(tmp_path):
    async def provider() -> str:
        raise RuntimeError("offline")

    app = _app(tmp_path, _long(seconds=30, status_provider=provider))
    async with app.run_test(size=(80, 24)) as pilot:
        await _until(pilot, lambda: _status(app) == "?")
        assert _on_splash(app)
        await pilot.press("space")
        await _until(pilot, lambda: not _on_splash(app))


async def test_slow_status_provider_does_not_block_dismissal(tmp_path):
    cancelled = asyncio.Event()

    async def provider() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    app = _app(tmp_path, _long(seconds=1.0, status_provider=provider))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert _on_splash(app) and _status(app) == ""
        # The splash leaves on its own 1 s timer, not the provider's.
        await _until(pilot, lambda: not _on_splash(app) and cancelled.is_set())


# --- readability in both themes -----------------------------------------------------


@pytest.mark.parametrize("t", [theme.CIN7_LIGHT, theme.CIN7_DARK], ids=lambda t: t.name)
async def test_splash_art_tagline_and_status_are_readable(tmp_path, t):
    async def provider() -> str:
        return "status line"

    spl = Splash(
        art=splash.CIN7_SPLASH.art,
        gradient=splash.CIN7_SPLASH.gradient,
        effect="none",
        seconds=30,
        tagline="read-only browser",
        status_provider=provider,
    )
    app = _app(tmp_path, spl, themes=[t])
    async with app.run_test(size=(100, 30)) as pilot:
        await _until(pilot, lambda: _on_splash(app) and _status(app) == "status line")
        await pilot.pause()
        runs = _painted(app, app.screen.region, ink=lambda c: not c.isspace())
        text = " ".join(r[0] for r in runs)
        for expected in ("┌─┐", "cli · tui · mcp", "read-only browser", "status line"):
            assert expected in text, f"{expected!r} was not painted"
        assert not _low_contrast(runs), "splash text"
        # The art keeps a gradient (not one flat colour) once made readable.
        art_colours = {tuple(r[1]) for r in runs if r[0][0] in "┌│└"}
        assert len(art_colours) >= 2


def test_readable_moves_colours_until_they_pass():
    for bg in ("#f3f5f8", "#121212"):
        for fg in ("#2f7ff0", "#18b0a8", "#808080", bg):
            out = splash.readable(fg, bg)
            assert splash.contrast(out, bg) >= 4.5
    # A colour that already passes is left alone.
    assert splash.readable("#1d2633", "#f3f5f8") == "#1d2633"
